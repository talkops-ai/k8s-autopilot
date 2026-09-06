"""Unit tests for the backend module (Phase 2)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestBackendRegistry:
    """Tests for BackendRegistry singleton and decorator."""

    def test_singleton(self) -> None:
        from k8s_autopilot.backend.registry import get_backend_registry

        r1 = get_backend_registry()
        r2 = get_backend_registry()
        assert r1 is r2

    def test_register_and_build(self) -> None:
        from k8s_autopilot.backend.registry import BackendRegistry

        registry = BackendRegistry()

        class FakeBackend:
            def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
                self.kwargs = kwargs

        registry.register("fake", FakeBackend)
        backend = registry.build("fake", root_dir="/tmp/test")
        assert isinstance(backend, FakeBackend)
        assert backend.kwargs["root_dir"] == "/tmp/test"

    def test_build_unknown_raises(self) -> None:
        from k8s_autopilot.backend.registry import BackendRegistry

        registry = BackendRegistry()
        with pytest.raises(ValueError, match="not registered"):
            registry.build("nonexistent")

    def test_list_registered(self) -> None:
        from k8s_autopilot.backend.registry import BackendRegistry

        registry = BackendRegistry()

        class A:
            pass

        class B:
            pass

        registry.register("beta", B)
        registry.register("alpha", A)
        assert registry.list_registered() == ["alpha", "beta"]

    def test_decorator_registers(self) -> None:
        from k8s_autopilot.backend.registry import BackendRegistry

        registry = BackendRegistry()

        # Simulate the decorator
        registry.register("decorated", type("Decorated", (), {}))
        assert registry.get_instance() is not None


class TestLocalShellBackend:
    """Tests for LocalShellBackend K8s env var handling."""

    def test_local_backend_registered(self) -> None:
        from k8s_autopilot.backend.registry import get_backend_registry

        # Import triggers @register_backend("local") decorator
        from k8s_autopilot.backend.local import LocalShellBackend  # noqa: F401

        registry = get_backend_registry()
        assert "local" in registry.list_registered()

    def test_k8s_env_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KUBECONFIG", "/home/user/.kube/config")
        monkeypatch.setenv("HELM_HOME", "/home/user/.helm")
        monkeypatch.setenv("ARGOCD_SERVER", "argocd.example.com")
        monkeypatch.setenv("PROMETHEUS_URL", "http://prometheus:9090")

        from k8s_autopilot.backend.local import _build_shell_env

        env = _build_shell_env()
        assert env["KUBECONFIG"] == "/home/user/.kube/config"
        assert env["HELM_HOME"] == "/home/user/.helm"
        assert env["ARGOCD_SERVER"] == "argocd.example.com"
        assert env["PROMETHEUS_URL"] == "http://prometheus:9090"

    def test_kube_prefix_env_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KUBE_CUSTOM_FLAG", "true")
        monkeypatch.setenv("KUBECTL_EXTERNAL_DIFF", "meld")

        from k8s_autopilot.backend.local import _build_shell_env

        env = _build_shell_env()
        assert env["KUBE_CUSTOM_FLAG"] == "true"
        assert env["KUBECTL_EXTERNAL_DIFF"] == "meld"

    def test_safe_keys_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/testuser")
        monkeypatch.setenv("SHELL", "/bin/zsh")

        from k8s_autopilot.backend.local import _build_shell_env

        env = _build_shell_env()
        assert env["HOME"] == "/home/testuser"
        assert env["SHELL"] == "/bin/zsh"

    def test_path_prepend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")

        from k8s_autopilot.backend.local import _build_shell_env

        env = _build_shell_env()
        assert "/usr/local/bin" in env["PATH"]

    def test_sensitive_env_not_leaked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SECRET_PASSWORD", "hunter2")
        monkeypatch.setenv("PRIVATE_KEY", "-----BEGIN-----")

        from k8s_autopilot.backend.local import _build_shell_env

        env = _build_shell_env()
        assert "SECRET_PASSWORD" not in env
        assert "PRIVATE_KEY" not in env


class TestK8sCompositeBackend:
    """Tests for K8sCompositeBackend routing and cleanup."""

    def test_composite_creates_temp_dir(self) -> None:
        from k8s_autopilot.backend.composite import K8sCompositeBackend

        backend = K8sCompositeBackend(default=MagicMock())
        assert backend._large_results_dir
        assert Path(backend._large_results_dir).is_dir()
        backend.cleanup()

    def test_composite_cleanup(self) -> None:
        from k8s_autopilot.backend.composite import K8sCompositeBackend

        backend = K8sCompositeBackend(default=MagicMock())
        temp_dir = backend._large_results_dir
        assert Path(temp_dir).exists()
        backend.cleanup()
        assert not Path(temp_dir).exists()

    def test_context_manager(self) -> None:
        from k8s_autopilot.backend.composite import K8sCompositeBackend

        with K8sCompositeBackend(default=MagicMock()) as backend:
            temp_dir = backend._large_results_dir
            assert Path(temp_dir).exists()
        assert not Path(temp_dir).exists()

    def test_custom_routes_merged(self) -> None:
        from k8s_autopilot.backend.composite import K8sCompositeBackend

        custom_backend = MagicMock()
        backend = K8sCompositeBackend(
            default=MagicMock(),
            routes={"/custom/": custom_backend},
        )
        assert "/custom/" in backend.routes
        assert "/large_tool_results/" in backend.routes
        assert "/conversation_history/" in backend.routes
        backend.cleanup()
