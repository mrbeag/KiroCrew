"""Import native Codex CLI threads into dashboard chat slots."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web

from kiro_crew.acp.types import ACP_BACKEND_CODEX, PROVIDER_LABEL_CODEX
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_runner import schedule_eager_spawn
from kiro_crew.dashboard.chat_utils import (
    _redact_for_display,
    _sync_dashboard_slots,
    effective_session_key,
)
from kiro_crew.dashboard.state import DashboardState, request_slot_origin
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_MAX_IMPORTED_MESSAGES = 500


async def _owner_only(request: web.Request) -> web.Response | None:
    """Local CLI history is available only to the authenticated dashboard owner."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    return await require_owner_dashboard_request(request, "codex.threads")


def _thread_title(thread: dict[str, Any]) -> str:
    for key in ("name", "preview"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return _redact_for_display(value.strip())[:200]
    return "Imported Codex session"


def _iso_timestamp(epoch: object, offset: int) -> str:
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        base = datetime.fromtimestamp(epoch, timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    return (base + timedelta(microseconds=offset)).isoformat()


def codex_thread_messages(thread: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Project native turns into the user/assistant rows Kiro Crew displays."""
    rows: list[tuple[str, str, str]] = []
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return rows
    offset = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            content = ""
            role = ""
            epoch: object = turn.get("startedAt")
            if kind == "userMessage":
                role = "user"
                parts = item.get("content")
                if isinstance(parts, list):
                    content = "\n".join(
                        part.get("text", "")
                        for part in parts
                        if isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    )
            elif kind == "agentMessage":
                role = "assistant"
                epoch = turn.get("completedAt") or turn.get("startedAt")
                content = item.get("text", "") if isinstance(item.get("text"), str) else ""
            if role and content.strip():
                rows.append(
                    (role, _redact_for_display(content.strip()), _iso_timestamp(epoch, offset))
                )
                offset += 1
    return rows[-_MAX_IMPORTED_MESSAGES:]


async def api_codex_threads(request: web.Request) -> web.Response:
    """GET /api/codex/threads — recent native Codex CLI/app-server sessions."""
    denied = await _owner_only(request)
    if denied is not None:
        return denied
    cfg = KiroCrewConfig.load()
    if cfg.agent.acp_backend != ACP_BACKEND_CODEX:
        return web.json_response({"error": "Codex harness is not selected"}, status=409)
    search = str(request.query.get("search", "")).strip()[:200]
    try:
        from kiro_crew.providers.codex.metadata import codex_threads

        result = await codex_threads(
            sandbox_mode=cfg.agent.sandbox,
            limit=100,
            search_term=search,
        )
    except Exception:
        logger.warning("Codex thread list unavailable", exc_info=True)
        return web.json_response({"error": "Codex sessions are unavailable"}, status=503)

    state: DashboardState = request.app["state"]
    rows: list[dict[str, Any]] = []
    for raw in result.get("data", []):
        if not isinstance(raw, dict):
            continue
        thread_id = raw.get("id")
        if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
            continue
        existing = state.sessions.find_key_by_sid(thread_id) if state.sessions else None
        rows.append(
            {
                "id": thread_id,
                "title": _thread_title(raw),
                "preview": _redact_for_display(str(raw.get("preview") or ""))[:500],
                "cwd": str(raw.get("cwd") or ""),
                "created_at": raw.get("createdAt"),
                "updated_at": raw.get("updatedAt"),
                "source": raw.get("source"),
                "imported": bool(existing),
                "local_session": existing or "",
            }
        )
    return web.json_response({"threads": rows})


async def api_codex_thread_import(request: web.Request) -> web.Response:
    """POST /api/codex/threads/import — fork or resume a native Codex thread."""
    denied = await _owner_only(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    thread_id = str(body.get("thread_id") or "").strip()
    mode = str(body.get("mode") or "fork").strip()
    if not _THREAD_ID_RE.fullmatch(thread_id):
        return web.json_response({"error": "invalid Codex thread id"}, status=400)
    if mode not in ("fork", "resume"):
        return web.json_response({"error": "mode must be fork or resume"}, status=400)

    cfg = KiroCrewConfig.load()
    if cfg.agent.acp_backend != ACP_BACKEND_CODEX:
        return web.json_response({"error": "Codex harness is not selected"}, status=409)
    state: DashboardState = request.app["state"]
    if mode == "resume" and state.sessions.find_key_by_sid(thread_id):
        return web.json_response({"error": "that Codex thread is already imported"}, status=409)

    try:
        from kiro_crew.providers.codex.metadata import codex_import_thread

        result = await codex_import_thread(
            sandbox_mode=cfg.agent.sandbox,
            thread_id=thread_id,
            mode=mode,
        )
    except Exception:
        logger.warning("Codex thread import failed", exc_info=True)
        return web.json_response({"error": "Codex session import failed"}, status=502)

    thread = result.get("thread")
    if not isinstance(thread, dict):
        return web.json_response({"error": "Codex returned no thread"}, status=502)
    imported_id = thread.get("id")
    if not isinstance(imported_id, str) or not _THREAD_ID_RE.fullmatch(imported_id):
        return web.json_response({"error": "Codex returned an invalid thread id"}, status=502)
    # Close the post-request race before publishing a second owner of the same
    # native thread. A fork has a fresh ID, but the same guard is harmless there.
    if state.sessions.find_key_by_sid(imported_id):
        return web.json_response({"error": "that Codex thread is already imported"}, status=409)

    with state.suspend_slots_push():
        slot = state.get_or_create_slot(
            None,
            origin=request_slot_origin(request.get("app", "")),
        )
        slot.title = _thread_title(thread)
        slot._titled = True
        slot._title_origin = "user"
        cwd = str(thread.get("cwd") or "")
        safe_cwd = ""
        if cwd and os.path.isabs(cwd) and os.path.isdir(cwd) and not is_sensitive_path(cwd):
            safe_cwd = os.path.realpath(cwd)
            slot.project = safe_cwd
        for role, content, ts in codex_thread_messages(thread):
            slot.append(
                role,
                content,
                "msg msg-u" if role == "user" else "msg msg-a",
                ts=ts,
                broadcast=False,
            )
        # Imported rows are a replay snapshot, not live output waiting for an
        # SSE reader. Keeping them in the delivery queue would retain a second
        # copy until the next turn even though no client should consume them.
        slot._pending.clear()
        slot.event.clear()
        state.sessions.seed_conversation(
            effective_session_key(slot),
            imported_id,
            provider=PROVIDER_LABEL_CODEX,
            cwd=safe_cwd,
        )
        _sync_dashboard_slots(state)
        state.push_slots_update()
    try:
        await state.sessions.aflush()
    except Exception:
        # The in-memory mapping is already authoritative and the map re-arms
        # its dirty flag on failure. Returning an error here would invite a
        # retry that forks the source thread a second time.
        logger.warning("Codex import session-map flush deferred", exc_info=True)
    await save_slot_off_loop(state, slot, force=True)
    schedule_eager_spawn(state, slot)
    payload = state.serialize_slot(slot)
    payload.update(
        {
            "codex_thread_id": imported_id,
            "import_mode": mode,
            "imported_messages": len(slot.messages),
        }
    )
    return web.json_response(payload)
