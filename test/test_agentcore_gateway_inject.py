"""AgentCore Gateway URL-only rebuild + per-session inbound sidecar."""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.governance import parse_policy
from kiro_crew.platform.interfaces import InboundToken, SessionPrincipal

_GATEWAY_URL = "https://gateway.example.test/mcp"
_TOKEN = "sltok-test-not-for-logs"


@pytest.fixture(autouse=True)
def _clear_live_agentcore_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leftover E2E export must not rewrite login sidecars in this module."""
    monkeypatch.delenv("KIROCREW_AGENTCORE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_POSTURE", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_WORKLOAD_NAME", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_AWS", raising=False)
    monkeypatch.delenv("KIROCREW_AGENTCORE_PROXY_PORT", raising=False)


class _CompanionIdentity(DefaultAgentIdentityProvider):
    def __init__(
        self,
        *,
        spec: dict[str, Any] | None = None,
        token: InboundToken | None = None,
    ) -> None:
        self._spec = spec
        self._token = token

    def enabled(self) -> bool:
        return True

    def gateway_mcp_spec(self) -> dict[str, object] | None:
        return self._spec

    async def vend_gateway_inbound_token(self, principal: SessionPrincipal) -> InboundToken | None:
        return self._token


def _ceiling(*, posture: str, enabled: bool = True) -> Any:
    return parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": enabled, "posture": posture}},
        }
    )


def _install(
    *,
    posture: str | None,
    spec: dict[str, Any] | None = None,
    token: InboundToken | None = None,
    capability: bool = True,
    identity: Any | None = None,
) -> None:
    base = build_default_context(KiroCrewConfig())
    adapter = identity or _CompanionIdentity(spec=spec, token=token)
    if posture is None:
        set_context(dataclasses.replace(base, agent_identity=adapter, governance=None))
        return
    set_context(
        dataclasses.replace(
            base,
            agent_identity=adapter,
            governance=_ceiling(posture=posture, enabled=capability),
        )
    )


def _seed_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings_dir = tmp_path / ".kiro" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"dummy-kiro-global": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    from kiro_crew.config import config_dir

    (config_dir() / "mcp.json").write_text(
        json.dumps({"mcpServers": {"dummy-crew-store": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", sys.executable)
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [])
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: sys.executable)
    return kiro_dir


def _rebuilt_servers(kiro_dir: Path) -> dict[str, Any]:
    data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    assert isinstance(servers, dict)
    return servers


def _principal(session_key: str = "dashboard:1") -> SessionPrincipal:
    return SessionPrincipal(
        surface="dashboard",
        subject="dashboard+alice",
        session_key=session_key,
    )


def _live_token() -> InboundToken:
    return InboundToken(
        scheme="bearer",
        token=_TOKEN,
        expires_at=4_000_000_000.0,
        audience="gateway",
    )


@pytest.fixture(autouse=True)
def _reset_ctx() -> Any:
    yield
    reset_context()


def test_enabled_off_rebuild_has_no_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    _install(
        posture="workload",
        spec={"url": _GATEWAY_URL, "headers": {"Authorization": f"Bearer {_TOKEN}"}},
        identity=DefaultAgentIdentityProvider(),
    )
    rebuild_agent_config()
    servers = _rebuilt_servers(kiro_dir)
    assert "agentcore-gateway" not in servers
    assert _TOKEN not in json.dumps(servers)


def test_capability_off_even_if_companion_enabled_has_no_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    _install(
        posture="workload",
        spec={"url": _GATEWAY_URL},
        capability=False,
    )
    rebuild_agent_config()
    assert "agentcore-gateway" not in _rebuilt_servers(kiro_dir)


def test_login_vend_none_attaches_url_only_oauth_challenge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )
    from kiro_crew.sel import sel

    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    _install(posture="login", spec={"url": _GATEWAY_URL}, token=None)
    rebuild_agent_config()
    servers = _rebuilt_servers(kiro_dir)
    assert "agentcore-gateway" not in servers
    assert _TOKEN not in json.dumps(servers)

    async def _run() -> None:
        path = await attach_gateway_inbound(_principal())
        assert path == inbound_sidecar_path("dashboard:1")
        assert path is not None and path.exists()
        sidecar = json.loads(path.read_text(encoding="utf-8"))
        assert sidecar["url"] == _GATEWAY_URL
        assert sidecar["oauth_challenge"] is True
        assert "headers" not in sidecar
        injected = session_gateway_servers("dashboard:1")
        assert injected == [
            {
                "name": "agentcore-gateway",
                "type": "http",
                "url": _GATEWAY_URL,
                "headers": [],
            }
        ]

    import asyncio

    asyncio.run(_run())
    inbound = [
        e for e in sel().recent(limit=50) if e.get("operation") == "agentcore.gateway_inbound"
    ]
    assert inbound
    assert inbound[0].get("outcome") == "ok"
    assert "oauth_challenge" in str(inbound[0].get("resources") or "")
    assert _TOKEN not in json.dumps(inbound)


def test_login_attach_uses_https_url_not_workload_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env leftover workload must not put the SigV4 proxy on a login sidecar."""
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
    )

    _seed_rebuild(tmp_path, monkeypatch)
    _install(posture="login", spec={"url": "http://127.0.0.1:9/mcp"}, token=None)
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_aws.resolved_gateway_url",
        lambda: _GATEWAY_URL,
    )

    async def _run() -> None:
        path = await attach_gateway_inbound(_principal())
        assert path == inbound_sidecar_path("dashboard:1")
        sidecar = json.loads(path.read_text(encoding="utf-8"))
        assert sidecar["url"] == _GATEWAY_URL
        assert sidecar["oauth_challenge"] is True
        assert "127.0.0.1" not in sidecar["url"]

    import asyncio

    asyncio.run(_run())


