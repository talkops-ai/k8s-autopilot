"""
Checkpointer configuration and management for HITL.

Supports both PostgreSQL (production) and MemorySaver (development) checkpointers.
Uses AsyncPostgresSaver for async FastAPI/Starlette backends with proper
psycopg connection pool lifecycle management.

Required psycopg flags (per LangGraph official docs):
  - autocommit=True  — mandatory for DDL commits during .setup()
  - row_factory=dict_row — mandatory to prevent TypeError on dict-style row access

Security:
  - LANGGRAPH_STRICT_MSGPACK=true should be set in the environment
    to enforce safe MessagePack deserialization (prevents RCE on a
    compromised database).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Optional

from langgraph.checkpoint.memory import MemorySaver

from k8s_autopilot.config.config import Config
from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver as _AsyncPostgresSaver
    from langgraph.checkpoint.postgres import PostgresSaver as _PostgresSaver
    AsyncPostgresSaverType = _AsyncPostgresSaver
    PostgresSaverType = _PostgresSaver
else:
    AsyncPostgresSaverType = Any
    PostgresSaverType = Any

# Optional async PostgreSQL checkpointer imports
try:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    ASYNC_POSTGRES_AVAILABLE = True
except ImportError:
    AsyncPostgresSaver = None  # type: ignore[assignment,misc]
    ASYNC_POSTGRES_AVAILABLE = False

# Optional sync PostgreSQL checkpointer import (kept for backward compat)
try:
    from langgraph.checkpoint.postgres import PostgresSaver

    POSTGRES_AVAILABLE = True
except ImportError:
    PostgresSaver = None  # type: ignore[assignment,misc]
    POSTGRES_AVAILABLE = False

hitl_logger = AgentLogger("k8sAutopilotHITL")

# ---------------------------------------------------------------------------
# Module-level pool reference for lifecycle management
# ---------------------------------------------------------------------------
_async_pool: object | None = None  # psycopg_pool.AsyncConnectionPool
_async_checkpointer_instance: Optional[AsyncPostgresSaverType] = None


def get_database_uri(config: Optional[Config] = None) -> Optional[str]:
    """Get database URI from configuration.

    Precedence: Config attribute → POSTGRES_URI env → DATABASE_URI env.
    """
    if config is None:
        config = Config()

    db_uri = getattr(config, "POSTGRES_URI", None)

    if not db_uri:
        db_uri = os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URI")

    return db_uri


# ---------------------------------------------------------------------------
# Async checkpointer (preferred for production)
# ---------------------------------------------------------------------------


async def create_async_checkpointer(
    config: Optional[Config] = None,
    database_uri: Optional[str] = None,
) -> Optional[AsyncPostgresSaverType]:  # type: ignore[type-arg]
    """Create an async PostgreSQL checkpointer with proper connection pool.

    Returns ``None`` if prerequisites are missing or setup fails — the
    caller should fall back to ``MemorySaver``.

    The connection pool is stored at module level so it can be closed
    cleanly during server shutdown via :func:`close_async_pool`.
    """
    global _async_pool, _async_checkpointer_instance  # noqa: PLW0603

    if _async_checkpointer_instance is not None:
        return _async_checkpointer_instance

    if not ASYNC_POSTGRES_AVAILABLE or AsyncPostgresSaver is None:
        hitl_logger.warning(
            "AsyncPostgresSaver not available (langgraph-checkpoint-postgres not installed)",
            extra={"fallback": "MemorySaver"},
        )
        return None

    db_uri = database_uri or get_database_uri(config)
    if not db_uri:
        hitl_logger.info(
            "No POSTGRES_URI configured — skipping async checkpointer",
        )
        return None

    try:
        from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

        hitl_logger.info(
            "Creating AsyncPostgresSaver with connection pool",
            extra={"has_uri": True},
        )

        # Build the async connection pool with mandatory flags.
        # autocommit=True  → required for DDL commits during setup()
        # row_factory=dict_row → required for dict-style row access
        from psycopg import AsyncConnection  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415

        pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            conninfo=db_uri,
            max_size=20,
            open=False,  # Explicit open() below — avoids deprecation warning
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
            },
        )

        # Open the pool (establishes initial connections)
        await pool.open()

        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer # noqa: PLC0415
        
        serde = JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("k8s_autopilot.core.state.base", "SupervisorWorkflowState"),
            ]
        )
        # Create checkpointer from the pool
        checkpointer = AsyncPostgresSaver(pool, serde=serde)  # type: ignore[arg-type]

        # Initialize checkpoint tables (idempotent — safe to call on every start)
        await checkpointer.setup()

        # Store pool reference for lifecycle management
        _async_pool = pool
        _async_checkpointer_instance = checkpointer

        hitl_logger.info(
            "AsyncPostgresSaver ready — checkpoint tables initialized",
            extra={"pool_max_size": 20},
        )
        return checkpointer

    except Exception as exc:
        hitl_logger.error(
            f"Failed to create AsyncPostgresSaver: {exc}",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        # Clean up pool if it was partially created
        if _async_pool is not None:
            try:
                await _async_pool.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            _async_pool = None
            _async_checkpointer_instance = None
        return None


async def get_async_checkpointer(
    config: Optional[Config] = None,
) -> Optional[AsyncPostgresSaverType]:  # type: ignore[type-arg]
    """Get an async checkpointer if PostgreSQL is configured.

    Returns ``None`` if PostgreSQL is not available or not configured,
    signaling the caller to keep its existing ``MemorySaver``.
    """
    db_uri = get_database_uri(config)
    if not db_uri:
        return None
    return await create_async_checkpointer(config=config, database_uri=db_uri)


def get_async_pool() -> object | None:
    """Return the module-level async connection pool."""
    return _async_pool


async def close_async_pool() -> None:
    """Close the module-level async connection pool.

    Call this during server shutdown (in the Starlette lifespan) to
    release all PostgreSQL connections cleanly.
    """
    global _async_pool  # noqa: PLW0603
    if _async_pool is not None:
        hitl_logger.info("Closing PostgreSQL async connection pool")
        try:
            await _async_pool.close()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            hitl_logger.warning(
                f"Error closing async pool: {exc}",
                extra={"error": str(exc)},
            )
        finally:
            _async_pool = None


# ---------------------------------------------------------------------------
# Sync checkpointer (development fallback / backward compatibility)
# ---------------------------------------------------------------------------


def create_checkpointer(
    checkpointer_type: str = "memory",
    config: Optional[Config] = None,
    database_uri: Optional[str] = None,
    auto_setup: bool = True,
) -> BaseCheckpointSaver:
    """Create a sync checkpointer instance.

    For production async usage, prefer :func:`get_async_checkpointer`.
    This function is retained for backward compatibility and for contexts
    where only a sync checkpointer is appropriate.
    """
    if checkpointer_type == "memory":
        hitl_logger.info(
            "Creating MemorySaver checkpointer (development mode)",
            extra={"checkpointer_type": "memory"},
        )
        return MemorySaver()

    if checkpointer_type == "postgres":
        if not POSTGRES_AVAILABLE or PostgresSaver is None:
            hitl_logger.warning(
                "PostgreSQL checkpointer not available, falling back to MemorySaver",
                extra={
                    "checkpointer_type": "postgres",
                    "fallback": "memory",
                    "reason": "langgraph.checkpoint.postgres not installed",
                },
            )
            return MemorySaver()

        db_uri = database_uri or get_database_uri(config)
        if not db_uri:
            hitl_logger.warning(
                "No database URI found, falling back to MemorySaver",
                extra={"checkpointer_type": "postgres", "fallback": "memory"},
            )
            return MemorySaver()

        try:
            hitl_logger.info(
                "Creating PostgresSaver checkpointer",
                extra={
                    "checkpointer_type": "postgres",
                    "has_uri": bool(db_uri),
                    "auto_setup": auto_setup,
                },
            )
            cm = PostgresSaver.from_conn_string(db_uri)
            checkpointer = cm.__enter__()  # type: ignore[union-attr]

            if auto_setup:
                checkpointer.setup()
                hitl_logger.info(
                    "PostgreSQL checkpointer tables initialized",
                    extra={"checkpointer_type": "postgres"},
                )

            return checkpointer

        except Exception as exc:
            hitl_logger.error(
                f"Failed to create PostgreSQL checkpointer: {exc}",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            hitl_logger.warning(
                "Falling back to MemorySaver due to PostgreSQL error",
                extra={"checkpointer_type": "memory"},
            )
            return MemorySaver()

    msg = f"Unknown checkpointer type: {checkpointer_type}"
    raise ValueError(msg)


def get_checkpointer(
    config: Optional[Config] = None,
    prefer_postgres: bool = True,
) -> BaseCheckpointSaver:
    """Get a sync checkpointer based on configuration.

    This is a **sync-only** convenience function.  For the recommended
    async production path, use :func:`get_async_checkpointer` instead.
    """
    if prefer_postgres:
        db_uri = get_database_uri(config)
        if db_uri:
            return create_checkpointer(
                checkpointer_type="postgres",
                config=config,
                database_uri=db_uri,
            )

    return create_checkpointer(checkpointer_type="memory", config=config)
