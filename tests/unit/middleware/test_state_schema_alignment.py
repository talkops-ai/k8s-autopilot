"""Unit tests verifying state schema alignment with OpsCode.

Asserts that the initial state schema at step 0 (e.g. at PatchToolCallsMiddleware)
matches reference/logs/middleware_opscode_schema.json:
- _auto_temp_artifacts: {}
- _quickjs_snapshot_payload: "" / b""
- _session_cost_transfers: {}
- _session_cost_usd: 0
- files: {}
- goal_criteria_request: None
- messages: [HumanMessage with additional_kwargs containing user_prompt_metadata]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from k8s_autopilot.agent.factory import create_k8s_autopilot_agent
from k8s_autopilot.middleware.auto_mode import (
    USER_PROMPT_METADATA_KEY,
    AutoModeState,
    AutoTempArtifact,
    AutoTempArtifactMutation,
    _merge_temp_artifacts,
    user_prompt_metadata,
)
from k8s_autopilot.middleware.auto_mode_hitl import (
    _validate_temp_artifact,
    _validate_temp_artifact_mutation,
)
from k8s_autopilot.middleware.cost_tracking import CostState
from k8s_autopilot.middleware.goal_criteria import GoalCriteriaState


class TestAutoTempArtifacts:
    """Tests for AutoTempArtifact state mutations and reducer."""

    def test_validate_temp_artifact_valid(self) -> None:
        valid_payload = {
            "allocation_id": "alloc-10",
            "file_path": "/tmp/k8s-autopilot-scratch-123.txt",
            "thread_key": "thread-10",
            "turn_id": "turn-10",
            "created_by_tool_call_id": "call-10",
            "file_device": 1,
            "file_inode": 200,
        }
        artifact = _validate_temp_artifact(valid_payload)
        assert artifact is not None
        assert artifact["allocation_id"] == "alloc-10"
        assert artifact["file_path"] == "/tmp/k8s-autopilot-scratch-123.txt"

    def test_validate_temp_artifact_invalid_prefix(self) -> None:
        invalid_payload = {
            "allocation_id": "alloc-10",
            "file_path": "/tmp/non-scratch-file.txt",
            "thread_key": "thread-10",
            "turn_id": "turn-10",
            "created_by_tool_call_id": "call-10",
            "file_device": 1,
            "file_inode": 200,
        }
        artifact = _validate_temp_artifact(invalid_payload)
        assert artifact is None

    def test_validate_temp_artifact_mutation(self) -> None:
        mutation_dict = {
            "allocation_id": "alloc-1",
            "artifact": {
                "allocation_id": "alloc-1",
                "file_path": "/tmp/k8s-autopilot-scratch-test.txt",
                "thread_key": "thread-1",
                "turn_id": "turn-1",
                "created_by_tool_call_id": "call-1",
                "file_device": 1,
                "file_inode": 100,
            },
        }
        mutation = _validate_temp_artifact_mutation("/tmp/k8s-autopilot-scratch-test.txt", mutation_dict)
        assert mutation is not None
        assert mutation["allocation_id"] == "alloc-1"

    def test_merge_create_mutation(self) -> None:
        mutation: AutoTempArtifactMutation = {
            "allocation_id": "alloc-1",
            "artifact": {
                "allocation_id": "alloc-1",
                "file_path": "/tmp/k8s-autopilot-scratch-test.txt",
                "thread_key": "thread-1",
                "turn_id": "turn-1",
                "created_by_tool_call_id": "call-1",
                "file_device": 1,
                "file_inode": 100,
            },
        }
        current: dict[str, AutoTempArtifactMutation] = {}
        merged = _merge_temp_artifacts(current, {"/tmp/k8s-autopilot-scratch-test.txt": mutation})
        assert len(merged) == 1
        assert "/tmp/k8s-autopilot-scratch-test.txt" in merged
        assert merged["/tmp/k8s-autopilot-scratch-test.txt"]["allocation_id"] == "alloc-1"

    def test_merge_delete_mutation(self) -> None:
        create_mutation: AutoTempArtifactMutation = {
            "allocation_id": "alloc-1",
            "artifact": {
                "allocation_id": "alloc-1",
                "file_path": "/tmp/k8s-autopilot-scratch-test.txt",
                "thread_key": "thread-1",
                "turn_id": "turn-1",
                "created_by_tool_call_id": "call-1",
                "file_device": 1,
                "file_inode": 100,
            },
        }
        delete_mutation: AutoTempArtifactMutation = {
            "allocation_id": "alloc-1",
            "artifact": None,
        }
        current = {"/tmp/k8s-autopilot-scratch-test.txt": create_mutation}
        merged = _merge_temp_artifacts(current, {"/tmp/k8s-autopilot-scratch-test.txt": delete_mutation})
        assert len(merged) == 0


class TestUserPromptMetadata:
    """Tests for user prompt metadata formatting."""

    def test_user_prompt_metadata_structure(self) -> None:
        meta = user_prompt_metadata(
            literal_user_text="hi how are you",
            referenced_paths=["/path/to/file.yaml"],
            turn_id="test-turn-123",
        )
        assert meta["literal_user_text"] == "hi how are you"
        assert meta["referenced_paths"] == ["/path/to/file.yaml"]
        assert meta["turn_id"] == "test-turn-123"

    def test_user_prompt_metadata_defaults(self) -> None:
        meta = user_prompt_metadata(
            literal_user_text="simple query",
        )
        assert meta["literal_user_text"] == "simple query"
        assert meta["referenced_paths"] == []
        assert meta["turn_id"] is None


class FakeChatModelWithTools(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        from langchain_core.outputs import ChatGeneration, ChatResult
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="I am doing well!"))])


class TestStateSchemaAlignment:
    """Verify that graph channels and initial turn state match the OpsCode schema."""

    def test_graph_channels_contain_all_opscode_state_keys(self, tmp_path: Path) -> None:
        fake_model = FakeChatModelWithTools(messages=iter([AIMessage(content="hello")]))
        graph, _ = create_k8s_autopilot_agent(
            model=fake_model,
            cwd=tmp_path,
            enable_interpreter=True,
            interactive=True,
        )

        channels = graph.channels
        # Required channels matching OpsCode schema
        assert "_auto_temp_artifacts" in channels
        assert "_quickjs_snapshot_payload" in channels
        assert "_session_cost_transfers" in channels
        assert "_session_cost_usd" in channels
        assert "goal_criteria_request" in channels
        assert "messages" in channels
        if "files" in channels:
            assert isinstance(channels["files"], Any)

    @pytest.mark.asyncio
    async def test_initial_state_matches_opscode_schema(self, tmp_path: Path) -> None:
        from langgraph.checkpoint.memory import MemorySaver

        captured_initial_states: list[dict[str, Any]] = []

        fake_model = FakeChatModelWithTools(messages=iter([AIMessage(content="I am doing well!")] * 10))
        checkpointer = MemorySaver()
        graph, _ = create_k8s_autopilot_agent(
            model=fake_model,
            cwd=tmp_path,
            checkpointer=checkpointer,
            enable_interpreter=True,
            interactive=True,
        )

        turn_id = "b9c31c10-ffef-4929-8a07-d7b1a23cd39f"
        prompt = "hi how are you"
        prompt_meta = user_prompt_metadata(
            literal_user_text=prompt,
            referenced_paths=[],
            turn_id=turn_id,
        )

        human_msg = HumanMessage(
            content=prompt,
            additional_kwargs={
                USER_PROMPT_METADATA_KEY: prompt_meta,
            },
        )

        input_data = {
            "messages": [human_msg],
            "goal_criteria_request": None,
        }

        # Verify initial input keys
        assert "messages" in input_data
        assert input_data["goal_criteria_request"] is None
        msg = input_data["messages"][0]
        assert isinstance(msg, HumanMessage)
        assert msg.content == "hi how are you"
        assert msg.additional_kwargs[USER_PROMPT_METADATA_KEY]["literal_user_text"] == "hi how are you"
        assert msg.additional_kwargs[USER_PROMPT_METADATA_KEY]["turn_id"] == turn_id

        # Stream one step and inspect the state checkpoint
        config = {"configurable": {"thread_id": "test-schema-alignment"}}
        async for _ in graph.astream(input_data, config=config):
            pass

        state = await graph.aget_state(config)
        values = state.values

        # Check expected keys are present in the resulting state
        assert "_auto_temp_artifacts" in values
        assert isinstance(values["_auto_temp_artifacts"], dict)

        assert "_session_cost_transfers" in values
        assert isinstance(values["_session_cost_transfers"], dict)

        assert "_session_cost_usd" in values
        assert isinstance(values["_session_cost_usd"], (int, float))

        if "files" in values:
            assert isinstance(values["files"], dict)

        assert "goal_criteria_request" in values
        assert values["goal_criteria_request"] is None

        assert "messages" in values
        assert len(values["messages"]) >= 1
        first_msg = values["messages"][0]
        assert first_msg.content == "hi how are you"
        assert USER_PROMPT_METADATA_KEY in first_msg.additional_kwargs
        assert "opscode_user_prompt" not in first_msg.additional_kwargs

    def test_all_middleware_trace_policies_preserve_inputs(self) -> None:
        """Verify that all middlewares preserve full input state in LangSmith trace policies.
        
        No middleware should have process_inputs=omit_payload (which strips input to {}).
        """
        from deepagents.middleware.filesystem import FilesystemMiddleware
        from deepagents.middleware.memory import MemoryMiddleware
        from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
        from deepagents.middleware.rubric import RubricMiddleware
        from deepagents.middleware.skills import SkillsMiddleware
        from deepagents.middleware.subagents import SubAgentMiddleware
        from deepagents.middleware.summarization import (
            SummarizationToolMiddleware,
            _DeepAgentsSummarizationMiddleware,
        )
        from langchain.agents.middleware._trace_policy import _node_trace_policy

        from k8s_autopilot.config.langsmith import enable_full_middleware_tracing
        from k8s_autopilot.middleware.compaction import CLICompactionMiddleware
        from k8s_autopilot.middleware.cost_tracking import CostTrackingMiddleware
        from k8s_autopilot.middleware.goal_criteria import GoalCriteriaMiddleware
        from k8s_autopilot.middleware.local_context import LocalContextMiddleware
        from k8s_autopilot.middleware.reliable_rubric import ReliableRubricMiddleware
        from k8s_autopilot.middleware.skills import PluginSkillsMiddleware
        from k8s_autopilot.middleware.subagents import SubagentsMiddleware

        enable_full_middleware_tracing()

        all_middlewares = [
            PatchToolCallsMiddleware,
            MemoryMiddleware,
            SkillsMiddleware,
            PluginSkillsMiddleware,
            RubricMiddleware,
            ReliableRubricMiddleware,
            FilesystemMiddleware,
            SubAgentMiddleware,
            SummarizationToolMiddleware,
            _DeepAgentsSummarizationMiddleware,
            CLICompactionMiddleware,
            CostTrackingMiddleware,
            LocalContextMiddleware,
            SubagentsMiddleware,
            GoalCriteriaMiddleware,
        ]

        sample_state = {
            "_auto_temp_artifacts": {},
            "_quickjs_snapshot_payload": "",
            "_session_cost_transfers": {},
            "_session_cost_usd": 0,
            "files": {},
            "goal_criteria_request": None,
            "messages": [{"type": "human", "content": "hi how are you"}],
        }

        for mw in all_middlewares:
            policy = _node_trace_policy(getattr(mw, "trace_policy", None))
            processed = policy.process_inputs(sample_state)
            # Verify input is NOT stripped to {}
            assert processed == sample_state, f"Middleware {mw.__name__} stripped input state"
            assert "messages" in processed
            assert "_quickjs_snapshot_payload" in processed

