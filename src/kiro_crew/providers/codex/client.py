"""Async JSONL client for ``codex app-server``.

Codex app-server uses a JSON-RPC-like protocol over stdin/stdout.  The client
keeps transport concerns here so :mod:`provider` only translates Codex thread
events into Kiro Crew's provider-neutral event vocabulary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import __version__, platform_compat
from kiro_crew.env import augmented_path
from kiro_crew.executors import subprocess_executor
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_SESSION_HOST,
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_agent_denied_env,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.security import redact_credentials

logger = logging.getLogger(__name__)

_INITIALIZE_TIMEOUT = 30.0
_REQUEST_TIMEOUT = 30.0
_STDOUT_BUFFER_LIMIT = 8 * 1024 * 1024
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CodexAppServerError(RuntimeError):
    """A transport or app-server request failed."""


@dataclass(frozen=True)
class CodexMessage:
    """One server notification or server-initiated request."""

    method: str
    params: dict[str, Any]
    request_id: str | int | None = None


class CodexAppServerClient:
    """One persistent ``codex app-server`` process and one Codex thread."""

    def __init__(
        self,
        *,
        work_dir: str | Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: str = "auto",
        sandbox_expose_docker_config: bool = False,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        extra_env: dict[str, str] | None = None,
        session_key: str | None = None,
        channel_id: str | None = None,
        resume_thread_id: str | None = None,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.model = (model or "").strip() or None
        self.reasoning_effort = (reasoning_effort or "").strip() or None
        self.sandbox_mode = sandbox_mode
        self.sandbox_expose_docker_config = sandbox_expose_docker_config
        self.mcp_servers = dict(mcp_servers or {})
        self.extra_env = dict(extra_env or {})
        self.session_key = session_key or ""
        self.channel_id = channel_id or ""
        self.resume_thread_id = resume_thread_id or ""

        self.thread_id = ""
        self.turn_id = ""
        self.resumed = False
        self.served_model = ""
        self.models: list[dict[str, Any]] = []

        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[str | int, asyncio.Future[dict[str, Any]]] = {}
        self._messages: asyncio.Queue[CodexMessage | BaseException] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._sandbox_cleanup: str | None = None
        self._child_pids: dict[int, tuple[int | None, bytes | None]] = {}

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self._process

    @property
    def _pid(self) -> int | None:
        """Compatibility PID consumed by Crew's generic lifecycle guards."""
        return self._process.pid if self._process is not None else None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def supports_steer(self) -> bool:
        """Expose native ``turn/steer`` to the dashboard's live-client seam.

        The chat runner publishes this inner client on ``slot._acp_client``
        while a turn is active. The mid-turn handler deliberately checks the
        capability on that published object before calling :meth:`steer`, so
        declaring it only on :class:`CodexProvider` makes native Codex steering
        look unsupported and silently demotes every steer to the yellow queue.
        """
        return True

    @property
    def steer_response_confirms_consumed(self) -> bool:
        """Codex's successful ``turn/steer`` reply is the consumption ack.

        Kiro ACP accepts a steer optimistically and later emits a separate
        ``steering_consumed`` event.  Codex app-server instead resolves the
        request only after it has accepted the input for the expected active
        turn, and does not emit that Kiro-specific echo.  The dashboard uses
        this distinction to avoid replaying an accepted Codex steer as a new
        turn when the current turn completes.
        """
        return True

    @property
    def exit_code(self) -> int | None:
        return None if self._process is None else self._process.returncode

    def set_resume_thread_id(self, thread_id: str | None) -> None:
        self.resume_thread_id = (thread_id or "").strip()

    async def start(self, *, open_thread: bool = True, load_models: bool = True) -> None:
        """Start app-server and optionally create a conversational thread.

        Dashboard catalogue and account-usage reads use the same authenticated
        app-server protocol as a chat session, but must not create empty Codex
        threads merely because the browser refreshed a picker or usage meter.
        Chat callers keep the historical behaviour through both true defaults.
        """
        if self.is_alive:
            return
        await asyncio.to_thread(self.work_dir.mkdir, parents=True, exist_ok=True)
        codex_bin = self._resolve_binary()
        app_server_argv = [
            codex_bin,
            "app-server",
            *self._mcp_config_overrides(),
            "--stdio",
        ]
        argv, self._sandbox_cleanup = await wrap_argv_async(
            app_server_argv,
            mode=self.sandbox_mode,
            strip_python_env=True,
            is_kiro_cli=False,
            expose_docker_config=self.sandbox_expose_docker_config,
            _prepare=wrap_argv,
        )
        try:
            argv = await asyncio.to_thread(cgroup_scope_argv, argv)
            env = {**os.environ, **self.extra_env}
            env = scrub_agent_denied_env(env)
            env["PATH"] = augmented_path(env.get("PATH", ""))
            if self.session_key:
                env["KIROCREW_SESSION_KEY"] = self.session_key
            else:
                env.pop("KIROCREW_SESSION_KEY", None)
            if self.channel_id:
                env["KIROCREW_CHANNEL_ID"] = self.channel_id
            else:
                env.pop("KIROCREW_CHANNEL_ID", None)

            self._process = await create_subprocess_limited(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.work_dir),
                limit=_STDOUT_BUFFER_LIMIT,
                env=env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=(
                    platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
                ),
                profile=RLIMIT_PROFILE_SESSION_HOST,
            )
        except BaseException:
            self._discard_sandbox_cleanup()
            raise

        try:
            await self._track_process_tree()
        except BaseException:
            logger.error("Could not track Codex app-server process; aborting spawn", exc_info=True)
            process = self._process
            if process is not None and process.returncode is None:
                await asyncio.to_thread(
                    platform_compat.kill_process_tree,
                    process.pid,
                    platform_compat.SIGKILL,
                )
                await process.wait()
            self._process = None
            self._discard_sandbox_cleanup()
            raise

        self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-app-server")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="codex-app-server-stderr")
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "kiro_crew",
                        "title": "Kiro Crew",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=_INITIALIZE_TIMEOUT,
            )
            await self.notify("initialized", {})
            if load_models:
                await self._load_models()
                # Catalogs may lag an explicit model selection. Let app-server
                # validate it; never silently replace the operator's choice.
            if open_thread:
                await self._open_thread()
        except BaseException:
            # Startup failures happen after the process has been registered with
            # the generic lifecycle tracker. Reap it here rather than depending
            # on the next gateway sweep to recover a half-initialized app-server.
            try:
                await self.shutdown()
            except BaseException:
                logger.exception("Codex app-server cleanup failed after startup error")
            raise

    async def _track_process_tree(self) -> None:
        """Record the root and early descendants for crash/restart cleanup."""
        process = self._process
        if process is None:
            raise CodexAppServerError("codex app-server process was not created")

        from kiro_crew.acp.client import _capture_child_records, _get_child_pids
        from kiro_crew.session_pid import _track_child_pids, _track_pid, _track_session_pid

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(subprocess_executor(), _track_pid, process.pid)
        await loop.run_in_executor(subprocess_executor(), _track_session_pid, process.pid)
        await asyncio.sleep(0.3)
        descendants = await loop.run_in_executor(
            subprocess_executor(), _get_child_pids, process.pid
        )
        if not descendants:
            return
        self._child_pids = await loop.run_in_executor(
            subprocess_executor(), _capture_child_records, descendants
        )
        await loop.run_in_executor(
            subprocess_executor(), _track_child_pids, self._child_pids, process.pid
        )

    def _mcp_config_overrides(self) -> list[str]:
        """Return process-local ``-c`` entries for Crew-managed MCP servers.

        Codex still loads the user's ordinary ``~/.codex/config.toml``. These
        overrides add Crew's own shims for this app-server process only; no user
        configuration is rewritten. Names and values come from Crew's trusted
        managed-server specs, but the grammar is validated here so a future
        caller cannot turn a dotted config path into an unrelated override.
        """
        argv: list[str] = []
        for name, spec in sorted(self.mcp_servers.items()):
            if not _MCP_NAME_RE.fullmatch(name) or not isinstance(spec, dict):
                raise CodexAppServerError(f"Invalid managed MCP server name: {name!r}")
            command = spec.get("command")
            if not isinstance(command, str) or not command:
                raise CodexAppServerError(f"Managed MCP server {name!r} has no command")
            prefix = f"mcp_servers.{name}"
            argv.extend(("-c", f"{prefix}.command={json.dumps(command)}"))
            args = spec.get("args")
            if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
                argv.extend(("-c", f"{prefix}.args={json.dumps(args)}"))
            env = spec.get("env")
            if isinstance(env, dict):
                for key, value in sorted(env.items()):
                    if not _MCP_NAME_RE.fullmatch(str(key)) or not isinstance(value, str):
                        raise CodexAppServerError(
                            f"Managed MCP server {name!r} has invalid environment data"
                        )
                    argv.extend(("-c", f"{prefix}.env.{key}={json.dumps(value)}"))
        return argv

    async def _open_thread(self) -> None:
        params: dict[str, Any] = {
            "cwd": str(self.work_dir),
            # Kiro Crew supplies the outer OS isolation. Avoid nesting Codex's
            # Linux sandbox inside it; command/file approval requests still flow
            # through app-server and are translated by CodexProvider.
            "sandbox": "danger-full-access",
            "approvalPolicy": "untrusted",
        }
        if self.model:
            params["model"] = self.model
        method = "thread/start"
        if self.resume_thread_id:
            method = "thread/resume"
            params["threadId"] = self.resume_thread_id
        try:
            result = await self.request(method, params)
        except CodexAppServerError:
            if method != "thread/resume":
                raise
            logger.warning(
                "Codex thread %s could not be resumed; starting a new thread",
                self.resume_thread_id,
                exc_info=True,
            )
            params.pop("threadId", None)
            result = await self.request("thread/start", params)
        thread = result.get("thread") or {}
        self.thread_id = str(thread.get("id") or "")
        if not self.thread_id:
            raise CodexAppServerError("codex thread response did not contain a thread id")
        self.resumed = method == "thread/resume" and self.thread_id == self.resume_thread_id
        self.served_model = str(result.get("model") or self.model or "")

    async def _load_models(self) -> None:
        cursor: str | None = None
        models: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await self.request("model/list", params)
            data = result.get("data")
            if isinstance(data, list):
                models.extend(x for x in data if isinstance(x, dict))
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        self.models = models

    async def start_turn(self, text: str) -> str:
        if not self.thread_id:
            raise CodexAppServerError("Codex thread is not initialized")
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if self.model:
            params["model"] = self.model
        if self.reasoning_effort:
            params["effort"] = self.reasoning_effort
        result = await self.request("turn/start", params)
        turn = result.get("turn") or {}
        self.turn_id = str(turn.get("id") or "")
        if not self.turn_id:
            raise CodexAppServerError("codex turn response did not contain a turn id")
        return self.turn_id

    async def steer(self, text: str) -> bool:
        if not self.thread_id or not self.turn_id:
            return False
        await self.request(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": self.turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        return True

    async def interrupt(self) -> bool:
        if not self.thread_id or not self.turn_id:
            return False
        await self.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": self.turn_id},
        )
        return True

    async def next_message(self) -> CodexMessage:
        item = await self._messages.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def respond(self, request_id: str | int, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        if not self.is_alive and method != "initialize":
            raise CodexAppServerError("codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            payload: dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            await self._write(payload)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise CodexAppServerError(f"codex app-server request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"method": method, "params": params or {}})

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexAppServerError("codex app-server stdin is unavailable")
        process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        failure: BaseException | None = None
        try:
            while line := await process.stdout.readline():
                try:
                    payload = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logger.warning("Ignoring malformed codex app-server stdout frame")
                    continue
                if not isinstance(payload, dict):
                    continue
                request_id = payload.get("id")
                method = payload.get("method")
                if request_id is not None and not method:
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    error = payload.get("error")
                    if error is not None:
                        future.set_exception(CodexAppServerError(self._format_error(error)))
                    else:
                        result = payload.get("result")
                        future.set_result(result if isinstance(result, dict) else {})
                    continue
                if isinstance(method, str):
                    params = payload.get("params")
                    await self._messages.put(
                        CodexMessage(
                            method=method,
                            params=params if isinstance(params, dict) else {},
                            request_id=request_id,
                        )
                    )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # pragma: no cover - defensive transport boundary
            failure = exc
            logger.exception("codex app-server stdout reader failed")
        finally:
            if failure is None:
                failure = CodexAppServerError(
                    f"codex app-server exited unexpectedly (code={self.exit_code})"
                )
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)
            await self._messages.put(failure)

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            text = redact_credentials(line.decode(errors="replace").rstrip())
            if text:
                logger.debug("codex app-server: %s", text)

    @staticmethod
    def _format_error(error: Any) -> str:
        if isinstance(error, dict):
            message = error.get("message")
            data = error.get("data")
            if data:
                return f"{message or 'Codex error'}: {data}"
            if message:
                return str(message)
        return str(error)

    @staticmethod
    def _resolve_binary() -> str:
        configured = os.environ.get("CODEX_BIN", "").strip()
        resolved = shutil.which(configured or "codex")
        if not resolved:
            raise CodexAppServerError(
                "Codex CLI not found. Install Codex or set CODEX_BIN to its executable path."
            )
        return resolved

    def _discard_sandbox_cleanup(self) -> None:
        path = self._sandbox_cleanup
        self._sandbox_cleanup = None
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    async def shutdown(self) -> None:
        process = self._process
        if process is None:
            self._discard_sandbox_cleanup()
            return

        saved_pid = process.pid
        saved_child_pids = dict(self._child_pids)

        # The cgroup wrapper can exit/reparent its sandbox child before this
        # client shuts down, so terminating only ``process`` can leave the real
        # app-server alive. Route tree termination through the cross-platform
        # shim: POSIX targets the isolated process group, while Windows uses the
        # Job-object/taskkill path without calling ``os.kill`` (which terminates
        # rather than probes there).
        if process.returncode is None:
            await asyncio.to_thread(
                platform_compat.kill_process_tree,
                process.pid,
                platform_compat.SIGTERM,
            )

        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 5.0)
            except TimeoutError:
                await asyncio.to_thread(
                    platform_compat.kill_process_tree,
                    process.pid,
                    platform_compat.SIGKILL,
                )
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._reader_task, self._stderr_task) if t is not None),
            return_exceptions=True,
        )
        self._process = None
        self.turn_id = ""
        await self._untrack_dead_processes(saved_pid, saved_child_pids)
        self._child_pids = {}
        self._discard_sandbox_cleanup()

    @staticmethod
    async def _untrack_dead_processes(
        root_pid: int, child_pids: dict[int, tuple[int | None, bytes | None]]
    ) -> None:
        """Drop tracking only for processes confirmed dead after teardown."""
        from kiro_crew.session_pid import (
            _pid_gone_or_unmanaged,
            _untrack_child_pids,
            _untrack_pid,
            _untrack_session_pid,
        )

        dead_children = {
            pid: record for pid, record in child_pids.items() if _pid_gone_or_unmanaged(pid)
        }
        loop = asyncio.get_running_loop()
        if dead_children:
            await loop.run_in_executor(subprocess_executor(), _untrack_child_pids, dead_children)
        survivors = [pid for pid in child_pids if pid not in dead_children]
        if survivors:
            logger.warning(
                "Retained tracking for %d live Codex child PID(s) that survived "
                "teardown; orphan sweep will reap them: %s",
                len(survivors),
                survivors,
            )
        if _pid_gone_or_unmanaged(root_pid):
            await loop.run_in_executor(subprocess_executor(), _untrack_pid, root_pid)
            await loop.run_in_executor(subprocess_executor(), _untrack_session_pid, root_pid)
        else:
            logger.warning(
                "Retained tracking for live Codex root PID %s that survived "
                "teardown; orphan sweep will reap it",
                root_pid,
            )
