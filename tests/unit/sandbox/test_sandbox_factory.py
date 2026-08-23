import os
import pytest
from unittest import mock
from pathlib import Path
from k8s_autopilot.core.sandbox.factory import (
    get_sandbox_backend,
    _active_sandboxes,
    _active_providers,
)
from deepagents.backends import LocalShellBackend

@pytest.fixture(autouse=True)
def clear_caches():
    """Clear caches before and after each test."""
    _active_sandboxes.clear()
    _active_providers.clear()
    yield
    _active_sandboxes.clear()
    _active_providers.clear()

def test_get_local_sandbox():
    with mock.patch.dict(os.environ, {"K8S_SANDBOX_PROVIDER": "local"}, clear=True):
        backend = get_sandbox_backend()
        assert isinstance(backend, LocalShellBackend)

def test_sandbox_caching():
    # Mock langsmith creation calls
    mock_backend = mock.MagicMock()
    mock_backend.id = "mock-sb-id"
    
    with mock.patch("k8s_autopilot.core.sandbox.factory._create_langsmith_sandbox") as mock_create, \
         mock.patch("k8s_autopilot.core.sandbox.factory._sync_workspace_to_sandbox") as mock_sync:
        mock_create.return_value = mock_backend
        
        env = {
            "K8S_SANDBOX_PROVIDER": "langsmith",
            "K8S_SANDBOX_SYNC_WORKSPACE": "true",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            # 1. Fetch backend for thread_a
            backend_1 = get_sandbox_backend(thread_id="thread_a")
            assert backend_1 == mock_backend
            mock_create.assert_called_once()
            mock_sync.assert_called_once_with(mock_backend, "langsmith")
            
            # 2. Fetch backend again for thread_a (should be cached)
            backend_2 = get_sandbox_backend(thread_id="thread_a")
            assert backend_2 == mock_backend
            # creation should NOT be called again
            assert mock_create.call_count == 1
            
            # 3. Fetch for thread_b (should trigger new creation)
            mock_create.reset_mock()
            backend_3 = get_sandbox_backend(thread_id="thread_b")
            assert backend_3 == mock_backend
            mock_create.assert_called_once()
