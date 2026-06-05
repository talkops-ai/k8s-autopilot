import pytest
import os
from k8s_autopilot.config.config import Config
from k8s_autopilot.utils.mcp_client import create_mcp_client

@pytest.mark.sandbox
@pytest.mark.asyncio
async def test_helm_install_vcluster():
    # Only run if explicitly testing sandbox or running all
    if not os.environ.get("RUN_SANDBOX"):
        pytest.skip("Skipping sandbox test unless RUN_SANDBOX is set")
        
    config = Config()
    async with create_mcp_client(config, server_filter=["helm_mcp_server"]) as mcp_client:
        try:
            # Install
            res = await mcp_client.execute_tool(
                "helm_mcp_server",
                "helm_install_chart", 
                {"chart_name": "bitnami/nginx", "release_name": "sbx-install-test", "namespace": "default"}
            )
            res_str = str(res).lower()
            assert "success" in res_str or "deployed" in res_str or "status: deployed" in res_str
        finally:
            # Cleanup
            try:
                await mcp_client.execute_tool(
                    "helm_mcp_server",
                    "helm_uninstall_release",
                    {"release_name": "sbx-install-test", "namespace": "default"}
                )
            except Exception:
                pass
