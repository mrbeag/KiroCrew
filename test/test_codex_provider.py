"""Native Codex app-server provider contract tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    PROVIDER_LABEL_CODEX,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
)
from kiro_crew.providers.codex.client import CodexAppServerClient, CodexMessage
from kiro_crew.providers.codex.provider import CodexProvider
from kiro_crew.session_pid import _MANAGED_AGENT_MARKERS
from kiro_crew.validation import MAX_TOOL_NAME_LEN


@pytest.mark.asyncio
async def test_codex_client_shutdown_terminates_complete_process_tree(tmp_path) -> None:
    client = CodexAppServerClient(work_dir=tmp_path)
    process = SimpleNamespace(pid=43210, returncode=None, wait=AsyncMock(return_value=0))
    client._process = process

    with patch("kiro_crew.providers.codex.client.platform_compat.kill_process_tree") as kill_tree:
        await client.shutdown()

    kill_tree.assert_called_once_with(43210, platform_compat.SIGTERM)
    process.wait.assert_awaited_once()
    assert client._process is None


def test_codex_client_exposes_root_pid_to_generic_lifecycle_guards(tmp_path) -> None:
    client = CodexAppServerClient(work_dir=tmp_path)
    assert client._pid is None

    client._process = SimpleNamespace(pid=43209)

    assert client._pid == 43209


@pytest.mark.asyncio
async def test_codex_client_shutdown_kills_process_tree_after_timeout(tmp_path) -> None:
    client = CodexAppServerClient(work_dir=tmp_path)
    process = SimpleNamespace(pid=43211, returncode=None, wait=AsyncMock(return_value=0))
    client._process = process

    async def force_timeout(awaitable, _timeout):
        awaitable.close()
        raise TimeoutError

    with (
        patch("kiro_crew.providers.codex.client.platform_compat.kill_process_tree") as kill_tree,
        patch("kiro_crew.providers.codex.client.asyncio.wait_for", side_effect=force_timeout),
    ):
        await client.shutdown()

    assert [item.args for item in kill_tree.call_args_list] == [
        (43211, platform_compat.SIGTERM),
        (43211, platform_compat.SIGKILL),
    ]
    assert process.wait.call_count == 2
    assert process.wait.await_count == 1
    assert client._process is None


def test_codex_processes_are_owned_by_tracked_pid_sweep() -> None:
    assert "codex" in _MANAGED_AGENT_MARKERS


class TestCodexProviderTranslation:
    @pytest.mark.asyncio
    async def test_failed_native_resume_requests_crew_history_replay(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path)
        provider.set_resume_session_id("missing-thread")
        provider.client.start = AsyncMock()
        provider.client.resumed = False

        await provider.start()

        assert provider._history_replay_needed is True

    def test_native_text_and_command_events(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path)
        text = provider._translate(
            CodexMessage(
                method="item/agentMessage/delta",
                params={"threadId": "thread-1", "turnId": "turn-1", "delta": "hello"},
            ),
            "turn-1",
        )
        assert text is not None and not isinstance(text, list)
        assert text.kind == EVENT_TEXT_CHUNK
        assert text.text == "hello"

        command = provider._translate(
            CodexMessage(
                method="item/started",
                params={
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "commandExecution",
                        "id": "item-1",
                        "command": "git status --short",
                        "cwd": str(tmp_path),
                    },
                },
            ),
            "turn-1",
        )
        assert command is not None and not isinstance(command, list)
        assert command.kind == EVENT_TOOL_CALL
        assert command.shell_command == "git status --short"
        assert command.raw_params_trusted is True
        assert command.shell_classified is True

    @pytest.mark.asyncio
    async def test_command_approval_round_trips_native_decision(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path)
        provider.client.thread_id = "thread-1"
        provider.client.respond = AsyncMock()
        request = provider._translate(
            CodexMessage(
                method="item/commandExecution/requestApproval",
                request_id=42,
                params={
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "command": "docker pull example/image:latest",
                },
            ),
            "turn-1",
        )
        assert request is not None and not isinstance(request, list)
        assert request.kind == EVENT_PERMISSION_REQUEST
        assert request.shell_command == "docker pull example/image:latest"
        await provider.approve_tool(42, always=True)
        provider.client.respond.assert_awaited_once_with(42, {"decision": "acceptForSession"})

    def test_long_multi_file_edit_keeps_approval_title_bounded(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path)
        prefix = str(tmp_path / ("long-worktree-segment-" * 4))
        changes = [
            {"path": f"{prefix}/cccm-vip.c", "kind": {"type": "update"}, "diff": "a"},
            {
                "path": f"{prefix}/cccm-vip-abi.h",
                "kind": {"type": "add"},
                "diff": "b",
            },
            {
                "path": f"{prefix}/cccm-vip-machine-test.c",
                "kind": {"type": "update"},
                "diff": "c",
            },
        ]
        assert len("Change " + ", ".join(change["path"] for change in changes)) > 256

        started = provider._translate(
            CodexMessage(
                method="item/started",
                params={
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "fileChange", "id": "item-1", "changes": changes},
                },
            ),
            "turn-1",
        )
        request = provider._translate(
            CodexMessage(
                method="item/fileChange/requestApproval",
                request_id="req-1",
                params={
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                },
            ),
            "turn-1",
        )

        assert started is not None and not isinstance(started, list)
        assert request is not None and not isinstance(request, list)
        assert started.title == request.title
        assert request.title == (
            "Change 3 files: cccm-vip.c, cccm-vip-abi.h, cccm-vip-machine-test.c"
        )
        assert len(request.title) <= MAX_TOOL_NAME_LEN
        assert request.tool_name == "fileChange"
        assert request.raw_tool_params == {"changes": changes}
        assert json.loads(request.tool_input) == {"changes": changes}

    def test_completion_and_capabilities(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path)
        provider._active_turn = True
        provider._unfinished_turn = True
        event = provider._translate(
            CodexMessage(
                method="turn/completed",
                params={
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed", "items": []},
                },
            ),
            "turn-1",
        )
        assert event is not None and not isinstance(event, list)
        assert event.kind == EVENT_COMPLETE
        assert event.stop_reason == STOP_REASON_END_TURN
        assert provider.supports_steer is True
        assert provider.client.supports_steer is True
        assert provider.client.steer_response_confirms_consumed is True
        assert provider.provider_label == PROVIDER_LABEL_CODEX
        assert provider.has_unfinished_turn() is False

    def test_cybersecurity_policy_error_is_a_terminal_refusal(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path)
        message = (
            "This content was flagged for possible cybersecurity risk. "
            "To get authorized for security work, join the Trusted Access for Cyber program."
        )

        notification = provider._translate(
            CodexMessage(method="error", params={"error": {"message": message}}),
            "turn-1",
        )
        completed = provider._translate(
            CodexMessage(
                method="turn/completed",
                params={
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "failed",
                        "error": {"message": message},
                    },
                },
            ),
            "turn-1",
        )

        assert notification is None
        assert completed is not None and not isinstance(completed, list)
        assert completed.kind == EVENT_COMPLETE
        assert completed.stop_reason == STOP_REASON_REFUSAL

    def test_other_codex_failures_keep_generic_error_handling(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path)
        event = provider._translate(
            CodexMessage(
                method="turn/completed",
                params={
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "failed",
                        "error": {"message": "app-server transport failed"},
                    },
                },
            ),
            "turn-1",
        )

        assert event is not None and not isinstance(event, list)
        assert event.stop_reason == "error: app-server transport failed"


class TestCodexModelCatalog:
    def test_catalog_and_effort_are_backend_advertised(self, tmp_path) -> None:
        provider = CodexProvider(work_dir=tmp_path, model="gpt-test")
        provider.client.served_model = "gpt-test"
        provider.client.models = [
            {
                "id": "gpt-test",
                "displayName": "GPT Test",
                "description": "Test model",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "high"},
                ],
            }
        ]
        assert provider.available_models() == [
            {"modelId": "gpt-test", "name": "GPT Test", "description": "Test model"}
        ]
        assert provider.get_valid_effort_levels() == ["low", "high"]


def test_managed_mcp_servers_are_process_local_codex_overrides(tmp_path) -> None:
    client = CodexAppServerClient(
        work_dir=tmp_path,
        mcp_servers={
            "kirocrew-core": {
                "command": "/opt/kiro crew/bin/kirocrew",
                "args": ["mcp-core"],
                "env": {"KIROCREW_HOME": "/tmp/crew home"},
            }
        },
    )

    assert client._mcp_config_overrides() == [
        "-c",
        'mcp_servers.kirocrew-core.command="/opt/kiro crew/bin/kirocrew"',
        "-c",
        'mcp_servers.kirocrew-core.args=["mcp-core"]',
        "-c",
        'mcp_servers.kirocrew-core.env.KIROCREW_HOME="/tmp/crew home"',
    ]


def test_default_registry_selects_codex_factory_without_changing_kiro_default() -> None:
    from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
    from kiro_crew.platform.defaults import DefaultProviderRegistry

    registry = DefaultProviderRegistry()
    kiro_cfg = KiroCrewConfig()
    assert registry.create_factory(kiro_cfg).__name__ == "_acp"

    codex_cfg = KiroCrewConfig(agent=AgentConfig(acp_backend="codex"))
    with patch(
        "kiro_crew.agent.build_agent_config",
        return_value={"mcpServers": {}},
    ):
        provider = registry.create_factory(codex_cfg)(session_key="dashboard:test")
    assert isinstance(provider, CodexProvider)


def test_dashboard_allows_harness_selection() -> None:
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    spec = _EDITABLE_CONFIG["agent.acp_backend"]
    assert spec["type"] == "enum"
    assert "values" not in spec
    assert "codex" in spec["values_fn"]()


def test_codex_is_not_warm_pool_eligible(tmp_path) -> None:
    provider = CodexProvider(work_dir=tmp_path)

    assert provider.is_warm_pool_eligible is False


@pytest.mark.asyncio
async def test_session_warm_pool_does_not_start_codex(tmp_path) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import SessionManager

    provider = CodexProvider(work_dir=tmp_path)
    provider.start = AsyncMock()
    manager = SessionManager(KiroCrewConfig(), provider_factory=lambda *_args, **_kwargs: provider)
    manager._pool_size = 1

    await manager._fill_warm_pool()

    assert manager._warm_pool.empty()
    provider.start.assert_not_awaited()
    await manager.close_all()


@pytest.mark.asyncio
async def test_dashboard_model_catalog_comes_from_codex_app_server() -> None:
    from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
    from kiro_crew.dashboard.handlers import agents

    cfg = KiroCrewConfig(agent=AgentConfig(acp_backend="codex"))
    request = MagicMock()
    request.app = {"state": SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: []))}
    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=cfg),
        patch(
            "kiro_crew.providers.codex.fetch_codex_models",
            AsyncMock(
                return_value=[{"model_name": "gpt-5.6-sol", "description": "Frontier coding model"}]
            ),
        ),
    ):
        response = await agents.api_models(request)

    assert response.status == 200
    payload = json.loads(response.body)
    assert [row["model_name"] for row in payload] == ["gpt-5.6-sol"]


@pytest.mark.asyncio
async def test_dashboard_skips_kiro_credit_scrape_for_codex() -> None:
    from kiro_crew.dashboard.handlers import sessions

    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(sessions=SimpleNamespace(uses_kiro_identity_store=lambda: False))
    }
    with patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch:
        response = await sessions.api_sessions_usage(request)

    assert response.status == 200
    assert json.loads(response.body) == {"usage": None}
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_dashboard_skips_kiro_readiness_probe_for_codex() -> None:
    from kiro_crew.dashboard import kiro_readiness

    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(
            sessions=SimpleNamespace(uses_kiro_identity_store=lambda _key=None: False)
        )
    }
    request.match_info = {}
    with patch.object(kiro_readiness, "kiro_verified_ready", AsyncMock()) as ready:
        response = await kiro_readiness.reject_if_kiro_unverified(request)

    assert response is None
    ready.assert_not_awaited()
