"""Unit tests for MCP environment variable resolution engine."""

from __future__ import annotations

import os
import pytest

from k8s_autopilot.mcp.config import resolve_mcp_server_env, _interpolate_env


def test_interpolate_env_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMETHEUS_HOST", "prometheus.internal")
    monkeypatch.setenv("PROMETHEUS_PORT", "9090")

    result = _interpolate_env(
        "http://${PROMETHEUS_HOST}:${PROMETHEUS_PORT}/api",
        field="url",
    )
    assert result == "http://prometheus.internal:9090/api"


def test_interpolate_env_default_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNSET_VAR", raising=False)
    monkeypatch.setenv("EMPTY_VAR", "")

    # Unset with default
    result1 = _interpolate_env("${UNSET_VAR:-http://localhost:8080}", field="url")
    assert result1 == "http://localhost:8080"

    # Empty with default
    result2 = _interpolate_env("${EMPTY_VAR:-fallback_value}", field="env.KEY")
    assert result2 == "fallback_value"


def test_interpolate_env_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRITICAL_TOKEN", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        _interpolate_env("Bearer ${CRITICAL_TOKEN}", field="headers.Authorization")

    assert "references unset env var CRITICAL_TOKEN" in str(exc_info.value)


def test_interpolate_env_malformed_raises() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        _interpolate_env("http://${UNCLOSED_URL/api", field="url")
    assert "malformed" in str(exc_info.value)


def test_interpolate_env_passthrough_bare_dollar() -> None:
    # Bare $ and unbraced $VAR should pass through untouched
    result = _interpolate_env("echo $MY_VAR and literal $5.00", field="command")
    assert result == "echo $MY_VAR and literal $5.00"


def test_resolve_mcp_server_env_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret123")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    raw_config = {
        "transport": "http",
        "url": "http://${MCP_HOST}:8080/mcp",
        "command": "python",
        "args": ["-m", "server", "--level", "${LOG_LEVEL:-INFO}"],
        "headers": {
            "Authorization": "Bearer ${MCP_AUTH_TOKEN}",
            "X-Tenant": "${TENANT_ID:-default-tenant}",
        },
        "env": {
            "DEBUG": "${DEBUG:-false}",
            "HOST": "${MCP_HOST}",
        },
        "enabled": True,
    }

    resolved = resolve_mcp_server_env("test-server", raw_config)
    assert resolved["url"] == "http://127.0.0.1:8080/mcp"
    assert resolved["args"] == ["-m", "server", "--level", "DEBUG"]
    assert resolved["headers"]["Authorization"] == "Bearer secret123"
    assert resolved["headers"]["X-Tenant"] == "default-tenant"
    assert resolved["env"]["DEBUG"] == "false"
    assert resolved["env"]["HOST"] == "127.0.0.1"
    assert resolved["enabled"] is True


def test_resolve_mcp_server_env_type_errors() -> None:
    # args must be list
    with pytest.raises(TypeError) as exc:
        resolve_mcp_server_env("bad-server", {"args": "not-a-list"})
    assert "args must be a list" in str(exc.value)

    # headers must be dict
    with pytest.raises(TypeError) as exc:
        resolve_mcp_server_env("bad-server", {"headers": "not-a-dict"})
    assert "headers must be a dictionary" in str(exc.value)
