"""Unit tests for Session Manager (Phase 22)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


class TestFormatTimestamp:
    """Tests for timestamp formatting utilities."""

    def test_format_timestamp_valid(self) -> None:
        from k8s_autopilot.state.session import format_timestamp

        result = format_timestamp("2025-12-30T18:10:00+00:00")
        assert result  # non-empty
        assert "dec" in result.lower() or "30" in result

    def test_format_timestamp_none(self) -> None:
        from k8s_autopilot.state.session import format_timestamp

        assert format_timestamp(None) == ""

    def test_format_timestamp_invalid(self) -> None:
        from k8s_autopilot.state.session import format_timestamp

        assert format_timestamp("not-a-date") == ""


class TestFormatRelativeTimestamp:
    """Tests for relative timestamp formatting."""

    def test_just_now(self) -> None:
        from k8s_autopilot.state.session import format_relative_timestamp

        now = datetime.now(tz=timezone.utc).isoformat()
        result = format_relative_timestamp(now)
        assert "s ago" in result or "just now" in result

    def test_none_returns_empty(self) -> None:
        from k8s_autopilot.state.session import format_relative_timestamp

        assert format_relative_timestamp(None) == ""


class TestFormatPath:
    """Tests for path formatting."""

    def test_home_path(self) -> None:
        from pathlib import Path
        from k8s_autopilot.state.session import format_path

        home = str(Path.home())
        assert format_path(home) == "~"

    def test_home_subpath(self) -> None:
        from pathlib import Path
        from k8s_autopilot.state.session import format_path

        home = str(Path.home())
        assert format_path(f"{home}/projects/test") == "~/projects/test"

    def test_non_home_path(self) -> None:
        from k8s_autopilot.state.session import format_path

        assert format_path("/usr/local/bin") == "/usr/local/bin"

    def test_empty_path(self) -> None:
        from k8s_autopilot.state.session import format_path

        assert format_path(None) == ""
        assert format_path("") == ""


class TestGenerateThreadId:
    """Tests for UUID7-based thread ID generation."""

    def test_generates_valid_uuid(self) -> None:
        from k8s_autopilot.state.session import generate_thread_id

        tid = generate_thread_id()
        assert len(tid) == 36  # UUID format
        assert tid.count("-") == 4

    def test_ids_are_unique(self) -> None:
        from k8s_autopilot.state.session import generate_thread_id

        ids = {generate_thread_id() for _ in range(100)}
        assert len(ids) == 100

    def test_ids_are_time_ordered(self) -> None:
        from k8s_autopilot.state.session import generate_thread_id

        id1 = generate_thread_id()
        id2 = generate_thread_id()
        # UUID7 is time-ordered, so id2 > id1
        assert id2 > id1


class TestPatchAiosqlite:
    """Tests for the aiosqlite compatibility patch."""

    def test_patch_is_idempotent(self) -> None:
        from k8s_autopilot.state.session import _patch_aiosqlite

        _patch_aiosqlite()
        _patch_aiosqlite()  # Should not raise


class TestGetDbPath:
    """Tests for database path resolution."""

    def test_returns_path(self) -> None:
        from k8s_autopilot.state.session import get_db_path

        path = get_db_path()
        assert path.name == "sessions.db"
        assert ".k8s_autopilot" in str(path)


class TestCacheHelpers:
    """Tests for the in-memory caching utilities."""

    def test_cache_recent_threads(self) -> None:
        from k8s_autopilot.state.session import (
            _cache_recent_threads,
            _recent_threads_cache,
            get_cached_threads,
        )

        _recent_threads_cache.clear()
        threads = [{"thread_id": "t1", "agent_name": None, "updated_at": None}]
        _cache_recent_threads(None, 20, threads)

        cached = get_cached_threads(limit=20)
        assert cached is not None
        assert len(cached) == 1

    def test_get_cached_threads_none_when_empty(self) -> None:
        from k8s_autopilot.state.session import _recent_threads_cache, get_cached_threads

        _recent_threads_cache.clear()
        assert get_cached_threads(limit=20) is None


@pytest.mark.asyncio
class TestSessionAsync:
    """Async integration tests for session management."""

    async def test_list_threads_empty_db(self, tmp_path) -> None:
        """list_threads on an empty/nonexistent DB returns empty list."""
        from k8s_autopilot.state import session

        # Point to a temp DB path
        original = session._db_path
        session._db_path = tmp_path / "test_sessions.db"
        try:
            threads = await session.list_threads()
            assert threads == []
        finally:
            session._db_path = original

    async def test_delete_thread_empty_db(self, tmp_path) -> None:
        """delete_thread on empty DB returns False."""
        from k8s_autopilot.state import session

        original = session._db_path
        session._db_path = tmp_path / "test_sessions.db"
        try:
            result = await session.delete_thread("nonexistent-id")
            assert result is False
        finally:
            session._db_path = original
