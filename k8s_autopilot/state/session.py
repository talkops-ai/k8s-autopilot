"""Thread and session management using LangGraph checkpoint persistence.

Supports dual-backend persistence:
- SQLite (default local mode: ~/.k8s_autopilot/.state/sessions.db)
- PostgreSQL (cloud / enterprise multi-tenant: POSTGRES_URI / DATABASE_URL)

Provides:
- ``ThreadInfo`` TypedDict
- ``list_threads()`` with covering SQLite index and PostgreSQL JSONB queries
- ``get_thread_messages()`` with DeltaChannel and writes reconstruction
- ``delete_thread()`` with cache cleanup across backends
- ``generate_thread_id()`` using UUID7 (time-ordered)
- ``_patch_aiosqlite()`` for LangGraph >=2.1.0 compatibility
- ``SessionManager`` class for multi-backend thread operations
- ``create_checkpointer()`` and ``get_checkpointer()`` (auto PostgreSQL -> SQLite)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
import contextlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, NamedTuple, NotRequired, TypedDict, cast
import uuid

from k8s_autopilot.middleware.goal_state_notice import is_internal_message

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import aiosqlite
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_aiosqlite_patched = False
_jsonplus_serializer: JsonPlusSerializer | None = None
_message_count_cache: dict[str, tuple[str | None, int]] = {}
_MAX_MESSAGE_COUNT_CACHE = 4096
_initial_prompt_cache: dict[str, tuple[str | None, str | None]] = {}
_MAX_INITIAL_PROMPT_CACHE = 4096
_recent_threads_cache: dict[tuple[str | None, int], list[ThreadInfo]] = {}
_MAX_RECENT_THREADS_CACHE_KEYS = 16


def get_active_backend() -> str:
    """Return 'postgres' if configured, otherwise 'sqlite'."""
    for key in ("CHECKPOINTER_BACKEND", "CHECKPOINT_BACKEND"):
        backend = os.environ.get(key, "").strip().lower()
        if backend in ("postgres", "sqlite"):
            return backend
    if os.environ.get("K8S_AUTOPILOT_POSTGRES_URI") or os.environ.get("POSTGRES_URI"):
        return "postgres"
    return "sqlite"


def get_postgres_uri() -> str:
    """Get the PostgreSQL URI from environment variables."""
    return (
        os.environ.get("POSTGRES_URI")
        or os.environ.get("K8S_AUTOPILOT_POSTGRES_URI")
        or os.environ.get("DATABASE_URL", "")
    )


def _patch_aiosqlite() -> None:
    """Patch aiosqlite.Connection with ``is_alive()`` if missing.

    Required by langgraph-checkpoint>=2.1.0.
    """
    global _aiosqlite_patched
    if _aiosqlite_patched:
        return

    import aiosqlite as _aiosqlite  # Lazy import: optional dependency

    if not hasattr(_aiosqlite.Connection, "is_alive"):

        def _is_alive(self: _aiosqlite.Connection) -> bool:
            return self._running and self._connection is not None

        _aiosqlite.Connection.is_alive = _is_alive  # type: ignore[attr-defined]

    _aiosqlite_patched = True


async def _drain_aiosqlite_worker(conn: aiosqlite.Connection) -> None:
    """Join the aiosqlite worker thread after its connection is closed."""
    worker = getattr(conn, "_thread", None)
    if worker is None or not worker.is_alive():
        return
    with contextlib.suppress(RuntimeError):
        await asyncio.to_thread(worker.join, 5.0)


_db_path: Path | None = None


def get_db_path() -> Path:
    """Get path to the sessions SQLite database."""
    global _db_path
    if _db_path is not None:
        return _db_path
    from k8s_autopilot.config.paths import (
        STATE_DIR,  # Lazy import: circular dependency avoidance
    )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _db_path = STATE_DIR / "sessions.db"
    return _db_path


@asynccontextmanager
async def _connect(db_path: Path | None = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Import aiosqlite, apply the compatibility patch, and connect to SQLite."""
    import aiosqlite as _aiosqlite  # Lazy import: optional dependency

    _patch_aiosqlite()

    target_path = db_path or get_db_path()
    conn: aiosqlite.Connection | None = None
    try:
        async with _aiosqlite.connect(str(target_path), timeout=30.0) as opened:
            conn = opened
            yield opened
    finally:
        if conn is not None:
            await _drain_aiosqlite_worker(conn)


