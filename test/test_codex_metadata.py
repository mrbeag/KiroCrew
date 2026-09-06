"""Model catalogue freshness without restarting native Codex sessions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.providers.codex import metadata
from kiro_crew.providers.codex.client import CodexAppServerClient


@pytest.fixture
def model_cache(monkeypatch):
    monkeypatch.setattr(metadata, "_model_cache", None)
    monkeypatch.setattr(metadata, "_model_lock", asyncio.Lock())
    clock = SimpleNamespace(monotonic=lambda: 1000.0)
    monkeypatch.setattr(metadata, "time", clock)
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_live_catalogue_refreshes_startup_snapshot(
    tmp_path, monkeypatch, model_cache, expired
):
    client = CodexAppServerClient(work_dir=tmp_path)
    old = [{"id": "old-model"}]
    fresh = old + [{"id": "new-model"}]
    client.models = old
    client.thread_id = "existing-thread"
    request = AsyncMock(return_value={"data": fresh})
    monkeypatch.setattr(client, "request", request)
    shutdown = AsyncMock()
    monkeypatch.setattr(client, "shutdown", shutdown)
    if expired:
        monkeypatch.setattr(
            metadata, "_model_cache", (1000.0 - metadata._MODEL_TTL_SECONDS, "auto", old)
        )

    result = await metadata.codex_models(sandbox_mode="auto", live_client=client)

    assert result == fresh
    request.assert_awaited_once_with("model/list", {"includeHidden": False, "limit": 100})
    assert client.thread_id == "existing-thread"
    shutdown.assert_not_awaited()
    assert metadata._model_cache == (1000.0, "auto", fresh)
    result[0]["id"] = "caller-edit"
    assert await metadata.codex_models(sandbox_mode="auto", live_client=client) == fresh
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_fresh_catalogue_does_not_query_live_client(monkeypatch, model_cache):
    rows = [{"id": "served-model"}]
    monkeypatch.setattr(metadata, "_model_cache", (1000.0, "auto", rows))
    client = SimpleNamespace(_load_models=AsyncMock(), shutdown=AsyncMock())

    assert await metadata.codex_models(sandbox_mode="auto", live_client=client) == rows
    client._load_models.assert_not_awaited()
    client.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_refresh_does_not_renew_stale_cache(monkeypatch, model_cache):
    rows = [{"id": "old-model"}]
    stale = (1000.0 - metadata._MODEL_TTL_SECONDS, "auto", rows)
    monkeypatch.setattr(metadata, "_model_cache", stale)
    client = SimpleNamespace(
        models=rows,
        _load_models=AsyncMock(side_effect=RuntimeError("offline")),
        shutdown=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="offline"):
        await metadata.codex_models(sandbox_mode="auto", live_client=client)

    assert metadata._model_cache == stale
    client.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_temporary_client_is_loaded_once_and_closed(monkeypatch, model_cache):
    rows = [{"id": "served-model"}]
    client = SimpleNamespace(models=rows, _load_models=AsyncMock(), shutdown=AsyncMock())
    create = AsyncMock(return_value=client)
    monkeypatch.setattr(metadata, "_temporary_client", create)

    assert await metadata.codex_models(sandbox_mode="auto") == rows
    create.assert_awaited_once_with(sandbox_mode="auto", load_models=True)
    client._load_models.assert_not_awaited()
    client.shutdown.assert_awaited_once()
