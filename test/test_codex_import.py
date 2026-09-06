"""Native Codex thread import contracts."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
from kiro_crew.dashboard import codex_import
from kiro_crew.providers.codex import metadata


def test_codex_thread_messages_projects_only_visible_conversation() -> None:
    thread = {
        "turns": [
            {
                "startedAt": 1_700_000_000,
                "completedAt": 1_700_000_005,
                "items": [
                    {
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "localImage", "path": "/tmp/image.png"},
                        ],
                    },
                    {"type": "reasoning", "summary": ["private chain"]},
                    {"type": "agentMessage", "text": "hi there"},
                    {"type": "commandExecution", "command": "pwd"},
                ],
            }
        ]
    }

    rows = codex_import.codex_thread_messages(thread)

    assert [(role, content) for role, content, _ts in rows] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]
    assert rows[0][2] < rows[1][2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims", [{"user": "visitor", "app": ""}, {"user": "owner", "app": "plugin"}]
)
async def test_native_history_requires_owner_before_loading_config(claims):
    request = MagicMock()
    request.get.side_effect = lambda key, default=None: claims.get(key, default)
    request.__contains__.side_effect = lambda key: key in claims
    request.__getitem__.side_effect = lambda key: claims[key]
    request.app = {"state": SimpleNamespace(owner_id="owner")}
    with patch.object(codex_import.KiroCrewConfig, "load") as load:
        response = await codex_import.api_codex_threads(request)
    assert response.status == 403
    load.assert_not_called()


@pytest.mark.asyncio
async def test_codex_threads_uses_metadata_only_app_server_request() -> None:
    client = SimpleNamespace(
        request=AsyncMock(return_value={"data": []}),
        shutdown=AsyncMock(),
    )
    with patch.object(metadata, "_temporary_client", AsyncMock(return_value=client)):
        result = await metadata.codex_threads(
            sandbox_mode="auto",
            limit=500,
            search_term="dashboard",
        )

    assert result == {"data": []}
    client.request.assert_awaited_once_with(
        "thread/list",
        {
            "limit": 25,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "searchTerm": "dashboard",
        },
    )
    client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_codex_threads_paginates_with_bounded_frames() -> None:
    client = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {
                    "data": [{"id": f"thread-{index}"} for index in range(25)],
                    "nextCursor": "page-2",
                },
                {
                    "data": [{"id": f"thread-{index}"} for index in range(25, 50)],
                    "nextCursor": "page-3",
                },
                {
                    "data": [{"id": f"thread-{index}"} for index in range(50, 60)],
                    "nextCursor": "page-4",
                },
            ]
        ),
        shutdown=AsyncMock(),
    )
    with patch.object(metadata, "_temporary_client", AsyncMock(return_value=client)):
        result = await metadata.codex_threads(
            sandbox_mode="auto",
            limit=60,
            search_term="dashboard",
        )

    assert [row["id"] for row in result["data"]] == [f"thread-{index}" for index in range(60)]
    assert result["nextCursor"] == "page-4"
    assert client.request.await_args_list == [
        call(
            "thread/list",
            {
                "limit": 25,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "searchTerm": "dashboard",
            },
        ),
        call(
            "thread/list",
            {
                "limit": 25,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "searchTerm": "dashboard",
                "cursor": "page-2",
            },
        ),
        call(
            "thread/list",
            {
                "limit": 10,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "searchTerm": "dashboard",
                "cursor": "page-3",
            },
        ),
    ]
    client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "method", "params", "request_kwargs"),
    [
        (
            "fork",
            "thread/fork",
            {
                "threadId": "019abcdef",
                "excludeTurns": True,
                "deferGoalContinuation": True,
                "threadSource": "codexcrew-import",
            },
            {"timeout": metadata._IMPORT_FORK_TIMEOUT_SECONDS},
        ),
        (
            "resume",
            "thread/read",
            {"threadId": "019abcdef", "includeTurns": False},
            {},
        ),
    ],
)
async def test_codex_import_uses_native_thread_operations(
    mode, method, params, request_kwargs
) -> None:
    client = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {"thread": {"id": "019result"}},
                {
                    "data": [
                        {"id": "newest", "items": []},
                        {"id": "oldest", "items": []},
                    ],
                    "nextCursor": None,
                },
            ]
        ),
        shutdown=AsyncMock(),
    )
    with patch.object(metadata, "_temporary_client", AsyncMock(return_value=client)):
        result = await metadata.codex_import_thread(
            sandbox_mode="auto",
            thread_id="019abcdef",
            mode=mode,
        )

    assert result["thread"]["id"] == "019result"
    assert [turn["id"] for turn in result["thread"]["turns"]] == ["oldest", "newest"]
    assert client.request.await_args_list == [
        call(method, params, **request_kwargs),
        call(
            "thread/turns/list",
            {
                "threadId": "019result",
                "limit": 100,
                "sortDirection": "desc",
                "itemsView": "summary",
            },
        ),
    ]
    client.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_recent_thread_turns_paginates_with_bounded_frames() -> None:
    client = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                {
                    "data": [{"id": "four"}, {"id": "three"}],
                    "nextCursor": "page-2",
                },
                {
                    "data": [{"id": "two"}],
                    "nextCursor": "page-3",
                },
            ]
        )
    )

    turns = await metadata._recent_thread_turns(client, "019result", max_turns=3)

    assert [turn["id"] for turn in turns] == ["two", "three", "four"]
    assert client.request.await_args_list == [
        call(
            "thread/turns/list",
            {
                "threadId": "019result",
                "limit": 3,
                "sortDirection": "desc",
                "itemsView": "summary",
            },
        ),
        call(
            "thread/turns/list",
            {
                "threadId": "019result",
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "summary",
                "cursor": "page-2",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_import_forks_thread_seeds_native_resume_and_transcript(tmp_path) -> None:
    messages: list[dict[str, str]] = []

    def append(role, content, cls="", ts="", **_kwargs):
        messages.append({"role": role, "content": content, "cls": cls, "ts": ts})

    slot = SimpleNamespace(
        key="chat-1-import",
        title="",
        project="",
        messages=messages,
        append=append,
        _pending=[],
        event=MagicMock(),
        _titled=False,
        _title_origin="",
    )
    sessions = SimpleNamespace(
        find_key_by_sid=MagicMock(return_value=None),
        seed_conversation=MagicMock(),
        aflush=AsyncMock(),
    )
    state = SimpleNamespace(
        sessions=sessions,
        get_or_create_slot=MagicMock(return_value=slot),
        serialize_slot=MagicMock(return_value={"key": slot.key, "title": "Imported"}),
        push_slots_update=MagicMock(),
        suspend_slots_push=lambda: nullcontext(),
    )
    request = MagicMock()
    request.get.side_effect = lambda key, default="": {"user": "local-app", "app": ""}.get(
        key, default
    )
    request.__contains__.side_effect = lambda key: key in ("user", "app")
    request.__getitem__.side_effect = lambda key: {"user": "local-app", "app": ""}[key]
    request.json = AsyncMock(
        return_value={"thread_id": "019abcdef-1234-7890-abcd-1234567890ab", "mode": "fork"}
    )
    request.app = {"state": state}
    cfg = KiroCrewConfig(agent=AgentConfig(acp_backend="codex"))
    native = {
        "thread": {
            "id": "019forked-1234-7890-abcd-1234567890ab",
            "name": "Existing CLI work",
            "cwd": str(tmp_path),
            "turns": [
                {
                    "startedAt": 1_700_000_000,
                    "completedAt": 1_700_000_001,
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": "one"}]},
                        {"type": "agentMessage", "text": "two"},
                    ],
                }
            ],
        }
    }
    with (
        patch.object(codex_import.KiroCrewConfig, "load", return_value=cfg),
        patch(
            "kiro_crew.providers.codex.metadata.codex_import_thread",
            AsyncMock(return_value=native),
        ) as native_import,
        patch.object(codex_import, "effective_session_key", return_value="dashboard:chat-1-import"),
        patch.object(codex_import, "_sync_dashboard_slots"),
        patch.object(codex_import, "schedule_eager_spawn"),
        patch.object(codex_import, "save_slot_off_loop", AsyncMock()) as save,
    ):
        response = await codex_import.api_codex_thread_import(request)

    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["codex_thread_id"] == "019forked-1234-7890-abcd-1234567890ab"
    assert payload["import_mode"] == "fork"
    assert payload["imported_messages"] == 2
    assert slot.title == "Existing CLI work"
    assert slot.project == str(tmp_path)
    native_import.assert_awaited_once_with(
        sandbox_mode=cfg.agent.sandbox,
        thread_id="019abcdef-1234-7890-abcd-1234567890ab",
        mode="fork",
    )
    sessions.seed_conversation.assert_called_once_with(
        "dashboard:chat-1-import",
        "019forked-1234-7890-abcd-1234567890ab",
        provider="codex",
        cwd=str(tmp_path),
    )
    sessions.aflush.assert_awaited_once()
    save.assert_awaited_once_with(state, slot, force=True)
    assert [(row["role"], row["content"]) for row in messages] == [
        ("user", "one"),
        ("assistant", "two"),
    ]


@pytest.mark.asyncio
async def test_resume_refuses_a_thread_already_owned() -> None:
    sessions = SimpleNamespace(find_key_by_sid=MagicMock(return_value="dashboard:existing"))
    request = MagicMock()
    request.get.side_effect = lambda key, default="": {"user": "local-app", "app": ""}.get(
        key, default
    )
    request.__contains__.side_effect = lambda key: key in ("user", "app")
    request.__getitem__.side_effect = lambda key: {"user": "local-app", "app": ""}[key]
    request.json = AsyncMock(return_value={"thread_id": "019abcdef", "mode": "resume"})
    request.app = {"state": SimpleNamespace(sessions=sessions)}
    cfg = KiroCrewConfig(agent=AgentConfig(acp_backend="codex"))
    with patch.object(codex_import.KiroCrewConfig, "load", return_value=cfg):
        response = await codex_import.api_codex_thread_import(request)

    assert response.status == 409
    assert "already imported" in json.loads(response.body)["error"]