@asynccontextmanager
async def _connect_postgres(uri: str | None = None) -> AsyncGenerator[Any, None]:
    """Connect to PostgreSQL asynchronously using psycopg."""
    import psycopg  # Lazy import: optional dependency
    from psycopg.rows import tuple_row  # Lazy import: optional dependency

    conn_uri = uri or get_postgres_uri()
    if not conn_uri:
        raise ValueError(
            "PostgreSQL checkpointer requested but no connection URI found. "
            "Set POSTGRES_URI, K8S_AUTOPILOT_POSTGRES_URI, or DATABASE_URL."
        )
    conn = await psycopg.AsyncConnection.connect(conn_uri, row_factory=tuple_row, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


class ThreadInfo(TypedDict):
    """Thread metadata returned by ``list_threads``."""

    thread_id: str
    agent_name: str | None
    updated_at: str | None
    created_at: NotRequired[str | None]
    git_branch: NotRequired[str | None]
    initial_prompt: NotRequired[str | None]
    message_count: NotRequired[int]
    latest_checkpoint_id: NotRequired[str | None]
    cwd: NotRequired[str | None]


class _CheckpointSummary(NamedTuple):
    """Checkpoint metadata extracted without full graph reconstruction."""

    created_at: str | None
    initial_prompt: str | None
    message_count: int | None
    agent_name: str | None
    cwd: str | None
    git_branch: str | None


def format_timestamp(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp string for human-readable display."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %H:%M")
    except (ValueError, TypeError):
        return ""


def format_relative_timestamp(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp string as a relative time description."""
    if not iso_timestamp:
        return ""
    try:
        clean_iso = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = now - dt

        seconds = delta.total_seconds()
        if seconds < 0:
            return "just now"
        if seconds < 60:
            return "just now"
        minutes = int(seconds // 60)
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days < 7:
            return f"{days}d ago"
        weeks = days // 7
        if weeks < 4:
            return f"{weeks}w ago"
        months = days // 30
        if months < 12:
            return f"{months}mo ago"
        years = days // 365
        return f"{years}y ago"
    except (ValueError, TypeError):
        return ""


def format_path(path: str | None) -> str:
    """Format a filesystem path for display."""
    if not path:
        return ""
    try:
        p = Path(path)
        home = Path.home()
        if p == home:
            return "~"
        if p.is_relative_to(home):
            return f"~/{p.relative_to(home)}"
        return str(p)
    except (ValueError, TypeError):
        return path


_last_uuid7_timestamp_ms = 0
_uuid7_counter = 0
_uuid7_lock = threading.Lock()


def generate_thread_id() -> str:
    """Generate a UUID7 thread ID (RFC 9562, time-ordered and monotonic)."""
    global _last_uuid7_timestamp_ms, _uuid7_counter

    with _uuid7_lock:
        nanoseconds = time.time_ns()
        timestamp_ms = nanoseconds // 1_000_000
        if timestamp_ms <= _last_uuid7_timestamp_ms:
            timestamp_ms = _last_uuid7_timestamp_ms
            _uuid7_counter = (_uuid7_counter + 1) & 0xFFF
        else:
            _last_uuid7_timestamp_ms = timestamp_ms
            _uuid7_counter = 0

        rand_bytes = os.urandom(8)
        b = bytearray(16)
        b[0:6] = timestamp_ms.to_bytes(6, byteorder="big")
        b[6] = 0x70 | ((_uuid7_counter >> 8) & 0x0F)
        b[7] = _uuid7_counter & 0xFF
        b[8] = (rand_bytes[0] & 0x3F) | 0x80
        b[9:16] = rand_bytes[1:8]

        return str(uuid.UUID(bytes=bytes(b)))


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    """Check if a table exists in the SQLite database."""
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ) as cursor:
        return bool(await cursor.fetchone())


async def _ensure_threads_list_index(conn: aiosqlite.Connection) -> None:
    """Create covering index for fast SQLite thread list queries."""
    if not await _table_exists(conn, "checkpoints"):
        return
    try:
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_checkpoints_thread_list_covering
            ON checkpoints(checkpoint_ns, thread_id, checkpoint_id DESC, type, metadata)
            """
        )
        await conn.commit()
    except sqlite3.OperationalError:
        pass


async def list_threads(
    limit: int = 10,
    offset: int = 0,
    *,
    db_path: Path | None = None,
    cwd: str | None = None,
    project_root: str | None = None,
    agent_name: str | Sequence[str] | None = None,
    git_branch: str | None = None,
    include_checkpoint_fields: bool = True,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
    sort_by: str = "updated",
    backend: str = "auto",
) -> list[ThreadInfo]:
    """List threads ordered by most recent update across SQLite or PostgreSQL."""
    if backend == "auto":
        backend = "sqlite" if db_path is not None else get_active_backend()

    if backend == "postgres":
        return await _list_threads_postgres(
            limit=limit,
            offset=offset,
            cwd=cwd,
            project_root=project_root,
            agent_name=agent_name,
            git_branch=git_branch,
            include_checkpoint_fields=include_checkpoint_fields,
            include_message_count=include_message_count,
            include_initial_prompt=include_initial_prompt,
            sort_by=sort_by,
        )
    return await _list_threads_sqlite(
        limit=limit,
        offset=offset,
        db_path=db_path,
        cwd=cwd,
        project_root=project_root,
        agent_name=agent_name,
        git_branch=git_branch,
        include_checkpoint_fields=include_checkpoint_fields,
        include_message_count=include_message_count,
        include_initial_prompt=include_initial_prompt,
        sort_by=sort_by,
    )


async def _list_threads_sqlite(
    limit: int = 10,
    offset: int = 0,
    *,
    db_path: Path | None = None,
    cwd: str | None = None,
    project_root: str | None = None,
    agent_name: str | Sequence[str] | None = None,
    git_branch: str | None = None,
    include_checkpoint_fields: bool = True,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
    sort_by: str = "updated",
) -> list[ThreadInfo]:
    """SQLite implementation of list_threads."""
    target_path = db_path or get_db_path()
    if not target_path.exists():
        return []

    async with _connect(target_path) as conn:
        if not await _table_exists(conn, "checkpoints"):
            return []

        await _ensure_threads_list_index(conn)

        where_clauses = ["checkpoint_ns = ''", "thread_id NOT LIKE '%:%'"]
        params: list[Any] = []

        if agent_name:
            agents_list = [agent_name] if isinstance(agent_name, str) else list(agent_name)
            is_k8s_autopilot = any(
                a in ("k8s-autopilot", "k8sAutopilotSupervisorAgent", "k8s_autopilot", "k8sAutopilotAgent")
                for a in agents_list
            )
            placeholders = ", ".join("?" for _ in agents_list)
            null_clause = (
                " OR (json_extract(metadata, '$.agent_name') IS NULL AND json_extract(metadata, '$.agent_id') IS NULL)"
                if is_k8s_autopilot
                else ""
            )
            where_clauses.append(
                f"(json_extract(metadata, '$.agent_name') IN ({placeholders}) "
                f"OR json_extract(metadata, '$.agent_id') IN ({placeholders}){null_clause})"
            )
            params.extend(agents_list)
            params.extend(agents_list)
        if git_branch:
            where_clauses.append("json_extract(metadata, '$.git_branch') = ?")
            params.append(git_branch)
        if cwd:
            where_clauses.append("json_extract(metadata, '$.cwd') = ?")
            params.append(cwd)

        where_sql = f"WHERE {' AND '.join(where_clauses)}"
        order_col = "created_at" if sort_by == "created" else "latest_checkpoint_id"

        query = f"""
            SELECT
                thread_id,
                MAX(COALESCE(json_extract(metadata, '$.agent_name'), json_extract(metadata, '$.agent_id'), 'k8s-autopilot')) as agent_name,
                MAX(json_extract(metadata, '$.updated_at')) as updated_at,
                MAX(checkpoint_id) as latest_checkpoint_id,
                MAX(json_extract(metadata, '$.cwd')) as cwd,
                MAX(json_extract(metadata, '$.git_branch')) as git_branch,
                MIN(json_extract(metadata, '$.updated_at')) as created_at,
                MAX(json_extract(metadata, '$.title')) as title
            FROM checkpoints
            {where_sql}
            GROUP BY thread_id
            ORDER BY {order_col} DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        async with conn.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()

        threads: list[ThreadInfo] = []
        for r in rows:
            raw_agent = r[1]
            if not raw_agent or raw_agent in ("k8sAutopilotSupervisorAgent", "k8s_autopilot", "k8sAutopilotAgent"):
                normalized_agent = "k8s-autopilot"
            else:
                normalized_agent = raw_agent

            t: ThreadInfo = {
                "thread_id": str(r[0]),
                "agent_name": normalized_agent,
                "updated_at": r[2],
                "latest_checkpoint_id": r[3],
                "cwd": r[4],
                "git_branch": r[5],
                "created_at": r[6],
            }
            if r[7]:
                t["initial_prompt"] = r[7]
            threads.append(t)

        if include_checkpoint_fields and threads:
            await _populate_checkpoint_fields_sqlite(
                conn,
                threads,
                include_message_count=include_message_count,
                include_initial_prompt=include_initial_prompt,
            )

        return threads


async def _list_threads_postgres(
    limit: int = 10,
    offset: int = 0,
    *,
    cwd: str | None = None,
    project_root: str | None = None,
    agent_name: str | Sequence[str] | None = None,
    git_branch: str | None = None,
    include_checkpoint_fields: bool = True,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
    sort_by: str = "updated",
) -> list[ThreadInfo]:
    """PostgreSQL implementation of list_threads."""
    uri = get_postgres_uri()
    if not uri:
        return []

    try:
        async with _connect_postgres(uri) as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'checkpoints'")
            if not await cur.fetchone():
                return []

            where_clauses = ["checkpoint_ns = ''", "POSITION(':' IN thread_id) = 0"]
            params: list[Any] = []

            if agent_name:
                agents_list = [agent_name] if isinstance(agent_name, str) else list(agent_name)
                is_k8s_autopilot = any(
                    a in ("k8s-autopilot", "k8sAutopilotSupervisorAgent", "k8s_autopilot", "k8sAutopilotAgent")
                    for a in agents_list
                )
                placeholders = ", ".join("%s" for _ in agents_list)
                null_clause = (
                    " OR (metadata->>'agent_name' IS NULL AND metadata->>'agent_id' IS NULL)"
                    if is_k8s_autopilot
                    else ""
                )
                where_clauses.append(
                    f"(metadata->>'agent_name' IN ({placeholders}) "
                    f"OR metadata->>'agent_id' IN ({placeholders}){null_clause})"
                )
                params.extend(agents_list)
                params.extend(agents_list)
            if git_branch:
                where_clauses.append("metadata->>'git_branch' = %s")
                params.append(git_branch)
            if cwd:
                where_clauses.append("metadata->>'cwd' = %s")
                params.append(cwd)

            where_sql = f"WHERE {' AND '.join(where_clauses)}"
            order_col = "MIN(metadata->>'updated_at')" if sort_by == "created" else "MAX(checkpoint_id)"

            query = f"""
                    SELECT
                        thread_id,
                        COALESCE(MAX(metadata->>'agent_name'), MAX(metadata->>'agent_id'), 'k8s-autopilot') as agent_name,
                        COALESCE(MAX(checkpoint->>'ts'), MAX(metadata->>'updated_at')) as updated_at,
                        MAX(checkpoint_id) as latest_checkpoint_id,
                        MAX(metadata->>'cwd') as cwd,
                        MAX(metadata->>'git_branch') as git_branch,
                        COALESCE(MIN(checkpoint->>'ts'), MIN(metadata->>'updated_at')) as created_at,
                        MAX(metadata->>'title') as title
                    FROM checkpoints
                    {where_sql}
                    GROUP BY thread_id
                    ORDER BY {order_col} DESC
                    LIMIT %s OFFSET %s
                """
            params.extend([limit, offset])
            await cur.execute(query, tuple(params))
            rows = await cur.fetchall()

            threads: list[ThreadInfo] = []
            for r in rows:
                raw_agent = r[1]
                if not raw_agent or raw_agent in ("k8sAutopilotSupervisorAgent", "k8s_autopilot", "k8sAutopilotAgent"):
                    normalized_agent = "k8s-autopilot"
                else:
                    normalized_agent = raw_agent

                t: ThreadInfo = {
                    "thread_id": str(r[0]),
                    "agent_name": normalized_agent,
                    "updated_at": r[2],
                    "latest_checkpoint_id": r[3],
                    "cwd": r[4],
                    "git_branch": r[5],
                    "created_at": r[6],
                }
                if r[7]:
                    t["initial_prompt"] = r[7]
                threads.append(t)

            if include_checkpoint_fields and threads:
                await _populate_checkpoint_fields_pg(
                    conn,
                    threads,
                    include_message_count=include_message_count,
                    include_initial_prompt=include_initial_prompt,
                )

            return threads
    except Exception as exc:
        logger.debug(f"PostgreSQL list_threads query failed: {exc}")
        return []


async def populate_thread_checkpoint_details(
    threads: list[ThreadInfo],
    *,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
    backend: str = "auto",
    db_path: Path | None = None,
) -> None:
    """Populate message count and initial prompt fields into thread dictionaries in-place."""
    if not threads:
        return

    if backend == "auto":
        backend = "sqlite" if db_path is not None else get_active_backend()

    if backend == "postgres":
        uri = get_postgres_uri()
        if uri:
            try:
                async with _connect_postgres(uri) as conn:
                    await _populate_checkpoint_fields_pg(
                        conn,
                        threads,
                        include_message_count=include_message_count,
                        include_initial_prompt=include_initial_prompt,
                    )
            except Exception as exc:
                logger.debug(f"PG populate_thread_checkpoint_details failed: {exc}")
    else:
        target_path = db_path or get_db_path()
        if not target_path.exists():
            return
        async with _connect(target_path) as conn:
            await _populate_checkpoint_fields_sqlite(
                conn,
                threads,
                include_message_count=include_message_count,
                include_initial_prompt=include_initial_prompt,
            )


async def prewarm_thread_message_counts(limit: int | None = None) -> None:
    """Populate message count cache for recent threads in the background."""
    try:
        threads = await list_threads(limit=limit or 20, include_checkpoint_fields=False)
        if threads:
            await populate_thread_checkpoint_details(threads, include_message_count=True, include_initial_prompt=False)
    except Exception as exc:
        logger.debug("Background prewarming of thread message counts skipped: %s", exc)


def get_cached_threads(project_root: str | None = None, limit: int = 10) -> list[ThreadInfo] | None:
    """Retrieve threads from in-memory cache if fresh."""
    cached = _recent_threads_cache.get((project_root, limit))
    return _copy_threads(cached) if cached is not None else None


def apply_cached_thread_message_counts(threads: list[ThreadInfo]) -> int:
    """Apply cached message counts in-place."""
    applied = 0
    for t in threads:
        cached = _message_count_cache.get(t["thread_id"])
        if cached is not None:
            t["message_count"] = cached[1]
            applied += 1
    return applied


def apply_cached_thread_initial_prompts(threads: list[ThreadInfo]) -> int:
    """Apply cached initial prompts in-place."""
    applied = 0
    for t in threads:
        cached = _initial_prompt_cache.get(t["thread_id"])
        if cached is not None:
            t["initial_prompt"] = cached[1]
            applied += 1
    return applied


async def _get_jsonplus_serializer() -> JsonPlusSerializer:
    global _jsonplus_serializer
    if _jsonplus_serializer is not None:
        return _jsonplus_serializer
    from langgraph.checkpoint.serde.jsonplus import (
        JsonPlusSerializer,  # Lazy import: optional dependency
    )

    _jsonplus_serializer = JsonPlusSerializer()
    return _jsonplus_serializer


def _cache_message_count(thread_id: str, freshness: str | None, count: int) -> None:
    if len(_message_count_cache) >= _MAX_MESSAGE_COUNT_CACHE:
        _message_count_cache.pop(next(iter(_message_count_cache)), None)
    _message_count_cache[thread_id] = (freshness, count)


def _cache_initial_prompt(thread_id: str, freshness: str | None, prompt: str | None) -> None:
    if len(_initial_prompt_cache) >= _MAX_INITIAL_PROMPT_CACHE:
        _initial_prompt_cache.pop(next(iter(_initial_prompt_cache)), None)
    _initial_prompt_cache[thread_id] = (freshness, prompt)


def _thread_freshness(thread: ThreadInfo) -> str | None:
    return thread.get("latest_checkpoint_id") or thread.get("updated_at")


def _cache_recent_threads(
    project_root: str | None,
    limit: int,
    threads: Sequence[ThreadInfo],
) -> None:
    if len(_recent_threads_cache) >= _MAX_RECENT_THREADS_CACHE_KEYS:
        _recent_threads_cache.pop(next(iter(_recent_threads_cache)), None)
    _recent_threads_cache[(project_root, limit)] = _copy_threads(threads)


def _copy_threads(threads: Sequence[ThreadInfo]) -> list[ThreadInfo]:
    return [dict(t) for t in threads]  # type: ignore[misc]


# ── SQLite Batch Fetchers ─────────────────────────────────────────────


async def _populate_checkpoint_fields_sqlite(
    conn: aiosqlite.Connection,
    threads: list[ThreadInfo],
    *,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
) -> None:
    """Populate details for batch of threads from SQLite."""
    if not threads:
        return

    serde = await _get_jsonplus_serializer()
    thread_ids = [t["thread_id"] for t in threads]

    if include_message_count:
        counts = await _load_message_counts_from_writes_batch_sqlite(conn, thread_ids, serde)
        for t in threads:
            tid = t["thread_id"]
            if tid in counts:
                t["message_count"] = counts[tid]
                _cache_message_count(tid, _thread_freshness(t), counts[tid])

    if include_initial_prompt:
        prompts = await _load_initial_prompts_from_writes_batch_sqlite(conn, thread_ids, serde)
        for t in threads:
            tid = t["thread_id"]
            if prompts.get(tid):
                t["initial_prompt"] = prompts[tid]
                _cache_initial_prompt(tid, _thread_freshness(t), prompts[tid])

    summaries = await _load_latest_checkpoint_summaries_batch_sqlite(conn, thread_ids, serde)
    for t in threads:
        tid = t["thread_id"]
        if tid in summaries:
            cp_count, cp_prompt = summaries[tid]
            if cp_count is not None and (t.get("message_count") is None or t.get("message_count") == 0):
                t["message_count"] = cp_count
            if cp_prompt and not t.get("initial_prompt"):
                t["initial_prompt"] = cp_prompt


async def _load_latest_checkpoint_summaries_batch_sqlite(
    conn: aiosqlite.Connection,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, tuple[int | None, str | None]]:
    if not thread_ids or not await _table_exists(conn, "checkpoints"):
        return {}

    placeholders = ",".join("?" for _ in thread_ids)
    query = f"""
        SELECT thread_id, type, checkpoint, metadata FROM (
            SELECT thread_id, type, checkpoint, metadata,
                   ROW_NUMBER() OVER (
                       PARTITION BY thread_id ORDER BY checkpoint_id DESC
                   ) AS rn
            FROM checkpoints
            WHERE thread_id IN ({placeholders})
        ) WHERE rn = 1
    """
    try:
        async with conn.execute(query, tuple(thread_ids)) as cursor:
            rows = await cursor.fetchall()
    except Exception:
        return {}

    results: dict[str, tuple[int | None, str | None]] = {}
    for row in rows:
        tid = str(row[0])
        type_str = row[1]
        blob = row[2]
        meta_blob = row[3] if len(row) > 3 else None

        prompt = None
        count = None

        if meta_blob:
            try:
                meta = serde.loads_typed((type_str, meta_blob))
                if isinstance(meta, dict) and meta.get("title"):
                    prompt = meta["title"]
            except Exception:
                pass

        if blob:
            try:
                cp = serde.loads_typed((type_str, blob))
                if isinstance(cp, dict):
                    vals = cp.get("channel_values", {})
                    msgs = vals.get("messages", [])
                    if isinstance(msgs, list) and msgs:
                        count = len(msgs)
                        if not prompt:
                            prompt = _initial_prompt_from_messages(msgs)
            except Exception:
                pass

        results[tid] = (count, prompt)
    return results


async def _load_initial_prompts_from_writes_batch_sqlite(
    conn: aiosqlite.Connection,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, str | None]:
    if not thread_ids or not await _table_exists(conn, "writes"):
        return {}

    placeholders = ",".join("?" for _ in thread_ids)
    query = f"""
        SELECT thread_id, type, value
        FROM writes
        WHERE channel = 'messages'
          AND checkpoint_ns = ''
          AND thread_id IN ({placeholders})
        ORDER BY thread_id, checkpoint_id ASC, task_id ASC, idx ASC
    """
    try:
        async with conn.execute(query, tuple(thread_ids)) as cursor:
            rows = await cursor.fetchall()
    except sqlite3.OperationalError:
        return {}

    result: dict[str, str | None] = {}
    for tid, type_str, blob in rows:
        if tid in result or not type_str or not blob:
            continue
        try:
            msgs = serde.loads_typed((type_str, blob))
            prompt = _initial_prompt_from_messages(msgs if isinstance(msgs, list) else [msgs])
            if prompt:
                result[tid] = prompt
        except Exception:
            pass

    return result


async def _load_message_counts_from_writes_batch_sqlite(
    conn: aiosqlite.Connection,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, int]:
    if not thread_ids or not await _table_exists(conn, "writes"):
        return {}

    placeholders = ",".join("?" for _ in thread_ids)
    query = f"""
        SELECT thread_id, type, value
        FROM writes
        WHERE channel = 'messages'
          AND checkpoint_ns = ''
          AND thread_id IN ({placeholders})
        ORDER BY thread_id, checkpoint_id ASC, task_id ASC, idx ASC
    """
    try:
        async with conn.execute(query, tuple(thread_ids)) as cursor:
            rows = await cursor.fetchall()
    except sqlite3.OperationalError:
        return {}

    return _reduce_message_write_rows(rows, serde)


# ── PostgreSQL Batch Fetchers ─────────────────────────────────────────


async def _populate_checkpoint_fields_pg(
    conn: Any,
    threads: list[ThreadInfo],
    *,
    include_message_count: bool = True,
    include_initial_prompt: bool = True,
) -> None:
    """Populate details for batch of threads from PostgreSQL."""
    if not threads:
        return

    serde = await _get_jsonplus_serializer()
    thread_ids = [t["thread_id"] for t in threads]

    if include_message_count:
        counts = await _load_message_counts_from_writes_batch_pg(conn, thread_ids, serde)
        for t in threads:
            tid = t["thread_id"]
            if tid in counts:
                t["message_count"] = counts[tid]
                _cache_message_count(tid, _thread_freshness(t), counts[tid])

    if include_initial_prompt:
        prompts = await _load_initial_prompts_from_writes_batch_pg(conn, thread_ids, serde)
        for t in threads:
            tid = t["thread_id"]
            if prompts.get(tid):
                t["initial_prompt"] = prompts[tid]
                _cache_initial_prompt(tid, _thread_freshness(t), prompts[tid])

    summaries = await _load_latest_checkpoint_summaries_batch_pg(conn, thread_ids, serde)
    for t in threads:
        tid = t["thread_id"]
        if tid in summaries:
            cp_count, cp_prompt = summaries[tid]
            if cp_count is not None and (t.get("message_count") is None or t.get("message_count") == 0):
                t["message_count"] = cp_count
            if cp_prompt and not t.get("initial_prompt"):
                t["initial_prompt"] = cp_prompt


async def _load_latest_checkpoint_summaries_batch_pg(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, tuple[int | None, str | None]]:
    if not thread_ids:
        return {}

    query = """
        SELECT thread_id, type, checkpoint, metadata FROM (
            SELECT thread_id, type, checkpoint, metadata,
                   ROW_NUMBER() OVER (
                       PARTITION BY thread_id ORDER BY checkpoint_id DESC
                   ) AS rn
            FROM checkpoints
            WHERE thread_id = ANY(%s)
        ) t WHERE rn = 1
    """
    async with conn.cursor() as cur:
        await cur.execute(query, (thread_ids,))
        rows = await cur.fetchall()

    results: dict[str, tuple[int | None, str | None]] = {}
    loop = asyncio.get_running_loop()
    for row in rows:
        tid = str(row[0])
        type_str = row[1]
        blob = row[2]
        meta = row[3]

        prompt = None
        count = None

        if meta:
            if isinstance(meta, dict):
                prompt = meta.get("title")
            elif isinstance(meta, (bytes, memoryview)):
                try:
                    m_data = await loop.run_in_executor(None, serde.loads_typed, (type_str, bytes(meta)))
                    if isinstance(m_data, dict):
                        prompt = m_data.get("title")
                except Exception:
                    pass

        if blob:
            if isinstance(blob, dict):
                vals = blob.get("channel_values", {})
                msgs = vals.get("messages", [])
                if isinstance(msgs, list) and msgs:
                    count = len(msgs)
                    if not prompt:
                        prompt = _initial_prompt_from_messages(msgs)
            elif isinstance(blob, (bytes, memoryview)):
                try:
                    cp_data = await loop.run_in_executor(None, serde.loads_typed, (type_str, bytes(blob)))
                    if isinstance(cp_data, dict):
                        vals = cp_data.get("channel_values", {})
                        msgs = vals.get("messages", [])
                        if isinstance(msgs, list) and msgs:
                            count = len(msgs)
                            if not prompt:
                                prompt = _initial_prompt_from_messages(msgs)
                except Exception:
                    pass

        results[tid] = (count, prompt)
    return results


async def _load_initial_prompts_from_writes_batch_pg(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, str | None]:
    if not thread_ids:
        return {}

    query = """
        SELECT thread_id, type, blob FROM (
            SELECT thread_id, type, blob,
                   ROW_NUMBER() OVER (
                       PARTITION BY thread_id ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                   ) AS rn
            FROM checkpoint_writes
            WHERE channel = 'messages'
              AND checkpoint_ns = ''
              AND thread_id = ANY(%s)
        ) t WHERE rn = 1
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(query, (thread_ids,))
            rows = await cur.fetchall()
    except Exception as exc:
        logger.debug("Failed loading initial prompts from PG writes: %s", exc)
        return {}

    results: dict[str, str | None] = {}
    loop = asyncio.get_running_loop()
    for row in rows:
        tid = str(row[0])
        type_str = row[1]
        raw_blob = row[2]
        if not type_str or raw_blob is None:
            continue
        try:
            raw_bytes = bytes(raw_blob) if isinstance(raw_blob, memoryview) else raw_blob
            msgs = await loop.run_in_executor(None, serde.loads_typed, (type_str, raw_bytes))
            prompt = _initial_prompt_from_messages(msgs if isinstance(msgs, list) else [msgs])
            if prompt:
                results[tid] = prompt
        except Exception:
            pass
    return results


async def _load_message_counts_from_writes_batch_pg(
    conn: Any,
    thread_ids: list[str],
    serde: JsonPlusSerializer,
) -> dict[str, int]:
    if not thread_ids:
        return {}

    query = """
        SELECT thread_id, type, blob
        FROM checkpoint_writes
        WHERE channel = 'messages'
          AND checkpoint_ns = ''
          AND thread_id = ANY(%s)
        ORDER BY thread_id, checkpoint_id ASC, task_id ASC, idx ASC
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute(query, (thread_ids,))
            rows = await cur.fetchall()
    except Exception as exc:
        logger.debug("Failed loading message counts from PG writes: %s", exc)
        return {}

    normalized_rows = [
        (str(r[0]), r[1], bytes(r[2]) if isinstance(r[2], (bytes, memoryview)) else r[2])
        for r in rows
        if r[2] is not None
    ]
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _reduce_message_write_rows, normalized_rows, serde)


# ── Shared Reducers & Helpers ─────────────────────────────────────────


def _reduce_message_write_rows(
    rows: Iterable[Any],
    serde: JsonPlusSerializer,
) -> dict[str, int]:
    deltas_by_thread: dict[str, list[Any]] = {}
    for tid, type_str, value_blob in rows:
        if not type_str or not value_blob:
            continue
        try:
            raw_blob = bytes(value_blob) if isinstance(value_blob, memoryview) else value_blob
            delta = serde.loads_typed((type_str, raw_blob))
            deltas_by_thread.setdefault(tid, []).append(delta)
        except Exception:
            continue

    counts: dict[str, int] = {}
    for tid, deltas in deltas_by_thread.items():
        with contextlib.suppress(Exception):
            counts[tid] = _count_messages_from_deltas(deltas)
    return counts


def _visible_message_count(messages: list[object]) -> int:
    return sum(1 for m in messages if not is_internal_message(m))


def _count_messages_from_deltas(deltas: list[Any]) -> int:
    count = 0
    for delta in deltas:
        if isinstance(delta, list):
            count += sum(1 for m in delta if not is_internal_message(m))
        elif delta is not None and not is_internal_message(delta):
            count += 1
    return count


def _initial_prompt_from_messages(messages: Sequence[object]) -> str | None:
    for m in messages:
        if not is_internal_message(m):
            m_type = (
                getattr(m, "type", None) if hasattr(m, "type") else (m.get("type") if isinstance(m, dict) else None)
            )
            if m_type in ("human", "user"):
                content = (
                    getattr(m, "content", None)
                    if hasattr(m, "content")
                    else (m.get("content") if isinstance(m, dict) else None)
                )
                text = _coerce_prompt_text(content)
                if text:
                    return text
    for m in messages:
        if not is_internal_message(m):
            content = (
                getattr(m, "content", None)
                if hasattr(m, "content")
                else (m.get("content") if isinstance(m, dict) else None)
            )
            text = _coerce_prompt_text(content)
            if text:
                return text
    return None


def _coerce_prompt_text(content: object) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if txt.strip():
                    return txt.strip()
            elif isinstance(block, str) and block.strip():
                return block.strip()
    return None


async def get_most_recent(
    *,
    project_root: str | None = None,
    cwd: str | None = None,
    agent_name: str | None = None,
    git_branch: str | None = None,
    backend: str = "auto",
) -> ThreadInfo | None:
    """Retrieve the single most recent thread matching the given filters."""
    threads = await list_threads(
        limit=1,
        project_root=project_root,
        cwd=cwd,
        agent_name=agent_name,
        git_branch=git_branch,
        backend=backend,
    )
    return threads[0] if threads else None


async def get_thread_agent(thread_id: str, *, backend: str = "auto") -> str | None:
    """Retrieve the agent name associated with the latest checkpoint of a thread.

    Args:
        thread_id: Unique thread identifier.
        backend: Database backend ('auto', 'sqlite', or 'postgres').

    Returns:
        Agent name string if found, or None.
    """
    if backend == "auto":
        backend = get_active_backend()

    if backend == "postgres":
        uri = get_postgres_uri()
        if not uri:
            return None
        try:
            async with (
                _connect_postgres(uri) as conn,
                conn.cursor() as cur,
            ):
                await cur.execute(
                    "SELECT metadata->>'agent_name' FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id,),
                )
                row = await cur.fetchone()
                return str(row[0]) if row and row[0] else None
        except Exception:
            return None
    else:
        if not get_db_path().exists():
            return None
        async with _connect() as conn:
            if not await _table_exists(conn, "checkpoints"):
                return None
            async with conn.execute(
                "SELECT json_extract(metadata, '$.agent_name') FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return str(row[0]) if row and row[0] else None


async def get_thread_cwd(thread_id: str, *, backend: str = "auto") -> str | None:
    """Retrieve the working directory associated with the latest checkpoint of a thread.

    Args:
        thread_id: Unique thread identifier.
        backend: Database backend ('auto', 'sqlite', or 'postgres').

    Returns:
        Working directory path string if found, or None.
    """
    if backend == "auto":
        backend = get_active_backend()

    if backend == "postgres":
        uri = get_postgres_uri()
        if not uri:
            return None
        try:
            async with _connect_postgres(uri) as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT metadata->>'cwd' FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id,),
                )
                row = await cur.fetchone()
                return str(row[0]) if row and row[0] else None
        except Exception:
            return None
    else:
        if not get_db_path().exists():
            return None
        async with _connect() as conn:
            if not await _table_exists(conn, "checkpoints"):
                return None
            async with conn.execute(
                "SELECT json_extract(metadata, '$.cwd') FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return str(row[0]) if row and row[0] else None


async def thread_exists(thread_id: str, *, backend: str = "auto") -> bool:
    """Check whether any checkpoint exists for the given thread ID.

    Args:
        thread_id: Unique thread identifier.
        backend: Database backend ('auto', 'sqlite', or 'postgres').

    Returns:
        True if the thread exists in checkpoints, False otherwise.
    """
    if backend == "auto":
        backend = get_active_backend()

    if backend == "postgres":
        uri = get_postgres_uri()
        if not uri:
            return False
        try:
            async with _connect_postgres(uri) as conn, conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM checkpoints WHERE thread_id = %s LIMIT 1", (thread_id,))
                return bool(await cur.fetchone())
        except Exception:
            return False
    else:
        if not get_db_path().exists():
            return False
        async with _connect() as conn:
            if not await _table_exists(conn, "checkpoints"):
                return False
            async with conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                (thread_id,),
            ) as cursor:
                return bool(await cursor.fetchone())


async def find_similar_threads(thread_id: str, limit: int = 3, *, backend: str = "auto") -> list[str]:
    """Find thread IDs sharing a prefix with the given thread identifier.

    Args:
        thread_id: Target thread ID prefix or identifier.
        limit: Maximum number of matching thread IDs to return.
        backend: Database backend ('auto', 'sqlite', or 'postgres').

    Returns:
        List of matching thread ID strings.
    """
    if backend == "auto":
        backend = get_active_backend()

    prefix = (thread_id[:8] if len(thread_id) >= 8 else thread_id) + "%"
    if backend == "postgres":
        uri = get_postgres_uri()
        if not uri:
            return []
        try:
            async with _connect_postgres(uri) as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE %s LIMIT %s",
                    (prefix, limit),
                )
                rows = await cur.fetchall()
                return [str(r[0]) for r in rows]
        except Exception:
            return []
    else:
        if not get_db_path().exists():
            return []
        async with _connect() as conn:
            if not await _table_exists(conn, "checkpoints"):
                return []
            async with conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ? LIMIT ?",
                (prefix, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [str(r[0]) for r in rows]


async def delete_thread(thread_id: str, *, backend: str = "auto", db_path: Path | None = None) -> bool:
    """Delete thread checkpoints and write records across SQLite and PostgreSQL."""
    if backend == "auto":
        backend = "sqlite" if db_path is not None else get_active_backend()

    deleted = False
    if backend == "postgres":
        uri = get_postgres_uri()
        if uri:
            try:
                async with _connect_postgres(uri) as conn, conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM checkpoints WHERE thread_id = %s OR thread_id LIKE %s",
                        (thread_id, f"{thread_id}:%"),
                    )
                    cp_del = cur.rowcount > 0
                    await cur.execute(
                        "DELETE FROM checkpoint_writes WHERE thread_id = %s OR thread_id LIKE %s",
                        (thread_id, f"{thread_id}:%"),
                    )
                    cw_del = cur.rowcount > 0
                    await cur.execute(
                        "DELETE FROM checkpoint_blobs WHERE thread_id = %s OR thread_id LIKE %s",
                        (thread_id, f"{thread_id}:%"),
                    )
                    cb_del = cur.rowcount > 0
                    deleted = cp_del or cw_del or cb_del
            except Exception as exc:
                logger.debug(f"Failed deleting Postgres thread {thread_id}: {exc}")
    else:
        target_path = db_path or get_db_path()
        if target_path.exists():
            async with _connect(target_path) as conn:
                if await _table_exists(conn, "checkpoints"):
                    cursor = await conn.execute(
                        "DELETE FROM checkpoints WHERE thread_id = ? OR thread_id LIKE ?",
                        (thread_id, f"{thread_id}:%"),
                    )
                    deleted = cursor.rowcount > 0
                    if await _table_exists(conn, "writes"):
                        w_cur = await conn.execute(
                            "DELETE FROM writes WHERE thread_id = ? OR thread_id LIKE ?",
                            (thread_id, f"{thread_id}:%"),
                        )
                        deleted = deleted or (w_cur.rowcount > 0)
                    if await _table_exists(conn, "checkpoint_blobs"):
                        await conn.execute(
                            "DELETE FROM checkpoint_blobs WHERE thread_id = ? OR thread_id LIKE ?",
                            (thread_id, f"{thread_id}:%"),
                        )
                    await conn.commit()

    if deleted:
        _message_count_cache.pop(thread_id, None)
        _initial_prompt_cache.pop(thread_id, None)
        _recent_threads_cache.clear()

    return deleted


@asynccontextmanager
async def get_checkpointer() -> AsyncGenerator[AsyncSqliteSaver, None]:
    """Get AsyncSqliteSaver for default database."""
    from langgraph.checkpoint.sqlite.aio import (
        AsyncSqliteSaver,  # Lazy import: optional dependency
    )

    _patch_aiosqlite()
    async with AsyncSqliteSaver.from_conn_string(str(get_db_path())) as checkpointer:
        yield checkpointer


@asynccontextmanager
async def create_checkpointer(
    backend: str = "auto",
) -> AsyncGenerator[BaseCheckpointSaver, None]:
    """Create a checkpointer supporting default SQLite and advanced PostgreSQL.

    Args:
        backend: ``"auto"`` (default SQLite, or PostgreSQL if configured via env),
            ``"sqlite"`` (default), or ``"postgres"`` (advanced).

    Yields:
        An initialized ``BaseCheckpointSaver`` (AsyncSqliteSaver or AsyncPostgresSaver).
    """
    if backend == "auto":
        backend = get_active_backend()

    if backend == "postgres":
        from langgraph.checkpoint.postgres.aio import (
            AsyncPostgresSaver,  # Lazy import: optional dependency
        )
        from psycopg import AsyncConnection  # Lazy import: optional dependency
        from psycopg.rows import DictRow, dict_row  # Lazy import: optional dependency
        from psycopg_pool import AsyncConnectionPool  # Lazy import: optional dependency

        uri = get_postgres_uri()
        if not uri:
            raise ValueError(
                "PostgreSQL checkpointer requested but no connection URI found. "
                "Set POSTGRES_URI, K8S_AUTOPILOT_POSTGRES_URI, or DATABASE_URL."
            )
        pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            conninfo=uri,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        try:
            await pool.open()
            pg_saver = AsyncPostgresSaver(pool)
            await pg_saver.setup()
            yield pg_saver
        finally:
            await pool.close()
    elif backend == "sqlite":
        from langgraph.checkpoint.sqlite.aio import (
            AsyncSqliteSaver,  # Lazy import: optional dependency
        )

        _patch_aiosqlite()
        sqlite_saver: AsyncSqliteSaver | None = None
        try:
            async with AsyncSqliteSaver.from_conn_string(str(get_db_path())) as checkpointer:
                sqlite_saver = checkpointer
                yield checkpointer
        finally:
            if sqlite_saver is not None:
                conn = getattr(sqlite_saver, "conn", None)
                if conn is not None:
                    await _drain_aiosqlite_worker(conn)
    else:
        raise ValueError(
            f"Unsupported checkpointer backend: {backend!r}. "
            "Supported backends are 'sqlite' (default) and 'postgres' (advanced)."
        )


def clear_session_caches() -> None:
    """Clear in-memory session and thread caches upon storage backend switch."""
    _recent_threads_cache.clear()
    _message_count_cache.clear()
    _initial_prompt_cache.clear()


async def close_checkpointer(cp: Any) -> None:
    """Cleanly close connection or pool of a retired checkpointer."""
    if cp is None:
        return
    try:
        conn = getattr(cp, "conn", None)
        if conn is not None:
            if hasattr(conn, "close"):
                res = conn.close()
                if asyncio.iscoroutine(res):
                    await res
            worker = getattr(conn, "_thread", None)
            if worker is not None and hasattr(worker, "is_alive") and worker.is_alive():
                await _drain_aiosqlite_worker(conn)
    except Exception as exc:
        logger.debug("Error closing retired checkpointer: %s", exc)


async def create_runtime_checkpointer(backend: str = "auto", uri: str | None = None) -> Any:
    """Instantiate an initialized checkpointer for runtime hot-swapping."""
    if backend == "auto":
        backend = get_active_backend()
    if backend == "postgres":
        from langgraph.checkpoint.postgres.aio import (
            AsyncPostgresSaver,  # Lazy import: optional dependency
        )
        from psycopg.rows import dict_row  # Lazy import: optional dependency
        from psycopg_pool import AsyncConnectionPool  # Lazy import: optional dependency

        pg_uri = uri or get_postgres_uri()
        if not pg_uri:
            raise ValueError("PostgreSQL checkpointer requested but no URI provided.")
        pool = AsyncConnectionPool(
            conninfo=pg_uri,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open()
        saver = AsyncPostgresSaver(cast(Any, pool))
        await saver.setup()
        return saver
    elif backend == "sqlite":
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import (
            AsyncSqliteSaver,  # Lazy import: optional dependency
        )

        _patch_aiosqlite()
        db_path = get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        return saver
    else:
        raise ValueError(
            f"Unsupported checkpointer backend: {backend!r}. "
            "Supported backends are 'sqlite' (default) and 'postgres' (advanced)."
        )


class SessionManager:
    """Manage conversation threads and checkpoint persistence across SQLite and PostgreSQL."""

    def __init__(self, db_path: Path | None = None, postgres_uri: str | None = None) -> None:
        """Initialize the SessionManager instance.

        Args:
            db_path: Optional path to SQLite database. Defaults to configured path.
            postgres_uri: Optional PostgreSQL connection URI.
        """
        if db_path is not None and postgres_uri is None:
            self._db_path = db_path
            self._postgres_uri = None
        elif postgres_uri is not None:
            self._db_path = db_path or get_db_path()
            self._postgres_uri = postgres_uri
        else:
            self._db_path = get_db_path()
            self._postgres_uri = get_postgres_uri() if get_active_backend() == "postgres" else None

    def generate_thread_id(self) -> str:
        """Generate a new unique thread identifier.

        Returns:
            UUID-based thread ID string.
        """
        return generate_thread_id()

    async def get_checkpointer(self) -> Any:
        """Instantiate and configure the appropriate LangGraph checkpointer for the active backend.

        Returns:
            Configured checkpointer instance (AsyncSqliteSaver or AsyncPostgresSaver).

        Raises:
            ValueError: If postgres backend is selected but postgres_uri is missing.
        """
        backend = "postgres" if self._postgres_uri else "sqlite"
        if backend == "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            if not self._postgres_uri:
                raise ValueError("Postgres URI is required for postgres backend")

            pool = AsyncConnectionPool(
                conninfo=str(self._postgres_uri),
                min_size=1,
                max_size=5,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await pool.open()
            saver = AsyncPostgresSaver(cast(Any, pool))
            await saver.setup()
            return saver
        else:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            _patch_aiosqlite()
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            return AsyncSqliteSaver.from_conn_string(str(self._db_path))

    async def list_threads(self, limit: int = 20) -> list[ThreadInfo]:
        """List recent conversation threads with populated checkpoint details.

        Args:
            limit: Maximum number of threads to return.

        Returns:
            List of ThreadInfo objects populated with message count and prompt details.
        """
        backend = "postgres" if self._postgres_uri else "sqlite"
        threads = await list_threads(
            limit=limit,
            include_message_count=False,
            backend=backend,
            db_path=self._db_path,
        )
        if threads:
            await populate_thread_checkpoint_details(
                threads,
                include_message_count=True,
                include_initial_prompt=True,
                backend=backend,
                db_path=self._db_path,
            )
        return threads

    async def resume_thread(self, thread_id: str) -> Any:
        """Fetch the latest checkpoint tuple for the given thread to resume execution.

        Args:
            thread_id: Unique thread identifier.

        Returns:
            Checkpoint tuple if found, or None.
        """
        from langchain_core.runnables import RunnableConfig

        checkpointer = await self.get_checkpointer()
        if hasattr(checkpointer, "__aenter__"):
            async with checkpointer as saver:
                config = RunnableConfig(configurable={"thread_id": thread_id})
                return await saver.aget_tuple(config)
        else:
            config = RunnableConfig(configurable={"thread_id": thread_id})
            return await checkpointer.aget_tuple(config)

    async def get_thread_messages(self, thread_id: str) -> list[Any]:
        """Fetch and deserialize all stored messages for a thread from writes/checkpoints."""
        from langgraph.graph.message import add_messages

        serde = await _get_jsonplus_serializer()
        accumulated: list[Any] = []

        # ── PostgreSQL Branch ──────────────────────────────────────────
        if self._postgres_uri:
            try:
                async with _connect_postgres(self._postgres_uri) as conn, conn.cursor() as cur:
                    await cur.execute(
                        """
                            SELECT type, blob FROM checkpoint_writes
                            WHERE thread_id = %s AND channel = 'messages' AND checkpoint_ns = ''
                            ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                            """,
                        (thread_id,),
                    )
                    rows = await cur.fetchall()
                    for type_str, payload in rows:
                        if type_str and payload is not None:
                            try:
                                raw_b = bytes(payload) if isinstance(payload, memoryview) else payload
                                delta = serde.loads_typed((type_str, raw_b))
                                accumulated = list(
                                    add_messages(accumulated, delta if isinstance(delta, list) else [delta])
                                )
                            except Exception as exc:
                                logger.debug("Failed deserializing PG write for %s: %s", thread_id, exc)

                    if not accumulated:
                        await cur.execute(
                            """
                                SELECT type, checkpoint FROM checkpoints
                                WHERE thread_id = %s AND checkpoint_ns = ''
                                ORDER BY checkpoint_id DESC LIMIT 1
                                """,
                            (thread_id,),
                        )
                        row = await cur.fetchone()
                        if row and row[1]:
                            try:
                                raw_cp = bytes(row[1]) if isinstance(row[1], memoryview) else row[1]
                                cp_data = (
                                    raw_cp
                                    if isinstance(raw_cp, dict)
                                    else (serde.loads_typed((row[0], raw_cp)) if row[0] else raw_cp)
                                )
                                if isinstance(cp_data, dict):
                                    msgs = cp_data.get("channel_values", {}).get("messages", [])
                                    if isinstance(msgs, list):
                                        accumulated = list(msgs)
                            except Exception as exc:
                                logger.debug("Failed deserializing PG checkpoint for %s: %s", thread_id, exc)

                    if not accumulated:
                        await cur.execute(
                            """
                                SELECT type, blob FROM checkpoint_blobs
                                WHERE thread_id = %s AND channel = 'messages' AND checkpoint_ns = ''
                                ORDER BY version DESC LIMIT 1
                                """,
                            (thread_id,),
                        )
                        row = await cur.fetchone()
                        if row and row[1]:
                            try:
                                raw_b = bytes(row[1]) if isinstance(row[1], memoryview) else row[1]
                                msgs = serde.loads_typed((row[0], raw_b)) if row[0] else raw_b
                                if isinstance(msgs, list):
                                    accumulated = list(msgs)
                            except Exception as exc:
                                logger.debug("Failed deserializing PG blob for %s: %s", thread_id, exc)
                if accumulated:
                    return accumulated
            except Exception as exc:
                logger.warning("Failed fetching PG thread history for %s: %s", thread_id, exc)

        # ── SQLite Branch ──────────────────────────────────────────────
        if self._db_path.exists():
            import aiosqlite

            _patch_aiosqlite()
            try:
                async with aiosqlite.connect(self._db_path) as conn:
                    async with conn.execute(
                        """
                        SELECT type, value FROM writes
                        WHERE thread_id = ? AND channel = 'messages'
                          AND checkpoint_ns = ''
                        ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                        """,
                        (thread_id,),
                    ) as cursor:
                        rows = await cursor.fetchall()

                    for type_str, blob in rows:
                        if type_str and blob:
                            try:
                                delta = serde.loads_typed((type_str, blob))
                                accumulated = list(add_messages(accumulated, delta if isinstance(delta, list) else [delta]))
                            except Exception as exc:
                                logger.debug("Failed deserializing SQLite write for %s: %s", thread_id, exc)

                    if not accumulated:
                        async with conn.execute(
                            """
                            SELECT type, checkpoint FROM checkpoints
                            WHERE thread_id = ? AND checkpoint_ns = ''
                            ORDER BY checkpoint_id DESC LIMIT 1
                            """,
                            (thread_id,),
                        ) as cursor:
                            row = await cursor.fetchone()
                        if row and row[0] and row[1]:
                            try:
                                cp_data = serde.loads_typed((row[0], row[1]))
                                if isinstance(cp_data, dict):
                                    msgs = cp_data.get("channel_values", {}).get("messages", [])
                                    if isinstance(msgs, list):
                                        accumulated = list(msgs)
                            except Exception as exc:
                                logger.debug("Failed deserializing SQLite checkpoint for %s: %s", thread_id, exc)
            except Exception as exc:
                logger.warning("Failed fetching SQLite thread history for %s: %s", thread_id, exc)

        # Cross-backend fallback if messages not found in active SQLite backend
        if not accumulated and not self._postgres_uri:
            fallback_uri = get_postgres_uri()
            if fallback_uri:
                with contextlib.suppress(Exception):
                    return await SessionManager(postgres_uri=fallback_uri).get_thread_messages(thread_id)

        return accumulated

    async def get_thread_token_usage_and_cost(self, thread_id: str) -> tuple[int, int, float, list[str]]:
        """Calculate cumulative input tokens, output tokens, cost in USD, and processed message IDs for a thread.

        Unlike `get_thread_messages`, this inspects writes across ALL namespaces (including subagents
        and rubric grading subgraphs) deduplicating by message ID to accurately reflect total LLM consumption.
        """
        serde = await _get_jsonplus_serializer()
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        seen_msg_ids: list[str] = []
        seen_set: set[str] = set()

        # ── PostgreSQL Branch ──────────────────────────────────────────
        if self._postgres_uri:
            try:
                async with _connect_postgres(self._postgres_uri) as conn, conn.cursor() as cur:
                    await cur.execute(
                        """
                            SELECT type, blob FROM checkpoint_writes
                            WHERE thread_id = %s AND channel = 'messages'
                            ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                            """,
                        (thread_id,),
                    )
                    rows = await cur.fetchall()
                    for type_str, payload in rows:
                        if type_str and payload is not None:
                            try:
                                raw_b = bytes(payload) if isinstance(payload, memoryview) else payload
                                delta = serde.loads_typed((type_str, raw_b))
                                msgs = delta if isinstance(delta, list) else [delta]
                                for m in msgs:
                                    mid = getattr(m, "id", None)
                                    u = (
                                        getattr(m, "usage_metadata", None)
                                        or getattr(m, "response_metadata", {}).get("token_usage")
                                        or getattr(m, "response_metadata", {}).get("usage")
                                    )
                                    if mid and mid not in seen_set:
                                        seen_set.add(mid)
                                        seen_msg_ids.append(mid)
                                        if isinstance(u, dict):
                                            input_tokens += int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
                                            output_tokens += int(
                                                u.get("output_tokens") or u.get("completion_tokens") or 0
                                            )
                            except Exception as exc:
                                logger.debug("Failed deserializing PG message write for %s: %s", thread_id, exc)

                    # Read cost from root checkpoint
                    await cur.execute(
                        """
                            SELECT checkpoint FROM checkpoints
                            WHERE thread_id = %s AND checkpoint_ns = ''
                            ORDER BY checkpoint_id DESC LIMIT 1
                            """,
                        (thread_id,),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        cp = row[0] if isinstance(row[0], dict) else serde.loads_typed(("json", bytes(row[0])))
                        if isinstance(cp, dict):
                            cv = cp.get("channel_values", {})
                            if "_session_cost_usd" in cv and cv["_session_cost_usd"] is not None:
                                with contextlib.suppress(Exception):
                                    cost_usd = float(cv["_session_cost_usd"])
                if seen_msg_ids:
                    return input_tokens, output_tokens, cost_usd, seen_msg_ids
            except Exception as exc:
                logger.warning("Failed calculating PG thread token usage for %s: %s", thread_id, exc)

        # ── SQLite Branch ──────────────────────────────────────────────
        if self._db_path.exists():
            import aiosqlite

            _patch_aiosqlite()
            try:
                async with aiosqlite.connect(self._db_path) as conn:
                    async with conn.execute(
                        """
                        SELECT type, value FROM writes
                        WHERE thread_id = ? AND channel = 'messages'
                        ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                        """,
                        (thread_id,),
                    ) as cursor:
                        rows = await cursor.fetchall()

                    for type_str, blob in rows:
                        if type_str and blob:
                            try:
                                delta = serde.loads_typed((type_str, blob))
                                msgs = delta if isinstance(delta, list) else [delta]
                                for m in msgs:
                                    mid = getattr(m, "id", None)
                                    u = (
                                        getattr(m, "usage_metadata", None)
                                        or getattr(m, "response_metadata", {}).get("token_usage")
                                        or getattr(m, "response_metadata", {}).get("usage")
                                    )
                                    if mid and mid not in seen_set:
                                        seen_set.add(mid)
                                        seen_msg_ids.append(mid)
                                        if isinstance(u, dict):
                                            input_tokens += int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
                                            output_tokens += int(u.get("output_tokens") or u.get("completion_tokens") or 0)
                            except Exception as exc:
                                logger.debug("Failed deserializing SQLite message write for %s: %s", thread_id, exc)

                    async with conn.execute(
                        """
                        SELECT type, checkpoint FROM checkpoints
                        WHERE thread_id = ? AND checkpoint_ns = ''
                        ORDER BY checkpoint_id DESC LIMIT 1
                        """,
                        (thread_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row and row[0] and row[1]:
                        try:
                            cp_data = serde.loads_typed((row[0], row[1]))
                            if isinstance(cp_data, dict):
                                cv = cp_data.get("channel_values", {})
                                if "_session_cost_usd" in cv and cv["_session_cost_usd"] is not None:
                                    with contextlib.suppress(Exception):
                                        cost_usd = float(cv["_session_cost_usd"])
                        except Exception as exc:
                            logger.debug("Failed deserializing SQLite checkpoint for cost: %s", exc)
            except Exception as exc:
                logger.warning("Failed calculating SQLite thread token usage for %s: %s", thread_id, exc)

        # Cross-backend fallback if usage not found in active SQLite backend
        if not seen_msg_ids and not self._postgres_uri:
            fallback_uri = get_postgres_uri()
            if fallback_uri:
                with contextlib.suppress(Exception):
                    return await SessionManager(postgres_uri=fallback_uri).get_thread_token_usage_and_cost(thread_id)

        return input_tokens, output_tokens, cost_usd, seen_msg_ids

    async def get_thread_goal_state(self, thread_id: str) -> dict[str, Any]:
        """Fetch authoritative goal objective, status, and rubric from checkpoints and writes."""
        serde = await _get_jsonplus_serializer()
        res: dict[str, Any] = {
            "objective": None,
            "status": None,
            "rubric": None,
            "status_note": None,
        }

        # ── PostgreSQL Branch ──────────────────────────────────────────
        if self._postgres_uri:
            try:
                async with (
                    _connect_postgres(self._postgres_uri) as conn,
                    conn.cursor() as cur,
                ):
                    # 1. Read from latest checkpoint
                    await cur.execute(
                        """
                        SELECT type, checkpoint FROM checkpoints
                        WHERE thread_id = %s AND checkpoint_ns = ''
                        ORDER BY checkpoint_id DESC LIMIT 1
                        """,
                        (thread_id,),
                    )
                    row = await cur.fetchone()
                    if row and row[1]:
                        raw_cp = bytes(row[1]) if isinstance(row[1], memoryview) else row[1]
                        cp_data = serde.loads_typed((row[0], raw_cp)) if row[0] else raw_cp
                        if isinstance(cp_data, dict):
                            cv = cp_data.get("channel_values", {})
                            if cv.get("_goal_objective"):
                                res["objective"] = str(cv["_goal_objective"])
                            if cv.get("_goal_status"):
                                res["status"] = str(cv["_goal_status"])
                            if cv.get("_rubric_status"):
                                r_stat = str(cv["_rubric_status"]).lower()
                                if r_stat in ("satisfied", "passed", "complete"):
                                    res["status"] = "complete"
                                elif (
                                    r_stat in ("max_iterations_reached", "failed", "blocked")
                                    and res["status"] != "complete"
                                ):
                                    res["status"] = "blocked"
                            raw_r = (
                                cv.get("rubric")
                                or cv.get("_goal_rubric")
                                or cv.get("_sticky_rubric")
                                or cv.get("criteria")
                            )
                            if isinstance(raw_r, list):
                                res["rubric"] = "\n".join(f"- {c}" for c in raw_r if c)
                            elif isinstance(raw_r, str) and raw_r.strip():
                                res["rubric"] = raw_r.strip()

                    # 2. Check pending/recent writes in checkpoint_writes
                    await cur.execute(
                        """
                        SELECT channel, type, blob FROM checkpoint_writes
                        WHERE thread_id = %s AND checkpoint_ns = ''
                          AND channel IN ('_goal_objective', '_goal_status', '_goal_rubric', '_sticky_rubric', 'rubric', '_rubric_status')
                        ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                        """,
                        (thread_id,),
                    )
                    rows = await cur.fetchall()
                    for ch, type_str, payload in rows:
                        if type_str and payload is not None:
                            try:
                                raw_b = bytes(payload) if isinstance(payload, memoryview) else payload
                                val = serde.loads_typed((type_str, raw_b))
                                if ch == "_goal_objective" and val:
                                    res["objective"] = str(val)
                                elif ch == "_goal_status" and val:
                                    res["status"] = str(val)
                                elif ch == "_rubric_status" and val:
                                    r_stat = str(val).lower()
                                    if r_stat in ("satisfied", "passed", "complete"):
                                        res["status"] = "complete"
                                    elif (
                                        r_stat in ("max_iterations_reached", "failed", "blocked")
                                        and res["status"] != "complete"
                                    ):
                                        res["status"] = "blocked"
                                elif ch in ("_goal_rubric", "_sticky_rubric", "rubric") and val:
                                    if isinstance(val, list):
                                        res["rubric"] = "\n".join(f"- {c}" for c in val if c)
                                    elif isinstance(val, str) and val.strip():
                                        res["rubric"] = val.strip()
                            except Exception:
                                pass
                    if res.get("objective") or res.get("status") or res.get("rubric"):
                        return res
            except Exception as exc:
                logger.warning("Failed fetching PG thread goal state for %s: %s", thread_id, exc)

        # ── SQLite Branch ──────────────────────────────────────────────
        if self._db_path.exists():
            import aiosqlite

            _patch_aiosqlite()
            try:
                async with aiosqlite.connect(self._db_path) as conn:
                    async with conn.execute(
                        """
                        SELECT type, checkpoint FROM checkpoints
                        WHERE thread_id = ? AND checkpoint_ns = ''
                        ORDER BY checkpoint_id DESC LIMIT 1
                        """,
                        (thread_id,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row and row[0] and row[1]:
                        try:
                            cp_data = serde.loads_typed((row[0], row[1]))
                            if isinstance(cp_data, dict):
                                cv = cp_data.get("channel_values", {})
                                if cv.get("_goal_objective"):
                                    res["objective"] = str(cv["_goal_objective"])
                                if cv.get("_goal_status"):
                                    res["status"] = str(cv["_goal_status"])
                                if cv.get("_rubric_status"):
                                    r_stat = str(cv["_rubric_status"]).lower()
                                    if r_stat in ("satisfied", "passed", "complete"):
                                        res["status"] = "complete"
                                    elif (
                                        r_stat in ("max_iterations_reached", "failed", "blocked")
                                        and res["status"] != "complete"
                                    ):
                                        res["status"] = "blocked"
                                raw_r = (
                                    cv.get("rubric")
                                    or cv.get("_goal_rubric")
                                    or cv.get("_sticky_rubric")
                                    or cv.get("criteria")
                                )
                                if isinstance(raw_r, list):
                                    res["rubric"] = "\n".join(f"- {c}" for c in raw_r if c)
                                elif isinstance(raw_r, str) and raw_r.strip():
                                    res["rubric"] = raw_r.strip()
                        except Exception:
                            pass

                    async with conn.execute(
                        """
                        SELECT channel, type, value FROM writes
                        WHERE thread_id = ? AND checkpoint_ns = ''
                          AND channel IN ('_goal_objective', '_goal_status', '_goal_rubric', '_sticky_rubric', 'rubric', '_rubric_status')
                        ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                        """,
                        (thread_id,),
                    ) as cursor:
                        rows = await cursor.fetchall()
                    for ch, type_str, blob in rows:
                        if type_str and blob:
                            try:
                                val = serde.loads_typed((type_str, blob))
                                if ch == "_goal_objective" and val:
                                    res["objective"] = str(val)
                                elif ch == "_goal_status" and val:
                                    res["status"] = str(val)
                                elif ch == "_rubric_status" and val:
                                    r_stat = str(val).lower()
                                    if r_stat in ("satisfied", "passed", "complete"):
                                        res["status"] = "complete"
                                    elif (
                                        r_stat in ("max_iterations_reached", "failed", "blocked")
                                        and res["status"] != "complete"
                                    ):
                                        res["status"] = "blocked"
                                elif ch in ("_goal_rubric", "_sticky_rubric", "rubric") and val:
                                    if isinstance(val, list):
                                        res["rubric"] = "\n".join(f"- {c}" for c in val if c)
                                    elif isinstance(val, str) and val.strip():
                                        res["rubric"] = val.strip()
                            except Exception:
                                pass
            except Exception as exc:
                logger.warning("Failed fetching SQLite thread goal state for %s: %s", thread_id, exc)

        # Cross-backend fallback if goal state not found in active SQLite backend
        if not (res.get("objective") or res.get("status") or res.get("rubric")) and not self._postgres_uri:
            fallback_uri = get_postgres_uri()
            if fallback_uri:
                with contextlib.suppress(Exception):
                    return await SessionManager(postgres_uri=fallback_uri).get_thread_goal_state(thread_id)

        return res

    async def delete_thread(self, thread_id: str) -> bool:
        """Delete all checkpoints, writes, and metadata associated with a thread.

        Args:
            thread_id: Unique thread identifier.

        Returns:
            True if deletion succeeded, False otherwise.
        """
        backend = "postgres" if self._postgres_uri else "sqlite"
        return await delete_thread(thread_id, backend=backend)
