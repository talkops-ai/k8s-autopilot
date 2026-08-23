import os
from unittest import mock
from k8s_autopilot.core.sandbox.config import SandboxConfig

def test_sandbox_config_defaults():
    # Clear environment variables to test defaults
    with mock.patch.dict(os.environ, {}, clear=True):
        config = SandboxConfig.from_env()
        assert config.provider == "local"
        assert config.sandbox_id is None
        assert config.image is None
        assert config.setup_script_path is None
        assert config.snapshot_name is None
        assert config.sync_workspace is True

def test_sandbox_config_custom():
    custom_env = {
        "K8S_SANDBOX_PROVIDER": "langsmith",
        "K8S_SANDBOX_ID": "sb-12345",
        "K8S_SANDBOX_IMAGE": "custom-k8s:v1",
        "K8S_SANDBOX_SETUP_SCRIPT": "/opt/setup.sh",
        "K8S_SANDBOX_SNAPSHOT": "k8s-blueprint",
        "K8S_SANDBOX_SYNC_WORKSPACE": "false",
    }
    with mock.patch.dict(os.environ, custom_env, clear=True):
        config = SandboxConfig.from_env()
        assert config.provider == "langsmith"
        assert config.sandbox_id == "sb-12345"
        assert config.image == "custom-k8s:v1"
        assert config.setup_script_path == "/opt/setup.sh"
        assert config.snapshot_name == "k8s-blueprint"
        assert config.sync_workspace is False
