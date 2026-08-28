"""AgentCore instance Policy.json postures, successor boundary, login withhold."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.cloud import iam
from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.governance import parse_policy

_WORKLOAD_DIR = "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default"
_WORKLOAD_ID = (
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew"
)
_WORKLOAD_ID_WILDCARD = (
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew-*"
)
_WORKLOAD_RESOURCES = [_WORKLOAD_DIR, _WORKLOAD_ID, _WORKLOAD_ID_WILDCARD]
_GATEWAY = "arn:aws:bedrock-agentcore:*:*:gateway/kirocrew-*"

# Byte-stable original boundary: SSM-core + source-bucket read, no AgentCore.
_ORIGINAL_BOUNDARY_SIDS = frozenset({"SsmCore", "SourceBucketRead"})


class _ForcedOnIdentity(DefaultAgentIdentityProvider):
    def enabled(self) -> bool:
        return True


def _agentcore_ceiling(*, posture: str) -> Any:
    return parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": True, "posture": posture}},
        }
    )


def _enable_agentcore(posture: str) -> None:
    base = build_default_context(KiroCrewConfig())
    set_context(
        dataclasses.replace(
            base,
            agent_identity=_ForcedOnIdentity(),
            governance=_agentcore_ceiling(posture=posture),
        )
    )


def _enable_agentcore_policy_only(posture: str) -> None:
    """Capability + posture on the ceiling; Default identity stays off."""
    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, governance=_agentcore_ceiling(posture=posture)))


def _statement_by_sid(doc: dict[str, Any], sid: str) -> dict[str, Any]:
    return next(s for s in doc["Statement"] if s["Sid"] == sid)


def _actions(st: dict[str, Any]) -> set[str]:
    raw = st["Action"]
    return {raw} if isinstance(raw, str) else set(raw)


def _resources(st: dict[str, Any]) -> list[str]:
    raw = st["Resource"]
    return [raw] if isinstance(raw, str) else list(raw)


def _seed_rebuild_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate kiro-cli home + agent dir; seed dummy servers. Never writes ~/.kiro."""
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings_dir = tmp_path / ".kiro" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"dummy-kiro-global": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    seam_global = tmp_path / "seam-global.json"
    seam_global.write_text(
        json.dumps({"mcpServers": {"dummy-seam-global": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    from kiro_crew.config import config_dir

    crew_mcp = config_dir() / "mcp.json"
    crew_mcp.write_text(
        json.dumps({"mcpServers": {"dummy-crew-store": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    leftover = kiro_dir / "kirocrew.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {"dummy-leftover": {"command": "dummy-srv"}},
                "tools": ["@dummy-leftover"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", sys.executable)
    monkeypatch.setattr(
        "kiro_crew.agent._extra_mcp_scope_globals",
        lambda: [seam_global],
    )
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: sys.executable)
    return kiro_dir


def test_launcher_policy_json_has_no_invoke_gateway() -> None:
    text = iam.policy_json()
    assert "InvokeGateway" not in text
    assert "GetWorkloadAccessToken" not in text
    assert "GetGateway" not in text
    assert "ListGatewayTargets" not in text
    assert "SynchronizeGatewayTargets" not in text


def test_launcher_policy_can_create_agentcore_identity() -> None:
    st = _statement_by_sid(iam.policy_document(), "AgentCoreWorkloadIdentityControlPlane")
    assert st["Effect"] == "Allow"
    assert "bedrock-agentcore:CreateWorkloadIdentity" in _actions(st)
    assert "bedrock-agentcore:DeleteWorkloadIdentity" in _actions(st)
    assert "InvokeGateway" not in "".join(_actions(st))
    assert "GetWorkloadAccessToken" not in "".join(_actions(st))
    assert _resources(st) == _WORKLOAD_RESOURCES


def test_agentcore_workload_name_is_per_tag() -> None:
    assert iam.agentcore_workload_name("kc-abc123", "workload") == "kirocrew-kc-abc123"
    assert iam.agentcore_workload_name("kc-abc123", "login") == "kirocrew-kc-abc123"
    assert iam.agentcore_workload_name("kc-abc123", "none") == ""
    assert iam.normalize_agentcore_posture("") == "none"
    assert iam.normalize_agentcore_posture("WORKLOAD") == "workload"


def test_launcher_create_role_accepts_either_boundary() -> None:
    st = _statement_by_sid(iam.policy_document(), "IamCreateRoleWithBoundary")
    cond = st["Condition"]["ArnLike"]["iam:PermissionsBoundary"]
    values = [cond] if isinstance(cond, str) else list(cond)
    assert f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}" in values
    assert f"arn:aws:iam::*:policy/{iam.AGENTCORE_BOUNDARY_NAME}" in values


def test_launcher_create_once_covers_successor_name() -> None:
    st = _statement_by_sid(iam.policy_document(), "IamInstanceBoundaryCreateOnce")
    assert set(st["Action"]) == {
        "iam:CreatePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
    }
    resources = _resources(st)
    assert f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}" in resources
    assert f"arn:aws:iam::*:policy/{iam.AGENTCORE_BOUNDARY_NAME}" in resources
    assert not any(r.endswith("*") for r in resources)


def test_workload_instance_document_denies_for_jwt() -> None:
    doc = iam.agentcore_instance_policy_document("workload")
    identity = _statement_by_sid(doc, "AgentCoreIdentity")
    assert identity["Effect"] == "Allow"
    assert _actions(identity) == {
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    }
    assert _resources(identity) == _WORKLOAD_RESOURCES

    gateway = _statement_by_sid(doc, "AgentCoreGateway")
    assert gateway["Effect"] == "Allow"
    assert _actions(gateway) == {"bedrock-agentcore:InvokeGateway"}
    assert _resources(gateway) == [_GATEWAY]

    deny = _statement_by_sid(doc, "DenyJwtPathOnWorkloadPosture")
    assert deny["Effect"] == "Deny"
    assert _actions(deny) == {"bedrock-agentcore:GetWorkloadAccessTokenForJWT"}
    assert _resources(deny) == ["*"]

    inspect = _statement_by_sid(doc, "AgentCoreGatewayInspect")
    assert inspect["Effect"] == "Allow"
    assert _actions(inspect) == {
        "bedrock-agentcore:GetGateway",
        "bedrock-agentcore:ListGatewayTargets",
        "bedrock-agentcore:GetGatewayTarget",
        "bedrock-agentcore:SynchronizeGatewayTargets",
    }
    assert _resources(inspect) == ["arn:aws:bedrock-agentcore:*:*:gateway/*"]

    for st in doc["Statement"]:
        if st["Effect"] == "Allow":
            assert "*" not in _resources(st)


def test_login_instance_document_denies_userid_and_invoke() -> None:
    doc = iam.agentcore_instance_policy_document("login")
    identity = _statement_by_sid(doc, "AgentCoreIdentityForJwt")
    assert identity["Effect"] == "Allow"
    assert _actions(identity) == {"bedrock-agentcore:GetWorkloadAccessTokenForJWT"}
    assert _resources(identity) == _WORKLOAD_RESOURCES

    deny = _statement_by_sid(doc, "DenyUserIdAndIamGateway")
    assert deny["Effect"] == "Deny"
    assert _actions(deny) == {
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:InvokeGateway",
    }
    assert _resources(deny) == ["*"]

    inspect = _statement_by_sid(doc, "AgentCoreGatewayInspect")
    assert inspect["Effect"] == "Allow"
    assert "bedrock-agentcore:GetGateway" in _actions(inspect)
    assert _resources(inspect) == ["arn:aws:bedrock-agentcore:*:*:gateway/*"]

    dumped = json.dumps(doc)
    assert "GetWorkloadAccessTokenForUserId" in dumped
    assert '"Effect": "Allow"' not in dumped.split("GetWorkloadAccessTokenForUserId")[0][-80:]
    for st in doc["Statement"]:
        if st["Effect"] == "Allow":
            assert "InvokeGateway" not in _actions(st)
            assert "GetWorkloadAccessTokenForUserId" not in _actions(st)
            assert "*" not in _resources(st)


def test_original_boundary_document_unchanged() -> None:
    doc = iam.boundary_policy_document("123456789012")
    dumped = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    assert "bedrock-agentcore" not in dumped
    assert "InvokeGateway" not in dumped
    assert "GetWorkloadAccessToken" not in dumped
    sids = {s["Sid"] for s in doc["Statement"]}
    assert sids == _ORIGINAL_BOUNDARY_SIDS
    ssm = _statement_by_sid(doc, "SsmCore")
    assert "ssm:UpdateInstanceInformation" in ssm["Action"]
    s3 = _statement_by_sid(doc, "SourceBucketRead")
    assert s3["Action"] == ["s3:GetObject"]
    assert s3["Resource"] == "arn:aws:s3:::kirocrew-src-123456789012-*/*"
    assert json.loads(iam.boundary_policy_json("123456789012")) == doc


def test_successor_boundary_is_union_ceiling() -> None:
    for posture in ("workload", "login"):
        doc = iam.agentcore_boundary_policy_document("123456789012", posture)
        sids = {s["Sid"] for s in doc["Statement"]}
        assert "SsmCore" in sids
        assert "SourceBucketRead" in sids
        dumped = json.dumps(doc)
        for action in (
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:InvokeGateway",
            "bedrock-agentcore:GetGateway",
            "bedrock-agentcore:ListGatewayTargets",
            "bedrock-agentcore:SynchronizeGatewayTargets",
        ):
            assert action in dumped
        inspect = _statement_by_sid(doc, "AgentCoreInspectCeiling")
        assert _resources(inspect) == ["arn:aws:bedrock-agentcore:*:*:gateway/*"]
        s3 = _statement_by_sid(doc, "SourceBucketRead")
        assert s3["Resource"] == "arn:aws:s3:::kirocrew-src-123456789012-*/*"


def test_successor_boundary_name_is_distinct() -> None:
    assert iam.AGENTCORE_BOUNDARY_NAME == "kirocrew-ec2-boundary-agentcore"
    assert iam.BOUNDARY_NAME == "kirocrew-ec2-boundary"
    assert iam.AGENTCORE_BOUNDARY_NAME != iam.BOUNDARY_NAME


def test_template_allowed_pattern_lists_both_boundary_names() -> None:
    from kiro_crew.cloud import ec2

    text = ec2.load_template()
    assert "kirocrew-ec2-boundary" in text
    assert "kirocrew-ec2-boundary-agentcore" in text
    assert "kirocrew-ec2-boundary(-agentcore)?" in text or (
        "kirocrew-ec2-boundary" in text and "agentcore" in text
    )


def test_template_instance_policies_include_inspect() -> None:
    from kiro_crew.cloud import ec2

    text = ec2.load_template()
    assert text.count("AgentCoreGatewayInspect") >= 2
    assert "bedrock-agentcore:GetGateway" in text
    assert "bedrock-agentcore:SynchronizeGatewayTargets" in text
    assert "gateway/*" in text


def test_login_rebuild_withholds_kiro_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable_agentcore("login")
        rebuild_agent_config()
    finally:
        reset_context()

    data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    assert "kirocrew-core" in servers
    assert "kirocrew-cron" in servers
    assert "dummy-kiro-global" not in servers
    assert "dummy-seam-global" not in servers
    assert "dummy-crew-store" not in servers
    assert "dummy-leftover" not in servers
    assert not any("gateway" in name.lower() for name in servers)


def test_login_rebuild_withholds_app_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.agent._collect_app_mcp_servers",
        lambda: {"dummyapp:srv": {"command": "dummy-srv"}},
    )
    try:
        _enable_agentcore("login")
        rebuild_agent_config()
    finally:
        reset_context()

    data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    assert "dummyapp:srv" not in servers
    assert "kirocrew-core" in servers
    assert "kirocrew-cron" in servers
    assert not any("gateway" in name.lower() for name in servers)


def test_login_rebuild_drops_ondisk_app_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior rebuild's ``{app}:{server}`` must not survive login withhold.

    The live merge path (``is_kirocrew_json``) re-reads on-disk kirocrew.json
    under the bridges lock and assigns every app-namespaced key back. Both
    agent-dir overrides must agree or that path stays closed.
    """
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "kirocrew.json"
    data = json.loads(leftover.read_text(encoding="utf-8"))
    data["mcpServers"]["dummyapp:srv"] = {"command": "dummy-srv"}
    leftover.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.bridges.KIRO_AGENTS_DIR", kiro_dir)
    try:
        _enable_agentcore("login")
        rebuild_agent_config()
    finally:
        reset_context()

    servers = json.loads(leftover.read_text(encoding="utf-8")).get("mcpServers") or {}
    assert "dummyapp:srv" not in servers
    assert "kirocrew-core" in servers
    assert "kirocrew-cron" in servers
    assert not any("gateway" in name.lower() for name in servers)


def test_login_rebuild_withholds_without_companion_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.platform.defaults import DefaultAgentIdentityProvider

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable_agentcore_policy_only("login")
        assert not DefaultAgentIdentityProvider().enabled()
        rebuild_agent_config()
    finally:
        reset_context()

    data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    assert "dummy-kiro-global" not in servers
    assert "dummy-seam-global" not in servers
    assert "dummy-crew-store" not in servers
    assert "dummy-leftover" not in servers
    assert "kirocrew-core" in servers
    assert "kirocrew-cron" in servers


def test_workload_rebuild_still_merges_kiro_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable_agentcore("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    assert "dummy-kiro-global" in servers
    assert "kirocrew-core" in servers


def test_login_probe_succeeds_iam_invoke_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.sel import sel

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(iam, "probe_instance_invoke_gateway", lambda: True)
    try:
        _enable_agentcore("login")
        rebuild_agent_config()
        events = sel().recent(limit=50)
    finally:
        reset_context()

    data = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    mismatch = [e for e in events if e.get("operation") == "agentcore.posture_mismatch"]
    assert mismatch, f"expected SEL agentcore.posture_mismatch row in {events!r}"
    assert mismatch[0].get("outcome") == "denied"
    assert not any("gateway" in name.lower() for name in servers)


@pytest.mark.asyncio
async def test_iam_policy_api_returns_labeled_instance_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.cloud import launch_job as lj
    from kiro_crew.dashboard import handlers_cloud as hc

    monkeypatch.setattr(hc.sys, "platform", "linux")
    state = SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_store=lj.LaunchJobStore(root=tmp_path / "launch-jobs"),
    )
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "GET",
        "/api/cloud/iam-policy?instance=1&posture=workload",
        app=app,
    )
    req["user"] = "owner-1"
    req["app"] = ""
    resp = await hc.api_cloud_iam_policy(req)
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert "policy" in body
    assert "InvokeGateway" not in body["policy"]
    instance = json.loads(body["instance_policy"])
    assert body["instance_posture"] == "workload"
    assert any("InvokeGateway" in json.dumps(s) for s in instance["Statement"])


def test_cli_iam_policy_instance_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from kiro_crew import cli_cloud

    ns = type("NS", (), {"cloud_action": "iam-policy", "instance": True, "posture": "login"})()
    assert cli_cloud.handle_cloud(ns) == 0
    out = capsys.readouterr().out
    assert "GetWorkloadAccessTokenForJWT" in out
    assert "DenyUserIdAndIamGateway" in out


def test_cli_iam_policy_instance_requires_posture(capsys: pytest.CaptureFixture[str]) -> None:
    """``--instance`` without ``--posture`` must not emit the privileged sibling."""
    from kiro_crew import cli_cloud

    ns = type("NS", (), {"cloud_action": "iam-policy", "instance": True, "posture": None})()
    assert cli_cloud.handle_cloud(ns) != 0
    captured = capsys.readouterr()
    assert "InvokeGateway" not in captured.out
    assert "InvokeGateway" not in captured.err