def test_login_missing_spec_still_withholds_gateway() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    _install(posture="login", spec=None, token=None)

    async def _run() -> None:
        assert await attach_gateway_inbound(_principal()) is None
        assert session_gateway_servers("dashboard:1") == []
        assert inbound_sidecar_path("dashboard:1").exists() is False

    import asyncio

    asyncio.run(_run())


def test_workload_rebuild_writes_localhost_proxy_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated rebuild must persist the listen URL kiro-cli will call."""
    from kiro_crew.agent import rebuild_agent_config

    proxy = "http://127.0.0.1:64156/mcp"
    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    _install(posture="workload", spec={"url": proxy})
    rebuild_agent_config()
    spec = _rebuilt_servers(kiro_dir)["agentcore-gateway"]
    assert spec["url"] == proxy
    assert spec["url"].startswith("http://127.0.0.1:")
    assert "headers" not in spec


def test_workload_rebuild_is_url_only_no_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
    )

    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    _install(
        posture="workload",
        spec={"url": _GATEWAY_URL, "headers": {"Authorization": f"Bearer {_TOKEN}"}},
        token=_live_token(),
    )
    rebuild_agent_config()
    servers = _rebuilt_servers(kiro_dir)
    spec = servers["agentcore-gateway"]
    assert spec["url"] == _GATEWAY_URL
    assert "headers" not in spec
    assert "Authorization" not in spec
    dumped = json.dumps(servers)
    assert _TOKEN not in dumped
    assert "Bearer" not in dumped

    async def _run() -> None:
        assert await attach_gateway_inbound(_principal()) is None

    import asyncio

    asyncio.run(_run())
    assert inbound_sidecar_path("dashboard:1").exists() is False
    from kiro_crew.platform.agentcore_gateway import session_gateway_servers

    # Companion https spec is the unsigned hostname — never session-inject it.
    assert session_gateway_servers("dashboard:1") == []


def test_login_vend_writes_sidecar_not_agent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    _install(posture="login", spec={"url": _GATEWAY_URL}, token=_live_token())
    rebuild_agent_config()
    agent_text = (kiro_dir / "kirocrew.json").read_text(encoding="utf-8")
    assert "agentcore-gateway" not in json.dumps(_rebuilt_servers(kiro_dir))
    assert _TOKEN not in agent_text

    async def _run() -> Path | None:
        return await attach_gateway_inbound(_principal())

    import asyncio

    path = asyncio.run(_run())
    assert path == inbound_sidecar_path("dashboard:1")
    assert path is not None and path.exists()
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert sidecar["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert sidecar["url"] == _GATEWAY_URL
    injected = session_gateway_servers("dashboard:1")
    assert injected[0]["url"] == _GATEWAY_URL
    assert injected[0]["type"] == "http"
    assert injected[0]["headers"][0]["value"] == f"Bearer {_TOKEN}"
    assert _TOKEN not in (kiro_dir / "kirocrew.json").read_text(encoding="utf-8")


def test_two_sessions_do_not_share_a_sidecar() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    token_a = InboundToken(scheme="bearer", token="tok-a", expires_at=4_000_000_000.0, audience="g")
    token_b = InboundToken(scheme="bearer", token="tok-b", expires_at=4_000_000_000.0, audience="g")
    _install(posture="login", spec={"url": _GATEWAY_URL}, token=token_a)

    async def _run() -> None:
        await attach_gateway_inbound(_principal("dashboard:a"))
        _install(posture="login", spec={"url": _GATEWAY_URL}, token=token_b)
        await attach_gateway_inbound(_principal("dashboard:b"))

    import asyncio

    asyncio.run(_run())
    path_a = inbound_sidecar_path("dashboard:a")
    path_b = inbound_sidecar_path("dashboard:b")
    assert path_a != path_b
    assert path_a.exists() and path_b.exists()
    servers_a = session_gateway_servers("dashboard:a")
    servers_b = session_gateway_servers("dashboard:b")
    assert servers_a[0]["headers"][0]["value"] == "Bearer tok-a"
    assert servers_b[0]["headers"][0]["value"] == "Bearer tok-b"


def test_token_never_appears_in_logs_or_sel(caplog: pytest.LogCaptureFixture) -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound
    from kiro_crew.sel import sel

    _install(posture="login", spec={"url": _GATEWAY_URL}, token=_live_token())
    caplog.set_level(logging.DEBUG)

    async def _run() -> None:
        await attach_gateway_inbound(_principal())

    import asyncio

    asyncio.run(_run())
    assert _TOKEN not in caplog.text
    events = sel().recent(limit=50)
    inbound = [e for e in events if e.get("operation") == "agentcore.gateway_inbound"]
    assert inbound, f"expected inbound SEL row in {events!r}"
    blob = json.dumps(inbound)
    assert _TOKEN not in blob
    assert inbound[0].get("outcome") == "ok"


def test_acp_http_server_always_has_type_and_headers() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        ACP_DENIED_PLACEHOLDER_URL,
        ACP_HTTP_TYPE,
        acp_http_server,
    )

    live = acp_http_server("http://127.0.0.1:18765/mcp")
    assert live["type"] == ACP_HTTP_TYPE
    assert live["headers"] == []
    bearer = acp_http_server(_GATEWAY_URL, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert bearer["headers"] == [{"name": "Authorization", "value": f"Bearer {_TOKEN}"}]
    denied = acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)
    assert denied["disabled"] is True
    assert denied["headers"] == []


def test_is_loopback_listen_url() -> None:
    from kiro_crew.platform.agentcore_gateway import is_loopback_listen_url

    assert is_loopback_listen_url("http://127.0.0.1:18765/mcp") is True
    assert is_loopback_listen_url("http://localhost:9/mcp") is True
    assert is_loopback_listen_url("http://[::1]:9/mcp") is True
    assert is_loopback_listen_url(_GATEWAY_URL) is False
    assert is_loopback_listen_url("https://127.0.0.1:18765/mcp") is False
    assert is_loopback_listen_url("http://example.test/mcp") is False
    assert is_loopback_listen_url("") is False


def test_workload_session_injects_live_loopback_proxy() -> None:
    """session/new must outrank a stale agent-file port after restart."""
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        session_gateway_servers,
    )

    listen = "http://127.0.0.1:18765/mcp"
    _install(posture="workload", spec={"url": listen})

    async def _run() -> None:
        assert await attach_gateway_inbound(_principal()) is None

    import asyncio

    asyncio.run(_run())
    from kiro_crew.platform.agentcore_gateway import acp_http_server

    assert session_gateway_servers("dashboard:1") == [acp_http_server(listen)]


def test_workload_session_never_injects_unsigned_https() -> None:
    from kiro_crew.platform.agentcore_gateway import session_gateway_servers

    _install(posture="workload", spec={"url": _GATEWAY_URL})
    assert session_gateway_servers("dashboard:1") == []


def test_login_without_sidecar_does_not_inject_loopback() -> None:
    from kiro_crew.platform.agentcore_gateway import session_gateway_servers

    _install(posture="login", spec={"url": "http://127.0.0.1:18765/mcp"})
    assert session_gateway_servers("dashboard:1") == []


def test_identity_off_does_not_inject_loopback() -> None:
    from kiro_crew.platform.agentcore_gateway import session_gateway_servers

    _install(
        posture="workload",
        spec={"url": "http://127.0.0.1:18765/mcp"},
        identity=DefaultAgentIdentityProvider(),
    )
    assert session_gateway_servers("dashboard:1") == []


def test_companion_extra_headers_stripped_on_workload_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    leak = {"url": _GATEWAY_URL, "headers": {"Authorization": f"Bearer {_TOKEN}"}}
    monkeypatch.setattr(
        "kiro_crew.agent._extra_mcp_servers",
        lambda: {"other-remote": dict(leak)},
    )
    _install(posture="workload", spec=dict(leak))
    rebuild_agent_config()
    servers = _rebuilt_servers(kiro_dir)
    dumped = json.dumps(servers)
    assert _TOKEN not in dumped
    assert "headers" not in servers["agentcore-gateway"]
    assert "headers" not in servers["other-remote"]
    assert servers["other-remote"]["url"] == _GATEWAY_URL


def test_login_rebuild_drops_gateway_even_from_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.agent._extra_mcp_servers",
        lambda: {
            "agentcore-gateway": {
                "url": _GATEWAY_URL,
                "headers": {"Authorization": f"Bearer {_TOKEN}"},
            }
        },
    )
    _install(posture="login", spec={"url": _GATEWAY_URL})
    rebuild_agent_config()
    servers = _rebuilt_servers(kiro_dir)
    assert "agentcore-gateway" not in servers
    assert _TOKEN not in json.dumps(servers)


@pytest.mark.asyncio
async def test_bind_session_principal_attaches_login_sidecar() -> None:
    from kiro_crew.platform.agent_identity import bind_session_principal
    from kiro_crew.platform.agentcore_gateway import inbound_sidecar_path

    _install(posture="login", spec={"url": _GATEWAY_URL}, token=_live_token())

    class _Sessions:
        def __init__(self) -> None:
            self.principals: dict[str, SessionPrincipal] = {}

        def set_principal(self, key: str, principal: SessionPrincipal) -> None:
            self.principals[key] = principal

    sessions = _Sessions()
    await bind_session_principal(
        sessions, surface="dashboard", raw_id="alice", session_key="dashboard:bind"
    )
    assert inbound_sidecar_path("dashboard:bind").exists()


class _RecordingSessions:
    def __init__(self) -> None:
        self.principals: dict[str, SessionPrincipal] = {}
        self.removed: list[str] = []

    def set_principal(self, key: str, principal: SessionPrincipal) -> None:
        self.principals[key] = principal

    async def remove(self, key: str) -> None:
        self.removed.append(key)


@pytest.mark.asyncio
async def test_expired_sidecar_recycles_live_session() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        drain_expired_gateway_transport,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    expired = InboundToken(scheme="bearer", token=_TOKEN, expires_at=1.0, audience="gateway")
    _install(posture="login", spec={"url": _GATEWAY_URL}, token=expired)
    await attach_gateway_inbound(_principal("dashboard:exp"))
    assert inbound_sidecar_path("dashboard:exp").exists()

    sessions = _RecordingSessions()
    drained = await drain_expired_gateway_transport(sessions, "dashboard:exp")
    assert drained is True
    assert sessions.removed == ["dashboard:exp"]
    assert inbound_sidecar_path("dashboard:exp").exists() is False
    assert session_gateway_servers("dashboard:exp") == []


@pytest.mark.asyncio
async def test_live_sidecar_does_not_recycle_session() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        drain_expired_gateway_transport,
    )

    _install(posture="login", spec={"url": _GATEWAY_URL}, token=_live_token())
    await attach_gateway_inbound(_principal("dashboard:live"))
    sessions = _RecordingSessions()
    drained = await drain_expired_gateway_transport(sessions, "dashboard:live")
    assert drained is False
    assert sessions.removed == []


@pytest.mark.asyncio
async def test_absent_sidecar_does_not_recycle_session() -> None:
    from kiro_crew.platform.agentcore_gateway import drain_expired_gateway_transport

    _install(posture="login", spec={"url": _GATEWAY_URL}, token=_live_token())
    sessions = _RecordingSessions()
    drained = await drain_expired_gateway_transport(sessions, "dashboard:missing")
    assert drained is False
    assert sessions.removed == []


@pytest.mark.asyncio
async def test_bind_recycles_then_writes_fresh_sidecar() -> None:
    from kiro_crew.platform.agent_identity import bind_session_principal
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    expired = InboundToken(scheme="bearer", token=_TOKEN, expires_at=1.0, audience="gateway")
    _install(posture="login", spec={"url": _GATEWAY_URL}, token=expired)
    await attach_gateway_inbound(_principal("dashboard:rebind"))
    _install(posture="login", spec={"url": _GATEWAY_URL}, token=_live_token())
    sessions = _RecordingSessions()
    await bind_session_principal(
        sessions, surface="dashboard", raw_id="alice", session_key="dashboard:rebind"
    )
    assert sessions.removed == ["dashboard:rebind"]
    path = inbound_sidecar_path("dashboard:rebind")
    assert path.exists()
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert sidecar["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert sidecar["expires_at"] == 4_000_000_000.0
    injected = session_gateway_servers("dashboard:rebind")
    assert injected[0]["headers"][0]["value"] == f"Bearer {_TOKEN}"


def test_expired_drain_sel_has_no_token(caplog: pytest.LogCaptureFixture) -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        drain_expired_gateway_transport,
    )
    from kiro_crew.sel import sel

    expired = InboundToken(scheme="bearer", token=_TOKEN, expires_at=1.0, audience="gateway")
    _install(posture="login", spec={"url": _GATEWAY_URL}, token=expired)
    caplog.set_level(logging.DEBUG)

    async def _run() -> None:
        await attach_gateway_inbound(_principal("dashboard:sel"))
        await drain_expired_gateway_transport(_RecordingSessions(), "dashboard:sel")

    import asyncio

    asyncio.run(_run())
    assert _TOKEN not in caplog.text
    events = sel().recent(limit=50)
    blob = json.dumps(events)
    assert _TOKEN not in blob
    expired_rows = [
        e
        for e in events
        if e.get("operation") == "agentcore.gateway_inbound"
        and "reason=expired" in str(e.get("resources") or "")
    ]
    assert expired_rows, f"expected expired inbound SEL row in {events!r}"
    assert expired_rows[0].get("outcome") == "denied"


@pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
def test_inbound_dir_is_keystone_under_every_home_prefix(prefix: str) -> None:
    from kiro_crew.platform.agentcore_gateway import INBOUND_DIR_NAME
    from kiro_crew.security import (
        _CREW_SECRET_LEAVES,
        is_sensitive_bash_command,
        is_sensitive_path,
        is_sensitive_write_path,
    )

    assert INBOUND_DIR_NAME in _CREW_SECRET_LEAVES
    directory = f"~/{prefix}/{INBOUND_DIR_NAME}"
    sidecar = f"{directory}/deadbeef.json"
    assert is_sensitive_path(directory) is True
    assert is_sensitive_path(sidecar) is True
    assert is_sensitive_write_path(directory) is True
    assert is_sensitive_write_path(sidecar) is True
    for cmd in (
        f"cat {directory}",
        f"cat {sidecar}",
        f"echo x > {sidecar}",
        f"tee {sidecar}",
        f"cp evil {sidecar}",
        f"tar -xf evil.tar -C {directory}",
        f"unzip -d {directory} evil.zip",
    ):
        assert is_sensitive_bash_command(cmd) is not None, cmd
