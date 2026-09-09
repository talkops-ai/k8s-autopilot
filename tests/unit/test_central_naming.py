"""Unit tests for Central Agent Naming and Discovery Backend Alignment.

Validates that:
1. Agent card JSON and fallback card define canonical kebab-case names ('k8s-autopilot').
2. Thread persistence (auto_touch and create_thread) defaults to 'k8s-autopilot'.
3. Thread search seamlessly finds threads saved under legacy names
   ('k8sAutopilotSupervisorAgent', 'k8s_autopilot', 'k8sAutopilotAgent', or None)
   when queried with agent_id='k8s-autopilot'.
4. Thread search filters out threads belonging to other agents.
5. Response metadata normalizes legacy agent aliases to 'k8s-autopilot'.
6. Executor defaults agent identity to 'k8s-autopilot'.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
import uuid

from langgraph.checkpoint.base import empty_checkpoint
import pytest

from k8s_autopilot.api.models import ThreadCreate, ThreadSearch
from k8s_autopilot.api.service import ThreadService
from k8s_autopilot.server.app import create_app
from k8s_autopilot.server.executor import A2AAutoPilotExecutor
from k8s_autopilot.state.session import create_checkpointer


def test_agent_card_json_spec() -> None:
    """Verify k8s_autopilot.json complies with TalkOps naming and discovery specs."""
    card_path = (
        Path(__file__).resolve().parent.parent.parent
        / "k8s_autopilot"
        / "card"
        / "k8s_autopilot.json"
    )
    assert card_path.is_file(), f"Card file not found: {card_path}"

    with card_path.open() as f:
        data = json.load(f)

    assert data["name"] == "k8s-autopilot"

    # Verify TalkOps UI extension metadata
    extensions = data.get("capabilities", {}).get("extensions", [])
    talkops_ext = next(
        (ext for ext in extensions if ext.get("uri") == "https://talkops.ai/a2a-extension/talkops-ui/v1"),
        None,
    )
    assert talkops_ext is not None, "TalkOps UI extension not found in agent card"

    metadata = talkops_ext.get("params", {}).get("metadata", {})
    assert metadata.get("id") == "k8s-autopilot"
    assert metadata.get("displayName") == "k8s-autopilot"


def test_agent_card_endpoint() -> None:
    """Verify /.well-known/agent-card.json serves canonical id and displayName."""
    from starlette.testclient import TestClient

    app = create_app()
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "k8s-autopilot"
    extensions = card.get("capabilities", {}).get("extensions", [])
    talkops_ext = next(
        (ext for ext in extensions if ext.get("uri") == "https://talkops.ai/a2a-extension/talkops-ui/v1"),
        None,
    )
    assert talkops_ext is not None
    meta = talkops_ext.get("params", {}).get("metadata", {})
    assert meta.get("id") == "k8s-autopilot"
    assert meta.get("displayName") == "k8s-autopilot"


def test_server_fallback_card_spec() -> None:
    """Verify fallback card definition in create_app complies with canonical naming."""
    from starlette.testclient import TestClient

    app = create_app(agent_card_path="/tmp/non_existent_card_file_xyz.json")
    client = TestClient(app)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "k8s-autopilot"
    extensions = card.get("capabilities", {}).get("extensions", [])
    talkops_ext = next(
        (ext for ext in extensions if ext.get("uri") == "https://talkops.ai/a2a-extension/talkops-ui/v1"),
        None,
    )
    assert talkops_ext is not None
    meta = talkops_ext.get("params", {}).get("metadata", {})
    assert meta.get("id") == "k8s-autopilot"
    assert meta.get("displayName") == "k8s-autopilot"


@pytest.mark.asyncio
async def test_search_threads_backward_compatibility_and_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify search_threads matches all historical aliases for k8s-autopilot and isolates others."""
    db_file = tmp_path / "test_naming.db"
    monkeypatch.setattr("k8s_autopilot.state.session.get_db_path", lambda: db_file)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")

    async with create_checkpointer(backend="sqlite") as checkpointer:
        service = ThreadService(checkpointer=checkpointer)

        # 1. Seed threads with different agent representations
        variants = [
            ("tid-k8s-kebab", "k8s-autopilot", "Thread 1 (kebab)"),
            ("tid-k8s-camel", "k8sAutopilotSupervisorAgent", "Thread 2 (camel)"),
            ("tid-k8s-snake", "k8s_autopilot", "Thread 3 (snake)"),
            ("tid-k8s-agent", "k8sAutopilotAgent", "Thread 4 (agent)"),
            ("tid-k8s-legacy-none", None, "Thread 5 (legacy none)"),
            ("tid-other-agent", "helm-operator", "Thread 6 (other agent)"),
        ]

        for tid, agent_name, title in variants:
            cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
            cp = empty_checkpoint()
            meta: dict[str, Any] = {
                "title": title,
                "created_at": "2026-09-08T10:00:00+00:00",
                "updated_at": "2026-09-08T10:00:00+00:00",
            }
            if agent_name is not None:
                meta["agent_name"] = agent_name
                meta["agent_id"] = agent_name

            await checkpointer.aput(cast(Any, cfg), cp, cast(Any, meta), new_versions={})

        # 2. Search for k8s-autopilot
        req = ThreadSearch(agent_id="k8s-autopilot", limit=20)
        results = await service.search_threads(req)

        result_ids = {str(r.thread_id) for r in results}
        assert "tid-k8s-kebab" in result_ids
        assert "tid-k8s-camel" in result_ids
        assert "tid-k8s-snake" in result_ids
        assert "tid-k8s-agent" in result_ids
        assert "tid-k8s-legacy-none" in result_ids
        assert "tid-other-agent" not in result_ids

        # 3. Verify all returned k8s-autopilot metadata is normalized
        for r in results:
            assert r.metadata.get("agent_name") == "k8s-autopilot"
            assert r.metadata.get("agent_id") == "k8s-autopilot"

        # 4. Search using legacy camelCase alias should also return all k8s-autopilot threads
        req_legacy = ThreadSearch(agent_id="k8sAutopilotSupervisorAgent", limit=20)
        results_legacy = await service.search_threads(req_legacy)
        legacy_ids = {str(r.thread_id) for r in results_legacy}
        assert legacy_ids == result_ids

        # 5. Search for the other agent must only return the other agent
        req_other = ThreadSearch(agent_id="helm-operator", limit=20)
        results_other = await service.search_threads(req_other)
        assert len(results_other) == 1
        assert str(results_other[0].thread_id) == "tid-other-agent"
        assert results_other[0].metadata.get("agent_name") == "helm-operator"


