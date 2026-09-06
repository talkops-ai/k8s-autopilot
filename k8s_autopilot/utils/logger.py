"""
Logging module for K8s Autopilot Agent.

Provides color-coded, multi-sink (console + file + websocket) logging with
both structured (JSON) and human-readable output modes.

Configuration is driven by ConfigStore and Settings. Every key is
resolved lazily so the logger never triggers a circular import.

Usage::

    from k8s_autopilot.utils.logger import get_logger

    logger = get_logger(__name__)
    logger.info("Module created", extra={"chart": "nginx", "files": 5})
    logger.warning("Drift detected", task_id="deploy-42")
    logger.error("Validation failed", extra={"exit_code": 1})
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional

from colorama import Fore, Style


# ---------------------------------------------------------------------------
# Color palettes
# ---------------------------------------------------------------------------

class _LevelColor(Enum):
    """Traffic-light colors for log levels."""
    DEBUG = Fore.LIGHTBLACK_EX
    INFO = Fore.BLUE
    WARNING = Fore.YELLOW
    ERROR = Fore.RED
    CRITICAL = Fore.LIGHTRED_EX


# ---------------------------------------------------------------------------
# Singleton config accessor (lazy — avoids circular imports)
# ---------------------------------------------------------------------------

_config_cache: Optional[Any] = ...  # sentinel: `...` means "not loaded yet"


def _get_config() -> Any:
    """Lazy-load settings once. Returns the Settings instance or None."""
    global _config_cache
    if _config_cache is ...:
        try:
            from k8s_autopilot.config.settings import get_settings

            _config_cache = get_settings()
        except Exception:
            _config_cache = None
    return _config_cache


def _cfg(key: str, fallback: Any) -> Any:
    """Read a single config value, falling back if Settings isn't available."""
    cfg = _get_config()
    if cfg is None:
        return fallback
    return getattr(cfg, key.lower(), getattr(cfg, key, fallback))


# ---------------------------------------------------------------------------
# Colored console formatter (for StreamHandler)
# ---------------------------------------------------------------------------

class _ColorFormatter(logging.Formatter):
    """
    Applies per-level color to log records for console output.

    Reads ``LOG_DATE_FORMAT`` from config for timestamp formatting.
    Keeps ANSI codes OUT of the ``logging.FileHandler`` path automatically
    because only the ``StreamHandler`` uses this formatter.
    """

    def __init__(self) -> None:
        super().__init__()
        self._date_fmt: str = _cfg("LOG_DATE_FORMAT", "%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        agent = getattr(record, "agent_name", "BASE")

        # Only level gets color — agent name stays neutral
        try:
            lc = _LevelColor[level].value
        except KeyError:
            lc = Fore.WHITE

        ts = datetime.now(timezone.utc).strftime(self._date_fmt)
        msg = record.getMessage()

        extras = getattr(record, "_structured_extra", None)
        if extras:
            extra_pieces = [f"{k}={v}" for k, v in extras.items()]
            extra_str = " | " + " ".join(extra_pieces)
        else:
            extra_str = ""

        formatted = (
            f"{lc}[{level}]{Style.RESET_ALL} "
            f"{agent}: "
            f"[{ts}] {msg}{extra_str}"
        )
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                formatted = formatted.rstrip() + "\n" + record.exc_text
        return formatted


# ---------------------------------------------------------------------------
# Structured (JSON) formatter (for both file and console when enabled)
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Emits each log record as a single JSON line (structured logging)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "agent": getattr(record, "agent_name", None),
            "message": record.getMessage(),
        }
        # Merge structured extras (task_id, context_id, turn_id, custom fields)
        extras = getattr(record, "_structured_extra", None)
        if extras:
            entry.update(extras)
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                entry["exception"] = record.exc_text
        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------------
# File formatter (plain text, no color codes)
# ---------------------------------------------------------------------------

