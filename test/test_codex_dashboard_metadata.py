"""Dashboard endpoints use the native Codex metadata client."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_dashboard_model_catalog_comes_from_codex_app_server() -> None:
    from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents

    cfg = KiroCrewConfig(agent=AgentConfig(acp_backend="codex"))
    request = MagicMock()
    request.app = {"state": SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: []))}
    rows = [
        {
            "id": "gpt-5.6-sol",
            "displayName": "GPT-5.6 Sol",
            "description": "Frontier coding model",
        }
    ]
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=cfg),
        patch("kiro_crew.providers.codex.metadata.codex_models", AsyncMock(return_value=rows)),
    ):
        response = await agents.api_models(request)

    assert response.status == 200
    payload = json.loads(response.body)
    assert [row["model_name"] for row in payload] == ["gpt-5.6-sol"]


@pytest.mark.asyncio
async def test_dashboard_codex_usage_normalizes_rate_limit_windows() -> None:
    from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
    from kiro_crew.dashboard.handlers import usage

    cfg = KiroCrewConfig(agent=AgentConfig(acp_backend="codex"))
    request = MagicMock()
    request.app = {"state": SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: []))}
    raw = {
        "rateLimits": {
            "planType": "plus",
            "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1},
            "secondary": {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 2},
        }
    }
    with (
        patch.object(usage.KiroCrewConfig, "load", return_value=cfg),
        patch(
            "kiro_crew.providers.codex.metadata.codex_rate_limits",
            AsyncMock(return_value=raw),
        ),
    ):
        response = await usage.api_codex_usage(request)

    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["plan"] == "plus"
    assert payload["primary"] == {
        "used_percent": 12,
        "window_minutes": 300,
        "resets_at": 1,
    }
