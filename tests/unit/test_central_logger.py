"""Unit tests for Central Logger architecture and onboarding."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from k8s_autopilot.config.settings import Settings, reload_from_store
from k8s_autopilot.utils.logger import (
    AgentLogger,
    _ColorFormatter,
    _JsonFormatter,
    _PlainFormatter,
    configure_logging,
    get_logger,
)


def test_get_logger_singleton_and_prefix_normalization():
    """get_logger should strip 'k8s_autopilot.' prefix and return cached instance."""
    logger1 = get_logger("k8s_autopilot.server.executor")
    logger2 = get_logger("server.executor")

    assert logger1 is logger2
    assert logger1.agent_name == "server.executor"
    assert isinstance(logger1, AgentLogger)
    assert logger1.name == "server.executor"


def test_agent_logger_stdlib_compatibility():
    """AgentLogger should support standard logging formatting and properties."""
    logger = get_logger("test_compat_module")

    # Property accessors
    assert isinstance(logger.level, int)
    assert isinstance(logger.handlers, list)
    assert logger.isEnabledFor(logging.INFO) or logger.isEnabledFor(logging.ERROR)

    # setLevel support
    logger.setLevel("DEBUG")
    assert logger.level == logging.DEBUG
    assert logger.isEnabledFor(logging.DEBUG)


def test_structured_json_formatting(capsys):
    """Log emit in JSON mode should output parseable JSON with metadata."""
    logger = get_logger("test_json_module")
    configure_logging(level="INFO", mode="json", to_console=True, to_file=False)

    logger.info(
        "Cluster sync completed: %s",
        "production",
        task_id="task-999",
        context_id="ctx-42",
        extra={"duration_ms": 123.45, "status": "ok"},
    )

    captured = capsys.readouterr().err.strip()
    # Find last line if multiple lines were emitted
    last_line = captured.splitlines()[-1]
    record = json.loads(last_line)

    assert record["level"] == "INFO"
    assert record["agent"] == "test_json_module"
    assert record["message"] == "Cluster sync completed: production"
    assert record["task_id"] == "task-999"
    assert record["context_id"] == "ctx-42"
    assert record["duration_ms"] == 123.45
    assert record["status"] == "ok"
    assert "timestamp" in record


def test_structured_json_formatting_with_kwargs(capsys):
    """Arbitrary kwargs should be cleanly captured in structured extra."""
    logger = get_logger("test_kwargs_module")
    configure_logging(level="INFO", mode="json", to_console=True, to_file=False)

    logger.warning(
        "Resource limit approaching",
        task_id="t-1",
        cluster="us-east-1",
        cpu_pct=92.5,
    )

    captured = capsys.readouterr().err.strip()
    last_line = captured.splitlines()[-1]
    record = json.loads(last_line)

    assert record["level"] == "WARNING"
    assert record["task_id"] == "t-1"
    assert record["cluster"] == "us-east-1"
    assert record["cpu_pct"] == 92.5


def test_color_and_plain_formatters():
    """Color formatter should have ANSI codes, plain formatter should not."""
    record = logging.LogRecord(
        name="agent.test",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg="Test failure",
        args=(),
        exc_info=None,
    )
    record.agent_name = "test_agent"
    record._structured_extra = {"task_id": "task-abc", "code": 500}

    color_formatter = _ColorFormatter()
    plain_formatter = _PlainFormatter()

    color_output = color_formatter.format(record)
    plain_output = plain_formatter.format(record)

    # Color output has escape character
    assert "\033[" in color_output
    assert "task_id=task-abc" in color_output
    assert "code=500" in color_output

    # Plain output does not have ANSI escape codes
    assert "\033[" not in plain_output
    assert "task_id=task-abc" in plain_output
    assert "code=500" in plain_output


def test_dynamic_configure_logging_hot_reload():
    """configure_logging should dynamically update log level and formatters across active loggers."""
    logger1 = get_logger("dyn_logger_1")
    logger2 = get_logger("dyn_logger_2")

    # Set to ERROR text
    configure_logging(level="ERROR", mode="text", to_console=True, to_file=False)
    assert logger1.level == logging.ERROR
    assert logger2.level == logging.ERROR
    assert any(isinstance(h.formatter, _ColorFormatter) for h in logger1.handlers)

    # Hot reload to DEBUG json
    configure_logging(level="DEBUG", mode="json", to_console=True, to_file=False)
    assert logger1.level == logging.DEBUG
    assert logger2.level == logging.DEBUG
    assert any(isinstance(h.formatter, _JsonFormatter) for h in logger1.handlers)


@pytest.mark.asyncio
async def test_reload_from_store_triggers_configure_logging(monkeypatch):
    """reload_from_store should call configure_logging with new settings."""
    logger = get_logger("store_reload_test")

    mock_store = MagicMock()
    # Mock resolve to return DEBUG and json
    async def mock_resolve(opt):
        if opt.settings_field == "log_level":
            return "DEBUG", "test"
        if opt.settings_field == "log_mode":
            return "json", "test"
        return opt.default, "test"

    mock_store.resolve = mock_resolve

    await reload_from_store(mock_store)

    assert logger.level == logging.DEBUG
    assert any(isinstance(h.formatter, _JsonFormatter) for h in logger.handlers)