class _PlainFormatter(logging.Formatter):
    """
    Plain-text format for file output — no ANSI escape codes.

    Respects ``LOG_FORMAT`` and ``LOG_DATE_FORMAT`` from config.
    """

    def __init__(self) -> None:
        log_fmt: str = _cfg(
            "LOG_FORMAT",
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        date_fmt: str = _cfg("LOG_DATE_FORMAT", "%Y-%m-%d %H:%M:%S")
        super().__init__(fmt=log_fmt, datefmt=date_fmt)

    def format(self, record: logging.LogRecord) -> str:
        # Inject agent_name into the record so %(name)s shows it
        record.name = getattr(record, "agent_name", record.name)
        msg = super().format(record)

        extras = getattr(record, "_structured_extra", None)
        if extras:
            extra_pieces = [f"{k}={v}" for k, v in extras.items()]
            msg += " | " + " ".join(extra_pieces)

        return msg


# ---------------------------------------------------------------------------
# Handler factory & Active Loggers Registry
# ---------------------------------------------------------------------------

_initialised_loggers: set[str] = set()
_active_loggers: dict[str, "AgentLogger"] = {}
_active_loggers_lock = threading.RLock()


def _ensure_handlers(py_logger: logging.Logger, agent_name: str) -> None:
    """
    Attach console + file handlers exactly once per logger name.

    Prevents duplicate handlers when multiple loggers share a name.
    """
    if py_logger.name in _initialised_loggers:
        return
    _initialised_loggers.add(py_logger.name)

    py_logger.handlers.clear()
    py_logger.propagate = False

    level_name: str = _cfg("LOG_LEVEL", "INFO")
    int_level = getattr(logging, level_name.upper(), logging.INFO)
    py_logger.setLevel(int_level)

    mode: str = str(_cfg("LOG_MODE", "text")).lower()
    is_json: bool = (mode == "json") or bool(_cfg("LOG_STRUCTURED_JSON", False))

    # ── Console handler ──
    if _cfg("LOG_TO_CONSOLE", True):
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(int_level)
        console.setFormatter(_JsonFormatter() if is_json else _ColorFormatter())
        py_logger.addHandler(console)

    # ── File handler ──
    if _cfg("LOG_TO_FILE", True):
        log_file: str = _cfg("LOG_FILE", "k8s_autopilot.log")
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(int_level)
            fh.setFormatter(_JsonFormatter() if is_json else _PlainFormatter())
            py_logger.addHandler(fh)
        except Exception:
            pass


def configure_logging(
    level: str | None = None,
    mode: str | None = None,
    to_console: bool | None = None,
    to_file: bool | None = None,
    file_path: str | None = None,
) -> None:
    """
    Dynamically reconfigure all active loggers from updated settings.

    Can be invoked when settings change (e.g. from ConfigStore or API)
    without restarting the server process.
    """
    global _config_cache
    _config_cache = ...  # Invalidate cached settings

    final_level_str = level if level is not None else _cfg("LOG_LEVEL", "INFO")
    final_mode_str = str(mode if mode is not None else _cfg("LOG_MODE", "text")).lower()
    final_to_console = to_console if to_console is not None else _cfg("LOG_TO_CONSOLE", True)
    final_to_file = to_file if to_file is not None else _cfg("LOG_TO_FILE", True)
    final_file_path = file_path if file_path is not None else _cfg("LOG_FILE", "k8s_autopilot.log")

    numeric_level = getattr(logging, str(final_level_str).upper(), logging.INFO)
    is_json = (final_mode_str == "json") or (mode is None and bool(_cfg("LOG_STRUCTURED_JSON", False)))

    with _active_loggers_lock:
        for agent_logger in _active_loggers.values():
            py_logger = agent_logger._logger
            py_logger.setLevel(numeric_level)

            # Close and clear existing handlers
            for h in list(py_logger.handlers):
                if isinstance(h, logging.FileHandler):
                    try:
                        h.close()
                    except Exception:
                        pass
            py_logger.handlers.clear()

            # Re-attach console handler
            if final_to_console:
                console = logging.StreamHandler(sys.stderr)
                console.setLevel(numeric_level)
                console.setFormatter(_JsonFormatter() if is_json else _ColorFormatter())
                py_logger.addHandler(console)

            # Re-attach file handler
            if final_to_file:
                try:
                    fh = logging.FileHandler(final_file_path, encoding="utf-8")
                    fh.setLevel(numeric_level)
                    fh.setFormatter(_JsonFormatter() if is_json else _PlainFormatter())
                    py_logger.addHandler(fh)
                except Exception:
                    pass


def get_logger(name: str = "k8s_autopilot") -> AgentLogger:
    """Return an AgentLogger for the specified module or component name.

    Normalizes module names by stripping the `k8s_autopilot.` prefix.

    Usage::

        from k8s_autopilot.utils.logger import get_logger
        logger = get_logger(__name__)
    """
    short_name = name
    if short_name.startswith("k8s_autopilot."):
        short_name = short_name.replace("k8s_autopilot.", "", 1)

    with _active_loggers_lock:
        if short_name not in _active_loggers:
            _active_loggers[short_name] = AgentLogger(short_name)
        return _active_loggers[short_name]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class AgentLogger:
    """
    Per-agent logger with color-coded console output, structured JSON support,
    file logging, and optional websocket streaming.

    Usage::

        log = AgentLogger("tf-validator")
        log.info("Starting validation")
        log.error("terraform validate failed", extra={"exit_code": 1})
        log.warning("Drift detected", task_id="deploy-42", context_id="sess-abc")
    """

    __slots__ = ("agent_name", "_logger", "_websocket", "_stream_fn")

    def __init__(self, agent_name: str = "BASE") -> None:
        self.agent_name = agent_name
        self._logger = logging.getLogger(f"agent.{agent_name}")
        self._websocket: Any = None
        self._stream_fn: Optional[Callable[..., Any]] = None

        _ensure_handlers(self._logger, agent_name)

        with _active_loggers_lock:
            _active_loggers[agent_name] = self

    # ── Compatibility Properties & Methods ──────────────────────────────

    @property
    def level(self) -> int:
        return self._logger.level

    @property
    def handlers(self) -> list[logging.Handler]:
        return self._logger.handlers

    @property
    def name(self) -> str:
        return self.agent_name

    def setLevel(self, level: int | str) -> None:
        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)
        self._logger.setLevel(level)

    def isEnabledFor(self, level: int) -> bool:
        return self._logger.isEnabledFor(level)

    def addHandler(self, hdlr: logging.Handler) -> None:
        self._logger.addHandler(hdlr)

    def removeHandler(self, hdlr: logging.Handler) -> None:
        self._logger.removeHandler(hdlr)

    # ── Convenience level methods (sync — suitable for most call-sites) ──

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {' '.join(str(a) for a in args)}"
        self._emit(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {' '.join(str(a) for a in args)}"
        self._emit(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {' '.join(str(a) for a in args)}"
        self._emit(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {' '.join(str(a) for a in args)}"
        self._emit(logging.ERROR, msg, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR level with exc_info attached (like stdlib)."""
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {' '.join(str(a) for a in args)}"
        kwargs.setdefault("exc_info", True)
        self._emit(logging.ERROR, msg, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {' '.join(str(a) for a in args)}"
        self._emit(logging.CRITICAL, msg, **kwargs)

    def log(self, level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = f"{msg} {' '.join(str(a) for a in args)}"
        self._emit(level, msg, **kwargs)

    # ── Core emit (unified path for ALL log output) ──────────────────────

    def _emit(
        self,
        level: int,
        message: str,
        *,
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
        exc_info: Any = None,
        **kwargs: Any,
    ) -> None:
        """Single code-path that feeds console, file, and websocket."""
        if exc_info:
            if exc_info is True:
                import sys
                exc_info = sys.exc_info()
            elif isinstance(exc_info, BaseException):
                exc_info = (type(exc_info), exc_info, exc_info.__traceback__)

        record = self._logger.makeRecord(
            name=self._logger.name,
            level=level,
            fn="",
            lno=0,
            msg=message,
            args=(),
            exc_info=exc_info,
        )
        # Attach agent identity + structured extras to the record
        record.agent_name = self.agent_name  # type: ignore[attr-defined]
        structured_extra: dict[str, Any] = {}
        if task_id:
            structured_extra["task_id"] = task_id
        if context_id:
            structured_extra["context_id"] = context_id
        if turn_id:
            structured_extra["turn_id"] = turn_id
        if extra:
            structured_extra.update(extra)
        if kwargs:
            structured_extra.update(kwargs)
        record._structured_extra = structured_extra  # type: ignore[attr-defined]

        self._logger.handle(record)

    # ── Backward-compat: log_structured() ────────────────────────────────

    def log_structured(
        self,
        level: str = "INFO",
        message: str = "",
        task_id: Optional[str] = None,
        context_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Backward-compatible structured log call.

        Prefer ``log.info(...)``, ``log.error(...)`` etc. for new code.
        """
        py_level = getattr(logging, level.upper(), logging.INFO)
        self._emit(py_level, message, task_id=task_id, context_id=context_id, extra=extra)

    # ── Async log (for websocket streaming) ──────────────────────────────

    async def alog(
        self,
        message: str,
        level: str = "INFO",
        **kwargs: Any,
    ) -> None:
        """Async variant that also pushes to websocket if configured."""
        py_level = getattr(logging, level.upper(), logging.INFO)
        self._emit(py_level, message, **kwargs)

        # Websocket push (fire-and-forget)
        if self._websocket and self._stream_fn:
            try:
                await self._stream_fn("logs", self.agent_name, message, self._websocket)
            except Exception:
                pass  # never let WS failure crash the agent

    def set_websocket(self, websocket: Any, stream_fn: Callable[..., Any]) -> None:
        """Attach a websocket sink for live streaming."""
        self._websocket = websocket
        self._stream_fn = stream_fn

    # ── Factory ──────────────────────────────────────────────────────────

    @classmethod
    def create(cls, agent_name: str) -> "AgentLogger":
        """Factory method (alias for constructor)."""
        return cls(agent_name)

    # Keep old name for backward compat
    create_logger = create

    def __repr__(self) -> str:
        return f"AgentLogger({self.agent_name!r})"


# Global AgentLogger for decorator logs (backward compat)
_decorator_logger = AgentLogger("DECORATOR")


def log_sync(func: Callable) -> Callable:
    """No-op decorator - structured logging is used instead."""
    return func


def log_async(func: Callable) -> Callable:
    """No-op decorator - structured logging is used instead."""
    return func


__all__ = [
    "AgentLogger",
    "get_logger",
    "configure_logging",
    "log_sync",
    "log_async",
]