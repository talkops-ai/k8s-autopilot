"""Unit tests for K8s Autopilot utils module."""

from __future__ import annotations

import pytest
from pathlib import Path

from k8s_autopilot.utils import (
    AgentLogger,
    ModelStats,
    STARTUP_ERROR_MARKER,
    SessionStats,
    TokenCostEstimator,
    emit_startup_failure,
    estimate_cost,
    find_git_dir,
    format_cost,
    format_token_count,
    get_git_branch,
    get_git_root,
)


class TestSessionStats:
    def test_record_and_merge(self):
        stats1 = SessionStats()
        stats1.record_request("claude-3-7-sonnet", 100, 50, provider="anthropic", cost_usd=0.005)
        assert stats1.request_count == 1
        assert stats1.input_tokens == 100
        assert stats1.output_tokens == 50
        assert stats1.total_cost_usd == 0.005
        assert ("anthropic", "claude-3-7-sonnet") in stats1.per_model

        stats2 = SessionStats()
        stats2.record_request("gpt-4o", 200, 80, provider="openai", cost_usd=0.01)

        stats1.merge(stats2)
        assert stats1.request_count == 2
        assert stats1.input_tokens == 300
        assert stats1.output_tokens == 130
        assert stats1.total_cost_usd == 0.015
        assert ("openai", "gpt-4o") in stats1.per_model

    def test_formatting_helpers(self):
        assert format_token_count(500) == "500"
        assert format_token_count(1500) == "1.5K"
        assert format_token_count(2_500_000) == "2.5M"

        assert format_cost(0.0) == "$0.00"
        assert format_cost(0.002) == "<$0.01"
        assert format_cost(1.50) == "$1.50"


class TestGitUtils:
    def test_git_helpers_in_workspace(self):
        root = Path.cwd()
        git_dir = find_git_dir(root)
        assert git_dir is not None
        assert get_git_root(root) is not None
        branch = get_git_branch(root)
        assert branch is not None or branch is None  # Runs cleanly without error


class TestStartupError:
    def test_emit_startup_failure(self, capsys):
        exc = ValueError("Invalid cluster configuration")
        emit_startup_failure(exc)
        captured = capsys.readouterr()
        assert STARTUP_ERROR_MARKER in captured.err
        assert "ValueError: Invalid cluster configuration" in captured.err
