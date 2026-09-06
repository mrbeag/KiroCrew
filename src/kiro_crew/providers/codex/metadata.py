"""Cached dashboard reads from the authenticated Codex app-server."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir
from kiro_crew.providers.codex.client import CodexAppServerClient

_MODEL_TTL_SECONDS = 300.0
_RATE_LIMIT_TTL_SECONDS = 25.0
# Forking a large native Codex thread copies its rollout before app-server can
# acknowledge the request.  Real long-running threads can be hundreds of MB,
# so the transport's ordinary 30-second metadata timeout is too short even
# when ``excludeTurns`` keeps the eventual response bounded.
_IMPORT_FORK_TIMEOUT_SECONDS = 300.0
_THREAD_LIST_PAGE_SIZE = 25
_IMPORT_TURN_PAGE_SIZE = 100
_IMPORT_MAX_TURNS = 500

_model_lock = asyncio.Lock()
_rate_limit_lock = asyncio.Lock()
_model_cache: tuple[float, str, list[dict[str, Any]]] | None = None
_rate_limit_cache: tuple[float, str, dict[str, Any]] | None = None


def _metadata_work_dir() -> Path:
    return config_dir() / "codex-metadata"


async def _temporary_client(*, sandbox_mode: str, load_models: bool) -> CodexAppServerClient:
    client = CodexAppServerClient(
        work_dir=_metadata_work_dir(),
        sandbox_mode=sandbox_mode,
    )
    try:
        await client.start(open_thread=False, load_models=load_models)
    except BaseException:
        await client.shutdown()
        raise
    return client


async def codex_models(
    *,
    sandbox_mode: str,
    live_client: CodexAppServerClient | None = None,
) -> list[dict[str, Any]]:
    """Return Codex's own model/list catalogue without creating a thread."""
    global _model_cache
    now = time.monotonic()
    cached = _model_cache
    if cached and cached[1] == sandbox_mode and now - cached[0] < _MODEL_TTL_SECONDS:
        return [dict(item) for item in cached[2]]

    async with _model_lock:
        now = time.monotonic()
        cached = _model_cache
        if cached and cached[1] == sandbox_mode and now - cached[0] < _MODEL_TTL_SECONDS:
            return [dict(item) for item in cached[2]]

        owned_client = live_client is None
        client = live_client or await _temporary_client(sandbox_mode=sandbox_mode, load_models=True)
        try:
            # A live session can outlast catalogue changes. Refresh from the
            # server on cache expiry instead of renewing its startup snapshot.
            if live_client is not None:
                await client._load_models()
            models = [dict(item) for item in client.models if isinstance(item, dict)]
        finally:
            if owned_client:
                await client.shutdown()
        if models:
            _model_cache = (time.monotonic(), sandbox_mode, models)
        return [dict(item) for item in models]


async def codex_rate_limits(
    *,
    sandbox_mode: str,
    live_client: CodexAppServerClient | None = None,
) -> dict[str, Any]:
    """Return account/rateLimits/read without creating a Codex thread."""
    global _rate_limit_cache
    now = time.monotonic()
    cached = _rate_limit_cache
    if cached and cached[1] == sandbox_mode and now - cached[0] < _RATE_LIMIT_TTL_SECONDS:
        return dict(cached[2])

    async with _rate_limit_lock:
        now = time.monotonic()
        cached = _rate_limit_cache
        if cached and cached[1] == sandbox_mode and now - cached[0] < _RATE_LIMIT_TTL_SECONDS:
            return dict(cached[2])

        owned_client = live_client is None
        client = live_client or await _temporary_client(
            sandbox_mode=sandbox_mode, load_models=False
        )
        try:
            result = await client.request("account/rateLimits/read")
        finally:
            if owned_client:
                await client.shutdown()
        _rate_limit_cache = (time.monotonic(), sandbox_mode, dict(result))
        return dict(result)


async def codex_threads(
    *,
    sandbox_mode: str,
    limit: int = 100,
    search_term: str = "",
) -> dict[str, Any]:
    """List recent interactive Codex threads in bounded app-server frames."""
    client = await _temporary_client(sandbox_mode=sandbox_mode, load_models=False)
    try:
        requested_limit = max(1, min(100, int(limit)))
        rows: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        result: dict[str, Any] = {"data": []}

        while len(rows) < requested_limit:
            params: dict[str, Any] = {
                "limit": min(_THREAD_LIST_PAGE_SIZE, requested_limit - len(rows)),
                "sortKey": "updated_at",
                "sortDirection": "desc",
            }
            if search_term:
                params["searchTerm"] = search_term[:200]
            if cursor:
                params["cursor"] = cursor

            page = await client.request("thread/list", params)
            result = dict(page)
            data = page.get("data")
            if not isinstance(data, list):
                break
            rows.extend(data)
            next_cursor = page.get("nextCursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
                or not data
            ):
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        result["data"] = rows[:requested_limit]
        return result
    finally:
        await client.shutdown()


async def codex_import_thread(
    *,
    sandbox_mode: str,
    thread_id: str,
    mode: str,
) -> dict[str, Any]:
    """Read an original Codex thread or create a native context-preserving fork.

    Large Codex histories can make the single JSONL response from
    ``thread/fork`` / ``thread/read`` hundreds of megabytes long.  Keep the
    native history in Codex, ask the mutation/read for metadata only, then
    hydrate just the recent display transcript through the protocol's bounded
    pagination endpoint.  The fork still retains the complete native context;
    the bounded turns are only the snapshot rendered in the dashboard.
    """
    client = await _temporary_client(sandbox_mode=sandbox_mode, load_models=False)
    try:
        if mode == "fork":
            result = await client.request(
                "thread/fork",
                {
                    "threadId": thread_id,
                    "excludeTurns": True,
                    "deferGoalContinuation": True,
                    "threadSource": "codexcrew-import",
                },
                timeout=_IMPORT_FORK_TIMEOUT_SECONDS,
            )
        elif mode == "resume":
            result = await client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
        else:
            raise ValueError("mode must be 'fork' or 'resume'")

        thread = result.get("thread")
        if isinstance(thread, dict):
            imported_id = thread.get("id")
            if isinstance(imported_id, str) and imported_id:
                thread["turns"] = await _recent_thread_turns(client, imported_id)
        return result
    finally:
        await client.shutdown()


async def _recent_thread_turns(
    client: CodexAppServerClient,
    thread_id: str,
    *,
    max_turns: int = _IMPORT_MAX_TURNS,
) -> list[dict[str, Any]]:
    """Return recent turns oldest-first without one unbounded JSONL frame."""
    newest_first: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while len(newest_first) < max_turns:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "limit": min(_IMPORT_TURN_PAGE_SIZE, max_turns - len(newest_first)),
            "sortDirection": "desc",
            "itemsView": "summary",
        }
        if cursor:
            params["cursor"] = cursor
        page = await client.request("thread/turns/list", params)
        data = page.get("data")
        if not isinstance(data, list):
            break
        newest_first.extend(turn for turn in data if isinstance(turn, dict))
        next_cursor = page.get("nextCursor")
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor in seen_cursors
            or not data
        ):
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    # Descending pagination gives newest-first pages.  Dashboard transcript
    # projection expects the same chronological order as ``thread/read``.
    return list(reversed(newest_first[:max_turns]))