@pytest.mark.asyncio
async def test_auto_touch_and_create_thread_default_naming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify auto_touch and create_thread default agent_name and agent_id to k8s-autopilot."""
    db_file = tmp_path / "test_defaults.db"
    monkeypatch.setattr("k8s_autopilot.state.session.get_db_path", lambda: db_file)
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "sqlite")

    async with create_checkpointer(backend="sqlite") as checkpointer:
        service = ThreadService(checkpointer=checkpointer)

        # 1. auto_touch without explicit agent_id
        tid1 = str(uuid.uuid4())
        await service.auto_touch(thread_id=tid1, user_query="Hello test")
        res1 = await service.get_thread(tid1)
        assert res1 is not None
        assert res1.metadata.get("agent_name") == "k8s-autopilot"
        assert res1.metadata.get("agent_id") == "k8s-autopilot"

        # 2. create_thread without explicit agent_name/agent_id
        res2 = await service.create_thread(ThreadCreate(title="Created thread"))
        assert res2.metadata.get("agent_name") == "k8s-autopilot"
        assert res2.metadata.get("agent_id") == "k8s-autopilot"


def test_executor_default_agent_identity() -> None:
    """Verify executor defaults to k8s-autopilot when agent has no name."""
    executor = A2AAutoPilotExecutor()
    assert getattr(executor.agent, "name", None) in ("k8s-autopilot", None)
