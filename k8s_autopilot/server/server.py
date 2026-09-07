"""A2A Server lifecycle and runner for K8s Autopilot.

Provides server startup, port management, health verification, and server process handling.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time

from k8s_autopilot.server._server_config import ServerConfig
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8000
_HEALTH_POLL_INTERVAL = 0.2
_HEALTH_TIMEOUT = 30


def _port_in_use(host: str, port: int) -> bool:
    """Check if a network port is currently in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
        else:
            return False


def _find_free_port(host: str = "127.0.0.1") -> int:
    """Find and return an available free port on the host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def get_server_url(host: str = "127.0.0.1", port: int = _DEFAULT_PORT) -> str:
    """Return the HTTP base URL for the server."""
    display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"http://{display_host}:{port}"


async def wait_for_server_health(
    url: str,
    *,
    timeout: float = _HEALTH_TIMEOUT,
    poll_interval: float = _HEALTH_POLL_INTERVAL,
) -> None:
    """Poll the server /health endpoint until it responds with 200 OK."""
    import httpx

    health_url = f"{url.rstrip('/')}/health"
    start_time = time.monotonic()
    last_exc: Exception | None = None
    last_status: int | None = None

    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() - start_time < timeout:
            try:
                resp = await client.get(health_url)
                if resp.status_code == 200:
                    logger.info("Server is healthy at %s", url)
                    return
                last_status = resp.status_code
            except (httpx.TransportError, OSError) as exc:
                last_exc = exc

            await asyncio.sleep(poll_interval)

    msg = f"Server at {url} did not become healthy within {timeout}s"
    if last_status is not None:
        msg += f" (last status: {last_status})"
    elif last_exc is not None:
        msg += f" (last error: {last_exc})"
    raise RuntimeError(msg)


def run_server(
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    *,
    reload: bool = False,
    config: ServerConfig | None = None,
) -> None:
    """Run the K8s Autopilot FastAPI / A2A server synchronously with uvicorn."""
    import uvicorn

    from k8s_autopilot.server.app import create_app

    if config is not None:
        # Populate environment variables from config
        for k, v in config.to_env().items():
            os.environ[k] = v

    app = create_app()
    logger.info("Starting K8s Autopilot A2A Server on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, reload=reload)


if __name__ == "__main__":
    cfg = ServerConfig.from_env()
    run_server(host=cfg.host, port=cfg.port)
