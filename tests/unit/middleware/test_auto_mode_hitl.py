"""Unit tests for auto-mode HITL middleware — classifier models, deterministic rules, and sanitization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestAutoDecisionModels:
    def test_category_values(self):
        from k8s_autopilot.middleware.auto_mode_hitl import AutoDecisionCategory

        assert AutoDecisionCategory.SCOPE_ESCALATION == "scope_escalation"
        assert AutoDecisionCategory.DESTRUCTIVE_ACTION == "destructive_action"

    def test_valid_allow_decision(self):
        from k8s_autopilot.middleware.auto_mode_hitl import AutoDecision

        d = AutoDecision(
            tool_call_id="tc-1", decision="allow",
            category="other_policy", reason="",
        )
        assert d.decision == "allow"

    def test_deny_requires_reason(self):
        from k8s_autopilot.middleware.auto_mode_hitl import AutoDecision

        with pytest.raises(Exception):
            AutoDecision(
                tool_call_id="tc-1", decision="deny",
                category="scope_escalation", reason="",
            )

    def test_empty_tool_call_id_rejected(self):
        from k8s_autopilot.middleware.auto_mode_hitl import AutoDecision

        with pytest.raises(Exception):
            AutoDecision(
                tool_call_id="", decision="allow",
                category="other_policy", reason="",
            )

    def test_batch_validation(self):
        from k8s_autopilot.middleware.auto_mode_hitl import (
            AutoDecision, AutoDecisionBatch, _validate_classifier_ids,
        )

        batch = AutoDecisionBatch(decisions=[
            AutoDecision(
                tool_call_id="tc-1", decision="allow",
                category="other_policy", reason="",
            ),
        ])
        _validate_classifier_ids(batch, {"tc-1"})

        with pytest.raises(ValueError, match="exactly one decision"):
            _validate_classifier_ids(batch, {"tc-1", "tc-2"})


class TestSanitization:
    def test_basic_text(self):
        from k8s_autopilot.middleware.auto_mode_hitl import sanitize_auto_reason

        assert sanitize_auto_reason("Hello world") == "Hello world"

    def test_empty_gets_default(self):
        from k8s_autopilot.middleware.auto_mode_hitl import sanitize_auto_reason

        assert "not authorized" in sanitize_auto_reason("")

    def test_known_secrets_redacted(self):
        from k8s_autopilot.middleware.auto_mode_hitl import sanitize_auto_reason

        assert "[redacted]" in sanitize_auto_reason(
            "key=mysupersecretvalue123",
            known_secrets=("mysupersecretvalue123",),
        )

    def test_url_credentials_redacted(self):
        from k8s_autopilot.middleware.auto_mode_hitl import sanitize_auto_reason

        result = sanitize_auto_reason("https://user:pass@example.com/path?key=val")
        assert "pass" not in result
        # Credentials are replaced with ***@ and query values with [redacted]
        assert "***@" in result


class TestToolCallIdHelpers:
    def test_extract_valid_id(self):
        from k8s_autopilot.middleware.auto_mode_hitl import _tool_call_id

        assert _tool_call_id({"id": "tc-123", "name": "test", "args": {}}) == "tc-123"

    def test_missing_id_raises(self):
        from k8s_autopilot.middleware.auto_mode_hitl import _tool_call_id

        with pytest.raises(ValueError, match="tool call to have an ID"):
            _tool_call_id({"name": "test", "args": {}})

    def test_batch_id_deterministic(self):
        from k8s_autopilot.middleware.auto_mode_hitl import _batch_id

        calls = [
            {"id": "tc-1", "name": "a", "args": {}},
            {"id": "tc-2", "name": "b", "args": {}},
        ]
        assert _batch_id(calls) == _batch_id(calls)

    def test_batch_id_varies_by_calls(self):
        from k8s_autopilot.middleware.auto_mode_hitl import _batch_id

        a = [{"id": "tc-1", "name": "a", "args": {}}]
        b = [{"id": "tc-2", "name": "b", "args": {}}]
        assert _batch_id(a) != _batch_id(b)

    def test_unique_ids_validated(self):
        from k8s_autopilot.middleware.auto_mode_hitl import _validate_unique_tool_call_ids

        _validate_unique_tool_call_ids([
            {"id": "tc-1", "name": "a", "args": {}},
            {"id": "tc-2", "name": "b", "args": {}},
        ])
        with pytest.raises(ValueError, match="duplicate"):
            _validate_unique_tool_call_ids([
                {"id": "tc-1", "name": "a", "args": {}},
                {"id": "tc-1", "name": "b", "args": {}},
            ])


class TestDeterministicAllowDeny:
    @pytest.fixture
    def repo(self, tmp_path):
        """Create a resolved repo root for consistent path tests."""
        root = tmp_path / "repo"
        root.mkdir()
        return root

    def test_web_search_allowed(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "web_search", "args": {"query": "test"}}
        assert _deterministic_allow(repo, call, None) is True

    def test_routine_py_write_allowed(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "write_file", "args": {"file_path": str(repo / "src" / "main.py")}}
        assert _deterministic_allow(repo, call, None) is True

    def test_env_file_denied(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "write_file", "args": {"file_path": str(repo / ".env")}}
        assert _deterministic_allow(repo, call, None) is False

    def test_shell_script_denied(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "write_file", "args": {"file_path": str(repo / "deploy.sh")}}
        assert _deterministic_allow(repo, call, None) is False

    def test_git_status_allowed(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "execute", "args": {"command": "git status"}}
        assert _deterministic_allow(repo, call, None) is True

    def test_git_push_denied(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "execute", "args": {"command": "git push origin main"}}
        assert _deterministic_allow(repo, call, None) is False

    def test_shell_injection_denied(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "execute", "args": {"command": "git status && rm -rf /"}}
        assert _deterministic_allow(repo, call, None) is False

    def test_outside_worktree_denied(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        call = {"name": "write_file", "args": {"file_path": "/etc/passwd"}}
        assert _deterministic_allow(repo, call, None) is False

    def test_dependency_file_denied(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _deterministic_allow

        for dep in ["package.json", "pyproject.toml", "go.mod", "Cargo.toml"]:
            call = {"name": "write_file", "args": {"file_path": str(repo / dep)}}
            assert _deterministic_allow(repo, call, None) is False, dep


class TestSensitiveWritePaths:
    @pytest.fixture
    def repo(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        return root

    def test_env_is_sensitive(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _is_sensitive_write_path

        assert _is_sensitive_write_path(repo, repo / ".env") is True

    def test_git_dir_is_sensitive(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _is_sensitive_write_path

        assert _is_sensitive_write_path(repo, repo / ".git" / "config") is True

    def test_source_file_not_sensitive(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _is_sensitive_write_path

        assert _is_sensitive_write_path(repo, repo / "src" / "main.py") is False

    def test_outside_root_is_sensitive(self, repo):
        from k8s_autopilot.middleware.auto_mode_hitl import _is_sensitive_write_path

        assert _is_sensitive_write_path(repo, Path("/etc/passwd")) is True


class TestMCPToolIntrospection:
    def test_read_only_detected(self):
        from k8s_autopilot.middleware.auto_mode_hitl import mcp_tool_is_coherently_read_only

        tool = MagicMock()
        tool.metadata = {"readOnlyHint": True, "destructiveHint": False}
        assert mcp_tool_is_coherently_read_only(tool) is True

    def test_non_readonly_rejected(self):
        from k8s_autopilot.middleware.auto_mode_hitl import mcp_tool_is_coherently_read_only

        tool = MagicMock()
        tool.metadata = {"readOnlyHint": False}
        assert mcp_tool_is_coherently_read_only(tool) is False

    def test_no_metadata_rejected(self):
        from k8s_autopilot.middleware.auto_mode_hitl import mcp_tool_is_coherently_read_only

        tool = MagicMock(spec=[])
        assert mcp_tool_is_coherently_read_only(tool) is False


class TestClassifierPolicyPrompt:
    def test_contains_essential_rules(self):
        from k8s_autopilot.middleware.auto_mode_hitl import _CLASSIFIER_POLICY

        assert "authorization_evidence" in _CLASSIFIER_POLICY
        assert "trust boundary" in _CLASSIFIER_POLICY
        assert "force-push" in _CLASSIFIER_POLICY
        assert "credential" in _CLASSIFIER_POLICY.lower()
        assert "scope escalation" in _CLASSIFIER_POLICY


class TestAutoModeHITLMiddlewareInit:
    def test_instantiation(self, tmp_path):
        from k8s_autopilot.middleware.auto_mode_hitl import AutoModeHITLMiddleware

        repo = tmp_path / "test-repo"
        repo.mkdir()
        mw = AutoModeHITLMiddleware(worktree_root=str(repo))
        assert mw._worktree_root == repo.resolve()
        assert len(mw.tools) == 2
        tool_names = {t.name for t in mw.tools}
        assert tool_names == {"create_temp_artifact", "delete_temp_artifact"}


class TestTempArtifactTools:
    def test_validate_suffix(self):
        from k8s_autopilot.middleware.auto_mode_hitl import _validate_temp_artifact_suffix

        assert _validate_temp_artifact_suffix("") == ""
        assert _validate_temp_artifact_suffix(".md") == ".md"
        assert _validate_temp_artifact_suffix(".json") == ".json"
        with pytest.raises(ValueError, match="suffix"):
            _validate_temp_artifact_suffix("invalid suffix with spaces")

    def test_allocate_and_delete_temp_artifact(self):
        from k8s_autopilot.middleware.auto_mode_hitl import (
            _allocate_temp_artifact,
            _delete_temp_artifact_file,
        )

        artifact = _allocate_temp_artifact(
            "test content",
            ".txt",
            thread_key="thread-1",
            turn_id="turn-1",
            tool_call_id="call-1",
        )
        assert Path(artifact["file_path"]).exists()
        assert Path(artifact["file_path"]).read_text(encoding="utf-8") == "test content"

        _delete_temp_artifact_file(artifact)
        assert not Path(artifact["file_path"]).exists()

    def test_managed_temp_rejection_generic_tools(self, tmp_path):
        from langchain.agents.middleware.types import ToolCallRequest
        from k8s_autopilot.middleware.auto_mode_hitl import (
            AutoModeHITLMiddleware,
            AutoTempArtifact,
            AutoTempArtifactMutation,
            _allocate_temp_artifact,
        )

        repo = tmp_path / "repo"
        repo.mkdir()
        mw = AutoModeHITLMiddleware(worktree_root=str(repo))

        artifact = _allocate_temp_artifact(
            "data",
            ".txt",
            thread_key="t1",
            turn_id="turn1",
            tool_call_id="c1",
        )
        try:
            state = {
                "_auto_temp_artifacts": {
                    artifact["file_path"]: AutoTempArtifactMutation(
                        allocation_id=artifact["allocation_id"],
                        artifact=artifact,
                    )
                }
            }
            # Attempt to write to managed scratch path with generic write_file tool
            req = ToolCallRequest(
                tool_call={"name": "write_file", "args": {"file_path": artifact["file_path"]}, "id": "tc-write"},
                state=state,
                runtime=MagicMock(),
                tool=MagicMock(),
            )
            rejection = mw._managed_temp_rejection(req)
            assert rejection is not None
            assert "Managed temporary artifacts cannot be changed" in rejection.content

            # Non-managed file should not be rejected
            req_normal = ToolCallRequest(
                tool_call={"name": "write_file", "args": {"file_path": str(repo / "normal.txt")}, "id": "tc-normal"},
                state=state,
                runtime=MagicMock(),
                tool=MagicMock(),
            )
            assert mw._managed_temp_rejection(req_normal) is None
        finally:
            Path(artifact["file_path"]).unlink(missing_ok=True)
