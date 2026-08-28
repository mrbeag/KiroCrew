"""AgentCore Gateway attach — URL-only rebuild + per-session inbound sidecar.

Public core never vends a token (``DefaultAgentIdentityProvider`` returns
``None``). A companion adapter supplies ``gateway_mcp_spec()`` and
``vend_gateway_inbound_token``. This module is the only writer of the inbound
sidecar and the only sanitizer that may put a Gateway spec on disk.

Workload posture emits a URL-only spec at rebuild (IAM inbound, no JWT).
The AWS extra rewrites that URL to a localhost SigV4 proxy; kiro-cli
never sees the unsigned Gateway hostname. Login posture leaves Gateway
out of ``~/.kiro/agents/kirocrew.json`` until ``attach_gateway_inbound``
writes a ``0600`` session sidecar; session/new reads that sidecar. A
companion JWT becomes an ``Authorization`` header. Without one, the
sidecar is URL-only so kiro-cli can run its MCP OAuth challenge
(``_kiro.dev/mcp/oauth_request``). Token bytes never enter the agent
file, SEL, logs, or ``status()``.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.platform.context import (
    async_safe_context_call,
    current_context,
    safe_context_call,
)
from kiro_crew.platform.governance import agentcore_posture
from kiro_crew.platform.governance_profiles import governance_permits
from kiro_crew.platform.interfaces import InboundToken, SessionPrincipal
from kiro_crew.security import allow_agentcore_consent_url
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Owning-module constants (code-style): Gateway MCP name + inbound dir.
GATEWAY_SERVER_NAME = "agentcore-gateway"
INBOUND_DIR_NAME = "agentcore-inbound"

# Unattended session-key prefixes (cron + TaskRunner). Login never attaches
# Gateway for these; workload user/OBO needs a vaulted owner token.
UNATTENDED_SESSION_PREFIXES = ("cron:", "taskrunner:", "taskrunner_", "task:")

# Companion ``status()`` keys. Display / policy only — never token material,
# and never written onto the sanitized Gateway spec (``_URL_ONLY_KEYS``).
STATUS_AUTHORIZATION_URL = "authorizationUrl"
STATUS_CREDENTIAL_KIND = "credentialKind"
STATUS_VAULTED_OWNER = "vaultedOwnerToken"
CREDENTIAL_KIND_M2M = "m2m"
CREDENTIAL_KIND_USER = "user"

# Inbound sidecar states. ``expired`` is the drain trigger: the file is
# gone and the live ACP child must be recycled so session/new cannot keep
# presenting a dead JWT.
SIDECAR_LIVE = "live"
SIDECAR_DENIED = "denied"
SIDECAR_EXPIRED = "expired"
SIDECAR_ABSENT = "absent"
REASON_EXPIRED = "expired"
REASON_OAUTH_CHALLENGE = "oauth_challenge"

# Spec keys that are bearer material or a place to hide it. Stripped before
# any write to ~/.kiro/agents/kirocrew.json.
_SECRET_SPEC_KEYS = frozenset({"headers", "authorization", "Authorization"})

# Remote-MCP keys safe to persist on the agent file (URL-only).
_URL_ONLY_KEYS = frozenset({"url", "type", "timeout", "disabledTools", "autoApprove"})


def strip_secret_spec_keys(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Copy *spec* without header / Authorization keys."""
    return {key: value for key, value in spec.items() if key not in _SECRET_SPEC_KEYS}


