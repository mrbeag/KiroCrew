"""Kiro Crew provider adapter for the native Codex app-server harness."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    PROVIDER_LABEL_CODEX,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    TurnUsage,
)
from kiro_crew.providers.base import CancelOutcome, LLMEvent, LLMProvider
from kiro_crew.providers.codex.client import CodexAppServerClient, CodexMessage
from kiro_crew.validation import MAX_TOOL_NAME_LEN

logger = logging.getLogger(__name__)

_COMMAND_APPROVAL = "item/commandExecution/requestApproval"
_FILE_APPROVAL = "item/fileChange/requestApproval"
_PERMISSIONS_APPROVAL = "item/permissions/requestApproval"


def _is_content_policy_error(message: object) -> bool:
    """Return whether an app-server error is a non-retryable content refusal."""
    normalized = str(message or "").casefold()
    return "this content was flagged" in normalized or "trusted access for cyber" in normalized


class CodexProvider(LLMProvider):
    """Native Codex harness exposed through Kiro Crew's provider contract."""

    def __init__(
        self,
        *,
        work_dir: str | Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: str = "auto",
        sandbox_expose_docker_config: bool = False,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._client = CodexAppServerClient(
            work_dir=work_dir,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            sandbox_expose_docker_config=sandbox_expose_docker_config,
            mcp_servers=mcp_servers,
            session_key=session_key,
            channel_id=channel_id,
            extra_env=extra_env,
        )
        self._active_turn = False
        self._unfinished_turn = False
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._stop_reason = ""
        self._last_steer = 0.0
        self._last_activity = time.monotonic()
        self._items: dict[str, dict[str, Any]] = {}
        self._approval_requests: dict[str | int, tuple[str, dict[str, Any]]] = {}
        self._context_window = 0
        self._context_used = 0
        self._last_usage = TurnUsage()
        self._history_replay_needed = False

    @property
    def client(self) -> CodexAppServerClient:
        return self._client

    async def start(self) -> None:
        resume_requested = bool(self._client.resume_thread_id)
        await self._client.start()
        self._history_replay_needed = resume_requested and not self._client.resumed

    async def shutdown(self) -> None:
        await self._client.shutdown()
        self._active_turn = False
        self._unfinished_turn = False
        self._turn_done.set()

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        if self._active_turn:
            raise RuntimeError("a Codex turn is already active")
        self._items.clear()
        self._approval_requests.clear()
        self._last_usage = TurnUsage()
        self._turn_done.clear()
        self._active_turn = True
        self._unfinished_turn = True
        self._stop_reason = ""
        try:
            turn_id = await self._client.start_turn(message)
            while True:
                raw = await self._client.next_message()
                if raw.params.get("threadId") not in (None, self.session_id):
                    continue
                event = self._translate(raw, turn_id)
                if event is None:
                    continue
                if isinstance(event, list):
                    for item in event:
                        yield item
                        if item.kind == EVENT_COMPLETE:
                            return
                else:
                    yield event
                    if event.kind == EVENT_COMPLETE:
                        return
        finally:
            self._active_turn = False
            self._unfinished_turn = False
            self._client.turn_id = ""
            self._turn_done.set()

    def _translate(self, raw: CodexMessage, turn_id: str) -> LLMEvent | list[LLMEvent] | None:
        method = raw.method
        params = raw.params
        msg_turn_id = params.get("turnId")
        if msg_turn_id not in (None, turn_id):
            return None
        self.touch_activity()

        if method == "item/agentMessage/delta":
            return LLMEvent(kind=EVENT_TEXT_CHUNK, text=str(params.get("delta") or ""))
        if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            return LLMEvent(kind=EVENT_THINKING_CHUNK, text=str(params.get("delta") or ""))
        if method == "item/plan/delta":
            return LLMEvent(kind=EVENT_THINKING_CHUNK, text=str(params.get("delta") or ""))
        if method == "item/started":
            item = params.get("item")
            if not isinstance(item, dict):
                return None
            item_id = str(item.get("id") or "")
            if item_id:
                self._items[item_id] = item
            return self._tool_started(item)
        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, dict):
                return None
            item_id = str(item.get("id") or "")
            if item_id:
                self._items[item_id] = item
            return self._tool_completed(item)
        if method == "item/commandExecution/outputDelta":
            item_id = str(params.get("itemId") or "")
            return LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_call_id=item_id,
                tool_output=str(params.get("delta") or ""),
                is_shell=True,
                raw_params_trusted=True,
                shell_classified=True,
            )
        if method == "item/fileChange/outputDelta":
            return LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_call_id=str(params.get("itemId") or ""),
                tool_output=str(params.get("delta") or ""),
            )
        if method == "item/mcpToolCall/progress":
            return LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_call_id=str(params.get("itemId") or ""),
                tool_output=str(params.get("message") or ""),
            )
        if method == "thread/tokenUsage/updated":
            self._capture_usage(params.get("tokenUsage"))
            return None
        if method in (_COMMAND_APPROVAL, _FILE_APPROVAL, _PERMISSIONS_APPROVAL):
            return self._permission_event(raw)
        if method == "turn/completed":
            turn = params.get("turn")
            turn = turn if isinstance(turn, dict) else {}
            status = str(turn.get("status") or "completed")
            error = turn.get("error")
            if status == "interrupted":
                reason = STOP_REASON_CANCELLED
            elif status == "failed":
                message = error.get("message") if isinstance(error, dict) else error
                reason = (
                    STOP_REASON_REFUSAL
                    if _is_content_policy_error(message)
                    else f"error: {message or 'Codex turn failed'}"
                )
            else:
                reason = STOP_REASON_END_TURN
            self._stop_reason = reason
            self._active_turn = False
            self._unfinished_turn = False
            self._turn_done.set()
            return LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=reason,
                context_usage_pct=self.context_usage_pct(),
                usage=self._last_usage,
            )
        if method == "error":
            error = params.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            # app-server follows this notification with turn/completed. Keep a
            # content-policy refusal out of the assistant stream and let the
            # terminal refusal event produce the single actionable error card.
            if _is_content_policy_error(message):
                return None
            if message:
                return LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"\nCodex error: {message}\n")
        return None

    def _tool_started(self, item: dict[str, Any]) -> LLMEvent | None:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        if item_type == "commandExecution":
            command = str(item.get("command") or "")
            raw_params: dict[str, Any] = {
                "command": command,
                "cwd": str(item.get("cwd") or self.cwd),
            }
            return LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=item_id,
                title=command or "Run command",
                tool_kind="execute",
                tool_input=json.dumps(raw_params),
                raw_tool_params=raw_params,
                is_shell=True,
                raw_params_trusted=True,
                shell_classified=True,
                tool_name="commandExecution",
            )
        if item_type == "fileChange":
            changes = item.get("changes")
            raw_params = {"changes": changes if isinstance(changes, list) else []}
            return LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=item_id,
                title=self._file_change_title(raw_params["changes"]),
                tool_kind="edit",
                tool_input=json.dumps(raw_params),
                raw_tool_params=raw_params,
                raw_params_trusted=True,
                shell_classified=True,
                tool_name="fileChange",
            )
        if item_type in ("mcpToolCall", "dynamicToolCall"):
            tool = str(item.get("tool") or "tool")
            server = str(item.get("server") or item.get("namespace") or "")
            arguments = item.get("arguments")
            raw_params = arguments if isinstance(arguments, dict) else {}
            title = f"{server}: {tool}" if server else tool
            return LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=item_id,
                title=title,
                tool_kind="mcp" if item_type == "mcpToolCall" else "other",
                tool_input=json.dumps(raw_params),
                raw_tool_params=raw_params,
                raw_params_trusted=True,
                shell_classified=True,
                tool_name=tool,
                mcp_server_name=server if item_type == "mcpToolCall" else "",
            )
        if item_type == "webSearch":
            query = str(item.get("query") or "")
            return LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=item_id,
                title=f"Search the web: {query}" if query else "Search the web",
                tool_kind="fetch",
                tool_input=json.dumps({"query": query}),
                raw_tool_params={"query": query},
                raw_params_trusted=True,
                shell_classified=True,
                tool_name="webSearch",
            )
        return None

    def _tool_completed(self, item: dict[str, Any]) -> LLMEvent | None:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        output: Any = None
        if item_type == "commandExecution":
            output = item.get("aggregatedOutput")
            if output is None:
                output = f"Command exited with code {item.get('exitCode')}"
        elif item_type == "fileChange":
            output = item.get("changes") or "File changes applied"
        elif item_type in ("mcpToolCall", "dynamicToolCall"):
            output = item.get("result")
            if output is None:
                output = item.get("contentItems")
            if output is None:
                output = item.get("error")
        elif item_type == "webSearch":
            output = item.get("results")
        else:
            return None
        if not isinstance(output, str):
            output = json.dumps(output, default=str)
        return LLMEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=item_id,
            tool_output=output,
            tool_final=True,
            is_shell=item_type == "commandExecution",
            raw_params_trusted=True,
            shell_classified=True,
        )

    def _permission_event(self, raw: CodexMessage) -> LLMEvent:
        request_id = raw.request_id
        if request_id is None:
            raise RuntimeError(f"Codex approval request {raw.method} has no request id")
        self._approval_requests[request_id] = (raw.method, dict(raw.params))
        params = raw.params
        item_id = str(params.get("itemId") or "")
        cached = self._items.get(item_id, {})
        raw_params: dict[str, Any]
        if raw.method == _COMMAND_APPROVAL:
            command = str(params.get("command") or cached.get("command") or "")
            raw_params = {"command": command, "cwd": str(params.get("cwd") or self.cwd)}
            title = command or str(params.get("reason") or "Run command")
            kind = "execute"
            is_shell = True
            tool_name = "commandExecution"
        elif raw.method == _FILE_APPROVAL:
            changes = cached.get("changes")
            raw_params = {"changes": changes if isinstance(changes, list) else []}
            fallback_title = self._file_change_title(raw_params["changes"])
            reason = str(params.get("reason") or "")
            title = reason if reason and len(reason) <= MAX_TOOL_NAME_LEN else fallback_title
            kind = "edit"
            is_shell = False
            tool_name = "fileChange"
        else:
            raw_params = {
                "permissions": params.get("permissions") or {},
                "cwd": str(params.get("cwd") or self.cwd),
            }
            title = str(params.get("reason") or "Grant additional permissions")
            kind = "other"
            is_shell = False
            tool_name = "permissions"
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=request_id,
            tool_call_id=item_id,
            title=title,
            tool_kind=kind,
            tool_input=json.dumps(raw_params),
            raw_tool_params=raw_params,
            options=[
                {"optionId": "accept", "name": "Allow once", "kind": "allow_once"},
                {
                    "optionId": "acceptForSession",
                    "name": "Allow for session",
                    "kind": "allow_always",
                },
                {"optionId": "decline", "name": "Deny", "kind": "reject_once"},
            ],
            is_shell=is_shell,
            raw_params_trusted=True,
            shell_classified=True,
            tool_name=tool_name,
        )

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        method, params = self._approval_requests.pop(request_id, ("", {}))
        if method == _PERMISSIONS_APPROVAL:
            # App-server's permission-profile approval is structurally different
            # from command/file decisions.  Return the requested profile for this
            # turn only; Kiro Crew never silently grants it for the whole thread.
            await self._client.respond(
                request_id,
                {
                    "permissions": params.get("permissions") or {},
                    "scope": "turn",
                    "strictAutoReview": True,
                },
            )
            return
        decision = "acceptForSession" if always else "accept"
        await self._client.respond(request_id, {"decision": decision})

    async def reject_tool(self, request_id: str | int) -> None:
        method, _params = self._approval_requests.pop(request_id, ("", {}))
        if method == _PERMISSIONS_APPROVAL:
            await self._client.respond(
                request_id,
                {"permissions": {}, "scope": "turn", "strictAutoReview": True},
            )
            return
        await self._client.respond(request_id, {"decision": "decline"})

    def _capture_usage(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        last = payload.get("last")
        if not isinstance(last, dict):
            return
        self._context_window = int(payload.get("modelContextWindow") or 0)
        self._context_used = int(last.get("totalTokens") or 0)
        self._last_usage = TurnUsage(
            input_tokens=int(last.get("inputTokens") or 0),
            output_tokens=int(last.get("outputTokens") or 0),
            cache_read_tokens=int(last.get("cachedInputTokens") or 0),
            num_turns=1,
        )

    @staticmethod
    def _file_change_title(changes: list[Any]) -> str:
        paths: list[str] = []
        for change in changes:
            if isinstance(change, dict):
                path = change.get("path")
                if path:
                    paths.append(str(path))
        if not paths:
            return "Apply file changes"

        displayed = paths[:3]
        full_title = f"Change {', '.join(displayed)}"
        if len(full_title) <= MAX_TOOL_NAME_LEN:
            return full_title

        # Approval titles are display text, while the complete paths and diffs
        # remain in ``raw_tool_params`` / ``tool_input``.  Long absolute paths
        # must not make an otherwise valid edit fail the dashboard's bounded
        # tool-name validation before an approval card can be shown.
        names = [path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] for path in displayed]
        count = len(paths)
        noun = "file" if count == 1 else "files"
        short_title = f"Change {count} {noun}: {', '.join(names)}"
        if len(short_title) <= MAX_TOOL_NAME_LEN:
            return short_title
        return f"{short_title[: MAX_TOOL_NAME_LEN - 1].rstrip()}…"

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        if not self._active_turn:
            return "no_turn"
        try:
            await self._client.interrupt()
        except Exception:
            logger.debug("Codex turn interrupt failed", exc_info=True)
            return "error"
        if wait_ack_timeout <= 0:
            return "acked"
        try:
            await asyncio.wait_for(self._turn_done.wait(), wait_ack_timeout)
        except TimeoutError:
            return "timeout"
        return (
            "acked"
            if self._stop_reason in (STOP_REASON_CANCELLED, STOP_REASON_END_TURN)
            else "timeout"
        )

    async def steer(self, message: str) -> bool:
        if not self._active_turn:
            return False
        try:
            accepted = await self._client.steer(message)
        except Exception:
            logger.debug("Codex turn steer failed", exc_info=True)
            return False
        if accepted:
            self._last_steer = time.monotonic()
        return accepted

    @property
    def supports_steer(self) -> bool:
        return True

    @property
    def last_steer_monotonic(self) -> float:
        return self._last_steer

    def has_active_turn(self) -> bool:
        return self._active_turn

    def has_unfinished_turn(self) -> bool:
        return self._unfinished_turn

    async def wait_turn_done(self, timeout: float) -> str:
        await asyncio.wait_for(self._turn_done.wait(), timeout)
        return self._stop_reason

    def context_usage_pct(self) -> float:
        if self._context_window <= 0:
            return 0.0
        return min(100.0, self._context_used * 100.0 / self._context_window)

    def context_window_tokens(self) -> int:
        return self._context_window

    def context_used_tokens(self) -> int:
        return self._context_used

    @property
    def session_id(self) -> str:
        return self._client.thread_id

    def set_resume_session_id(self, session_id: str | None) -> None:
        self._client.set_resume_thread_id(session_id)

    @property
    def supports_native_resume(self) -> bool:
        return True

    @property
    def resumed(self) -> bool:
        return self._client.resumed

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABEL_CODEX

    @property
    def cwd(self) -> str:
        return str(self._client.work_dir)

    @property
    def served_model(self) -> str:
        return self._client.served_model

    def available_models(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for model in self._client.models:
            model_id = str(model.get("id") or model.get("model") or "")
            if not model_id:
                continue
            result.append(
                {
                    "modelId": model_id,
                    "name": str(model.get("displayName") or model_id),
                    "description": str(model.get("description") or ""),
                }
            )
        return result

    def get_valid_effort_levels(self) -> list[str]:
        served = self.served_model or self._client.model or ""
        for model in self._client.models:
            if str(model.get("id") or "") != served:
                continue
            options = model.get("supportedReasoningEfforts")
            if not isinstance(options, list):
                return []
            return [
                str(option.get("reasoningEffort"))
                for option in options
                if isinstance(option, dict) and option.get("reasoningEffort")
            ]
        return []

    def is_alive(self) -> bool:
        return self._client.is_alive

    def is_process_alive(self) -> bool:
        return self._client.is_alive

    @property
    def exit_code(self) -> int | None:
        return self._client.exit_code

    def runtime_info(self) -> tuple[int | None, str | None]:
        process = self._client.process
        return (process.pid if process is not None else None, None)

    def touch_activity(self) -> None:
        self._last_activity = time.monotonic()


def create_codex_provider_factory(cfg: Any) -> Callable[..., CodexProvider]:
    """Build the standard Kiro Crew provider factory for Codex app-server."""

    from kiro_crew.acp.client import DEFAULT_MODEL
    from kiro_crew.config.loader import _session_work_dir, docker_registry_access_enabled

    configured_model = str(cfg.agent.model or "").strip()
    if configured_model == DEFAULT_MODEL:
        configured_model = ""
    sandbox_mode = str(cfg.agent.sandbox or "auto")
    default_effort = str(cfg.agent.reasoning_effort or "").strip()

    # Reuse the same dynamically resolved commands and data-home environment as
    # the Kiro agent spec, but inject only Crew-owned servers. User MCP servers
    # remain owned by Codex's normal config.toml and are never copied or written.
    from kiro_crew.agent import build_agent_config

    agent_config = build_agent_config()
    all_servers = agent_config.get("mcpServers")
    all_servers = all_servers if isinstance(all_servers, dict) else {}
    managed_names = frozenset(("kirocrew-core", "kirocrew-cron", "kirocrew-computer"))
    managed_mcp_servers = {
        name: {key: value for key, value in spec.items() if key in ("command", "args", "env")}
        for name, spec in all_servers.items()
        if name in managed_names and isinstance(spec, dict)
    }

    def _codex(
        session_key: str | None = None,
        agent: str | None = None,
        channel_id: str | None = None,
        model_override: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        reasoning_effort_override: str | None = None,
        crew_agent: str | None = None,
        **_kwargs: object,
    ) -> CodexProvider:
        del crew_agent
        work_dir = Path(cwd) if cwd else _session_work_dir(session_key)
        model = (model_override or configured_model or "").strip() or None
        if model == DEFAULT_MODEL:
            model = None  # Crew's unset sentinel delegates to Codex's own default.
        if reasoning_effort_override:
            effort = reasoning_effort_override
        elif agent in ("kirocrew-lite", "kirocrew-heartbeat"):
            effort = cfg.agent.resolve_effort("background")
        else:
            effort = default_effort
        return CodexProvider(
            work_dir=work_dir,
            model=model,
            reasoning_effort=effort or None,
            sandbox_mode=sandbox_mode,
            mcp_servers=managed_mcp_servers,
            sandbox_expose_docker_config=docker_registry_access_enabled(),
            session_key=session_key,
            channel_id=channel_id,
            extra_env=extra_env,
        )

    return _codex


async def fetch_codex_models(cfg: Any) -> list[dict[str, Any]]:
    """Read the cached authenticated catalog without creating a chat thread."""
    from kiro_crew.providers.codex.metadata import codex_models

    models = await codex_models(sandbox_mode=str(cfg.agent.sandbox or "auto"))
    return [
        {
            "model_name": str(model.get("id") or model.get("model")),
            "description": str(model.get("description") or ""),
        }
        for model in models
        if model.get("id") or model.get("model")
    ]
