"""Unit tests for rubric generation, evaluation, and reliable middleware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from k8s_autopilot.middleware.goal_state_notice import build_goal_state_notice
from k8s_autopilot.rubrics.evaluator import (
    _create_rubric_grader_tools,
    _validate_rubric_grader_read_path,
)
from k8s_autopilot.rubrics.generator import (
    GOAL_AMENDMENT_SYSTEM_PROMPT,
    GOAL_RUBRIC_SYSTEM_PROMPT,
    K8S_RUBRIC_SYSTEM_PROMPT,
    _goal_amendment_human_prompt,
    _goal_rubric_human_prompt,
    generate_rubric,
)
from k8s_autopilot.rubrics.middleware import ReliableRubricMiddleware, RubricMiddleware
from k8s_autopilot.middleware.reliable_rubric import (
    _is_transient_grader_transport_error,
    _without_internal_control_messages,
)
import httpx


def test_rubric_prompts_and_helpers() -> None:
    """Verify rubric system prompts and prompt builders."""
    assert "Kubernetes operations" in GOAL_RUBRIC_SYSTEM_PROMPT
    assert "Resource state verification" in K8S_RUBRIC_SYSTEM_PROMPT
    assert "amend" in GOAL_AMENDMENT_SYSTEM_PROMPT

    human_prompt = _goal_rubric_human_prompt("Deploy nginx helm chart")
    assert "<goal>\nDeploy nginx helm chart\n</goal>" in human_prompt

    feedback_prompt = _goal_rubric_human_prompt(
        "Deploy nginx helm chart",
        feedback="Include replica count check",
        previous_criteria="- Pod is running",
    )
    assert "<user_feedback>\nInclude replica count check\n</user_feedback>" in feedback_prompt
    assert "<previous_criteria>\n- Pod is running\n</previous_criteria>" in feedback_prompt

    amend_prompt = _goal_amendment_human_prompt(
        "Deploy nginx", "- Pod is running", "Add service check"
    )
    assert "<current_goal>\nDeploy nginx\n</current_goal>" in amend_prompt
    assert "<user_feedback>\nAdd service check\n</user_feedback>" in amend_prompt


def test_validate_rubric_grader_read_path() -> None:
    """Verify evidence path validation rejects arbitrary filesystem paths."""
    assert _validate_rubric_grader_read_path("/large_tool_results/result_1.txt") is None
    assert _validate_rubric_grader_read_path("/etc/passwd") is not None
    assert _validate_rubric_grader_read_path("/large_tool_results/../secret") is not None


def test_create_rubric_grader_tools(tmp_path) -> None:
    """Verify rubric grader read_file tool reads allowed paths."""
    tools = _create_rubric_grader_tools()
    assert len(tools) == 1
    read_tool = tools[0]
    assert read_tool.name == "read_file"

    # Disallowed path test
    res = read_tool.invoke({"file_path": "/tmp/test.txt"})
    assert "Rubric grader can only read files under /large_tool_results/" in res


def test_transient_transport_error_detection() -> None:
    """Verify retryable transport errors are correctly recognized."""
    read_err = httpx.ReadError("Connection lost during read")
    assert _is_transient_grader_transport_error(read_err) is True

    proto_err = httpx.RemoteProtocolError("Server disconnected unexpectedly")
    assert _is_transient_grader_transport_error(proto_err) is True

    val_err = ValueError("Invalid input")
    assert _is_transient_grader_transport_error(val_err) is False


def test_without_internal_control_messages() -> None:
    """Verify control messages like goal notices are filtered before grader evaluation."""
    control_msg = build_goal_state_notice({"goal_objective": "test", "goal_status": "accepted"})
    normal_msg = HumanMessage(content="Hello")
    state = {"messages": [normal_msg, control_msg]}

    filtered = _without_internal_control_messages(state)
    assert len(filtered["messages"]) == 1
    assert filtered["messages"][0] == normal_msg


@patch("k8s_autopilot.rubrics.generator.create_model")
def test_generate_rubric(mock_create_model) -> None:
    """Verify generate_rubric invokes model and returns stripped text."""
    mock_model = MagicMock()
    mock_model.invoke.return_value = AIMessage(content="- Criterion 1\n- Criterion 2\n")
    mock_create_model.return_value = MagicMock(model=mock_model)

    result = generate_rubric("Verify deployment")
    assert result == "- Criterion 1\n- Criterion 2"
    mock_model.invoke.assert_called_once()


def test_reliable_rubric_middleware_initialization() -> None:
    """Verify ReliableRubricMiddleware initializes correctly and exports RubricMiddleware alias."""
    mw = ReliableRubricMiddleware(model="gpt-4o", max_iterations=5)
    assert mw.max_iterations == 5
    assert ReliableRubricMiddleware is RubricMiddleware