def sanitize_gateway_spec(spec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a URL-only remote MCP spec, or ``None`` when it is not one.

    Requires a non-empty string ``url``. Drops ``headers`` / ``Authorization``
    so a companion extra cannot persist a bearer into the agent file.
    """
    if not isinstance(spec, dict):
        return None
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    out: dict[str, Any] = {"url": url.strip()}
    for key in _URL_ONLY_KEYS:
        if key == "url" or key not in spec:
            continue
        out[key] = spec[key]
    return out


def is_unattended_session(session_key: str) -> bool:
    """True when *session_key* names cron / TaskRunner, not a human at a keyboard."""
    return bool(session_key) and session_key.startswith(UNATTENDED_SESSION_PREFIXES)


def _adapter_status() -> dict[str, Any]:
    raw = safe_context_call(
        lambda: current_context().agent_identity.status(),
        fallback={},
        log_message="agent_identity.status lookup failed; treating as empty",
    )
    return raw if isinstance(raw, dict) else {}


def _credential_kind() -> str:
    kind = str(_adapter_status().get(STATUS_CREDENTIAL_KIND) or "").strip().lower()
    if kind == CREDENTIAL_KIND_M2M:
        return CREDENTIAL_KIND_M2M
    return CREDENTIAL_KIND_USER


def _vaulted_owner_token() -> bool:
    return _adapter_status().get(STATUS_VAULTED_OWNER) is True


def _unattended_user_permitted() -> bool:
    """M2M may run unattended; user/OBO needs a still-valid vaulted owner token."""
    if _credential_kind() == CREDENTIAL_KIND_M2M:
        return True
    return _vaulted_owner_token()


def _consent_host_path(url: str) -> str:
    """Host+path only — never a query string (state / PKCE / code)."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if not host:
        return ""
    return f"{host}{path}"


def surface_consent_url(url: str | None) -> str | None:
    """Return *url* when it is allowlisted; SEL grant/deny. Never token bytes.

    An absent / empty URL is not a deny — there is nothing to surface.
    A present URL that fails the allowlist is refused (SEL denied).
    """
    if not isinstance(url, str) or not url.strip():
        return None
    stripped = url.strip()
    host_path = _consent_host_path(stripped)
    if allow_agentcore_consent_url(stripped):
        sel().log_api_access(
            caller="system",
            operation="agentcore.consent_url",
            outcome="ok",
            source="agentcore_gateway",
            resources=host_path,
        )
        return stripped
    sel().log_api_access(
        caller="system",
        operation="agentcore.consent_url",
        outcome="denied",
        source="agentcore_gateway",
        resources=host_path or "unknown-host",
    )
    return None


def pending_consent_url() -> str | None:
    """Allowlisted companion ``authorizationUrl``, or ``None``.

    Capability / adapter / posture must all be on (same conjunct as Gateway
    attach). The URL is never taken from a tool argument or the model.
    """
    snap = consent_snapshot()
    return snap["url"] if snap["pending"] else None


def consent_snapshot() -> dict[str, Any]:
    """Pending 3LO URL after the allowlist, or a refused/absent snapshot.

    ``refused`` is True only when the companion published a URL that failed
    the allowlist — the dashboard maps that to 403 ``consent_host_refused``.
    """
    if not _identity_on():
        return {"pending": False, "url": None, "refused": False}
    raw = _adapter_status().get(STATUS_AUTHORIZATION_URL)
    if not isinstance(raw, str) or not raw.strip():
        return {"pending": False, "url": None, "refused": False}
    allowed = surface_consent_url(raw)
    if allowed is None:
        return {"pending": False, "url": None, "refused": True}
    return {"pending": True, "url": allowed, "refused": False}


def inbound_sidecar_path(session_key: str) -> Path:
    """Owner-only sidecar path for one session's inbound token.

    The filename is a digest of *session_key* so ``:`` / ``/`` in a key cannot
    escape the inbound directory.
    """
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    return config_dir() / INBOUND_DIR_NAME / f"{digest}.json"


def _identity_on() -> bool:
    """Adapter on AND capability permitted AND known posture. Fail closed."""
    adapter_on = bool(
        safe_context_call(
            lambda: current_context().agent_identity.enabled(),
            fallback=False,
            log_message="agent_identity.enabled lookup failed; treating as disabled",
        )
    )
    if not adapter_on:
        return False
    permitted = bool(
        safe_context_call(
            lambda: getattr(
                governance_permits(
                    "capabilities.agentcore",
                    "",
                    fail_closed=True,
                    log_warning=False,
                ),
                "permitted",
                False,
            ),
            fallback=False,
            log_message="agentcore governance lookup failed; treating as disabled",
        )
    )
    if not permitted:
        return False
    return bool(
        safe_context_call(
            lambda: agentcore_posture(current_context().governance) is not None,
            fallback=False,
            log_message="agentcore posture lookup failed; treating as disabled",
        )
    )


def _current_posture() -> str | None:
    return safe_context_call(
        lambda: agentcore_posture(current_context().governance),
        fallback=None,
        log_message="agentcore posture lookup failed; no Gateway contribution",
    )


def _gateway_spec_from_adapter() -> dict[str, Any] | None:
    spec: dict[str, Any] | None = safe_context_call(
        lambda: current_context().agent_identity.gateway_mcp_spec(),
        fallback=None,
        log_message="gateway_mcp_spec lookup failed; no Gateway contribution",
    )
    if spec is not None:
        return sanitize_gateway_spec(spec)
    extras: dict[str, Any] = safe_context_call(
        lambda: current_context().mcp_tooling.extra_mcp_servers(),
        fallback={},
        log_message="extra_mcp_servers lookup failed; no Gateway fallback",
    )
    if not extras:
        return None
    return sanitize_gateway_spec(extras.get(GATEWAY_SERVER_NAME))


def rebuild_gateway_contribution() -> dict[str, dict[str, Any]]:
    """Gateway servers safe to write into the rebuilt agent file.

    Empty when identity is off, posture is not ``workload``, the companion
    spec is missing a URL, or a login-posture host can still IAM-invoke
    Gateway (mismatch — fail closed, SEL already recorded by the probe).
    The URL is whatever ``gateway_mcp_spec()`` returned after sanitizer
    strip — the AWS extra substitutes a ``127.0.0.1`` SigV4 proxy.
    """
    if not _identity_on():
        return {}
    if _current_posture() != "workload":
        return {}
    sanitized = _gateway_spec_from_adapter()
    if sanitized is None:
        return {}
    return {GATEWAY_SERVER_NAME: sanitized}


def clear_inbound_sidecar(session_key: str) -> None:
    """Remove this session's inbound sidecar if it exists."""
    path = inbound_sidecar_path(session_key)
    with contextlib.suppress(OSError):
        path.unlink()


def _write_owner_only_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write *payload* to *path* with owner-only mode before any secret byte."""
    platform_compat.make_owner_only_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=str(path.parent))
    wrote = False
    fd_owned = True
    try:
        platform_compat.fchmod_safe(fd, 0o600)
        if not platform_compat.IS_POSIX:
            platform_compat.restrict_to_owner(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd_owned = False
            json.dump(dict(payload), handle, sort_keys=True)
        os.replace(tmp, path)
        wrote = True
    finally:
        if fd_owned:
            with contextlib.suppress(OSError):
                os.close(fd)
        if not wrote:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def inbound_sidecar_state(session_key: str) -> str:
    """Classify this session's inbound sidecar without mutating it.

    ``expired`` means a file is present and ``expires_at`` is in the past.
    Callers that must drain a live ACP transport key off this, not off a
    later ``read_inbound_sidecar`` miss (that helper deletes the file).
    """
    path = inbound_sidecar_path(session_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SIDECAR_ABSENT
    except (OSError, ValueError):
        logger.warning("agentcore inbound sidecar unreadable; treating as absent")
        return SIDECAR_ABSENT
    if not isinstance(raw, dict):
        return SIDECAR_ABSENT
    if raw.get("denied") is True:
        return SIDECAR_DENIED
    expires_at = raw.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        return SIDECAR_EXPIRED
    return SIDECAR_LIVE


def read_inbound_sidecar(session_key: str) -> dict[str, Any] | None:
    """Load this session's inbound sidecar, or ``None`` if missing / expired."""
    state = inbound_sidecar_state(session_key)
    if state in (SIDECAR_ABSENT, SIDECAR_EXPIRED):
        if state == SIDECAR_EXPIRED:
            clear_inbound_sidecar(session_key)
        return None
    path = inbound_sidecar_path(session_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("agentcore inbound sidecar unreadable; treating as absent")
        return None
    return raw if isinstance(raw, dict) else None


async def drain_expired_gateway_transport(sessions: Any, session_key: str) -> bool:
    """Recycle a live ACP child whose inbound JWT has expired.

    Gateway is unpooled and injected on ``session/new``. An expired sidecar
    is not enough on its own: kiro-cli still holds the dead header until
    the child is gone. ``SessionManager.remove`` preserves the session map
    so the next turn cold-starts and ``session/load`` restores the
    conversation. Does not touch mcp_gateway pooled backends.

    Returns True when an expired sidecar was found and cleared.
    """
    if not session_key:
        return False
    if inbound_sidecar_state(session_key) != SIDECAR_EXPIRED:
        return False
    clear_inbound_sidecar(session_key)
    sel().log_api_access(
        caller="system",
        operation="agentcore.gateway_inbound",
        outcome="denied",
        source="agentcore_gateway",
        resources=f"session={session_key} reason={REASON_EXPIRED}",
    )
    remover = getattr(sessions, "remove", None)
    if callable(remover):
        try:
            result = remover(session_key)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug(
                "expired inbound drain could not recycle session %s",
                session_key,
                exc_info=True,
            )
    return True


def session_gateway_servers(session_key: str) -> list[dict[str, Any]]:
    """ACP ``mcpServers`` entries for this session's Gateway inbound, or ``[]``.

    Empty when there is no live sidecar (fail closed). Two sessions never
    share a sidecar path, so Gateway stays unpooled.
    """
    if not session_key:
        return []
    data = read_inbound_sidecar(session_key)
    if data is None:
        return []
    if data.get("denied") is True:
        # Session inject outranks the same-named agent-file entry (kiro-cli).
        # Workload user/OBO unattended retracts Gateway this way.
        return [{"name": GATEWAY_SERVER_NAME, "disabled": True}]
    url = data.get("url")
    headers = data.get("headers")
    if not isinstance(url, str) or not url:
        return []
    shaped: dict[str, Any] = {
        "name": str(data.get("name") or GATEWAY_SERVER_NAME),
        "url": url,
    }
    if isinstance(headers, dict) and headers:
        shaped["headers"] = [
            {"name": str(key), "value": str(value)} for key, value in headers.items()
        ]
    return [shaped]


def _authorization_value(token: InboundToken) -> str:
    scheme = (token.scheme or "bearer").strip() or "bearer"
    return f"{scheme} {token.token}".strip()


def _log_unattended_denied(principal: SessionPrincipal, *, reason: str) -> None:
    sel().log_api_access(
        caller="system",
        operation="agentcore.unattended_denied",
        outcome="denied",
        source="agentcore_gateway",
        resources=(
            f"session={principal.session_key} subject={principal.subject} " f"reason={reason}"
        ),
    )


def _write_unattended_deny_sidecar(principal: SessionPrincipal, *, reason: str) -> None:
    """Retract Gateway for this session without writing a token."""
    payload: dict[str, Any] = {
        "name": GATEWAY_SERVER_NAME,
        "denied": True,
        "reason": reason,
        "session_key": principal.session_key,
        "subject": principal.subject,
    }
    _write_owner_only_json(inbound_sidecar_path(principal.session_key), payload)
    _log_unattended_denied(principal, reason=reason)


async def attach_gateway_inbound(principal: SessionPrincipal) -> Path | None:
    """Attach login-posture Gateway for this session, or withhold it.

    Workload posture clears any leftover sidecar (IAM inbound, no JWT)
    unless this is an unattended user/OBO session without a vaulted owner
    token — then a deny sidecar retracts the agent-file Gateway.
    Login posture writes a ``0600`` sidecar when a URL-only spec exists:
    a vend'd JWT becomes the ``Authorization`` header; otherwise the
    sidecar is URL-only so kiro-cli can start its MCP OAuth challenge.
    Unattended login sessions never attach.
    """
    if not _identity_on():
        clear_inbound_sidecar(principal.session_key)
        return None
    posture = _current_posture()
    unattended = is_unattended_session(principal.session_key)
    if posture == "workload":
        if unattended and not _unattended_user_permitted():
            _write_unattended_deny_sidecar(principal, reason="user_without_vault")
            return None
        clear_inbound_sidecar(principal.session_key)
        return None
    if posture != "login":
        clear_inbound_sidecar(principal.session_key)
        return None
    if unattended:
        clear_inbound_sidecar(principal.session_key)
        _log_unattended_denied(principal, reason="login_unattended")
        return None

    from kiro_crew.cloud import iam as cloud_iam

    # Public probe defaults False: "no mismatch detected", not "IAM inbound
    # is impossible". A companion must override the live check.
    if cloud_iam.probe_instance_invoke_gateway():
        sel().log_api_access(
            caller="system",
            operation="agentcore.posture_mismatch",
            outcome="denied",
            source="agentcore_gateway",
            resources="InvokeGateway succeeded under login posture; inbound withheld",
        )
        clear_inbound_sidecar(principal.session_key)
        return None

    async def _vend() -> InboundToken | None:
        return await current_context().agent_identity.vend_gateway_inbound_token(principal)

    sanitized = _gateway_spec_from_adapter()
    if sanitized is None:
        sel().log_api_access(
            caller="system",
            operation="agentcore.gateway_inbound",
            outcome="denied",
            source="agentcore_gateway",
            resources=(
                f"session={principal.session_key} subject={principal.subject}; " "Gateway withheld"
            ),
        )
        clear_inbound_sidecar(principal.session_key)
        return None

    token = await async_safe_context_call(
        _vend,
        fallback=None,
        log_message="vend_gateway_inbound_token failed; attaching URL-only Gateway",
    )
    payload: dict[str, Any] = {
        "name": GATEWAY_SERVER_NAME,
        "url": sanitized["url"],
        "session_key": principal.session_key,
        "subject": principal.subject,
    }
    if token is not None:
        payload["headers"] = {"Authorization": _authorization_value(token)}
        payload["expires_at"] = token.expires_at
        payload["audience"] = token.audience
        reason = "bearer"
    else:
        # No companion JWT: kiro-cli presents the URL, Gateway returns 401
        # + WWW-Authenticate, and Crew already surfaces Authorize.
        payload["oauth_challenge"] = True
        reason = REASON_OAUTH_CHALLENGE
    path = inbound_sidecar_path(principal.session_key)
    _write_owner_only_json(path, payload)
    sel().log_api_access(
        caller="system",
        operation="agentcore.gateway_inbound",
        outcome="ok",
        source="agentcore_gateway",
        resources=(
            f"session={principal.session_key} subject={principal.subject} " f"reason={reason}"
        ),
    )
    logger.debug("agentcore inbound sidecar written for session %s", principal.session_key)
    return path
