"""Unit tests for LocalContextMiddleware and detection script."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from k8s_autopilot.middleware.local_context import (
    DETECT_CONTEXT_SCRIPT,
    LocalContextMiddleware,
    _build_k8s_context_section,
    build_detect_script,
)


class TestLocalContextMiddleware:
    def test_detect_script_generation(self) -> None:
        script = build_detect_script()
        assert "## Local Context" in script
        assert "PROJ_LANG" in script
        assert "__DETECT_CONTEXT_EOF__" in script

    def test_k8s_context_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KUBECONFIG", "/test/kubeconfig")
        monkeypatch.setenv("KUBE_NAMESPACE", "prod-system")
        monkeypatch.setenv("ARGOCD_SERVER", "https://argocd.internal")

        section = _build_k8s_context_section()
        assert "### Kubernetes Environment" in section
        assert "KUBECONFIG: /test/kubeconfig" in section
        assert "Kubernetes namespace: prod-system" in section
        assert "ArgoCD server: https://argocd.internal" in section

    def test_before_agent_cached_state_passthrough(self) -> None:
        mw = LocalContextMiddleware()
        runtime = MagicMock()
        state = {"_local_context": "Already cached context"}
        update = mw.before_agent(state, runtime)
        assert update is None

    def test_before_agent_initial_detection(self) -> None:
        mw = LocalContextMiddleware(working_dir="/tmp/workspace")
        runtime = MagicMock()
        state = {}
        update = mw.before_agent(state, runtime)
        assert update is not None
        assert "_local_context" in update
        assert "/tmp/workspace" in update["_local_context"]
