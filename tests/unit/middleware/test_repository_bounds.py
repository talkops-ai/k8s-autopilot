"""Unit tests for RepositoryBounds path containment and size safety."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from deepagents.backends.filesystem import FilesystemBackend
from k8s_autopilot.middleware._repository_bounds import (
    REPOSITORY_GLOB_MATCH_LIMIT,
    REPOSITORY_GREP_MATCH_LIMIT,
    REPOSITORY_LISTING_ERROR,
    REPOSITORY_PATH_ERROR,
    REPOSITORY_READ_LINE_LIMIT,
    REPOSITORY_READ_ONLY_ERROR,
    REPOSITORY_SIZE_ERROR,
    RepositoryBounds,
)


class TestRepositoryBounds:
    def test_init_valid_root(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(virtual_mode=False)
        bounds = RepositoryBounds(backend, root=str(tmp_path))
        assert bounds.root == str(tmp_path)

    def test_init_invalid_root_relative(self) -> None:
        backend = MagicMock()
        with pytest.raises(ValueError, match="must be an absolute contained path"):
            RepositoryBounds(backend, root="relative/path")

    def test_init_invalid_root_traversal(self) -> None:
        backend = MagicMock()
        with pytest.raises(ValueError, match="must be an absolute contained path"):
            RepositoryBounds(backend, root="/path/../etc")

    def test_safe_path(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(virtual_mode=False)
        bounds = RepositoryBounds(backend, root=str(tmp_path))
        assert bounds.safe_path(str(tmp_path / "file.txt"))
        assert not bounds.safe_path(str(tmp_path / "../outside.txt"))
        assert not bounds.safe_path("~/home.txt")
        assert not bounds.safe_path("relative/path.txt")

    def test_safe_pattern(self) -> None:
        assert RepositoryBounds.safe_pattern("*.py")
        assert RepositoryBounds.safe_pattern("src/**/*.rs")
        assert not RepositoryBounds.safe_pattern("../etc/*")
        assert not RepositoryBounds.safe_pattern("~/secret/*")

    def test_clamp_args(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(virtual_mode=False)
        bounds = RepositoryBounds(backend, root=str(tmp_path))

        # read_file limit clamp
        clamped = bounds.clamp_args("read_file", {"file_path": "/test", "limit": 99999})
        assert clamped["limit"] == REPOSITORY_READ_LINE_LIMIT

        # grep match clamp
        clamped_grep = bounds.clamp_args("grep", {"pattern": "foo", "max_count": 5000})
        assert clamped_grep["max_count"] == REPOSITORY_GREP_MATCH_LIMIT
        assert clamped_grep["path"] == str(tmp_path)

    def test_preflight_rejects_non_readonly_tools(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(virtual_mode=False)
        bounds = RepositoryBounds(backend, root=str(tmp_path))
        assert bounds.preflight("write_file", {"file_path": "/test"}) == REPOSITORY_READ_ONLY_ERROR
        assert bounds.preflight("execute", {"command": "ls"}) == REPOSITORY_READ_ONLY_ERROR

    def test_bound_text(self, tmp_path: Path) -> None:
        backend = FilesystemBackend(virtual_mode=False)
        bounds = RepositoryBounds(backend, root=str(tmp_path))

        short_text = "Short output"
        assert bounds.bound_text("read_file", short_text) == short_text

        long_text = "a" * 20_000
        bounded = bounds.bound_text("read_file", long_text)
        assert len(bounded) <= 12_000
        assert "shortened to the context limit" in bounded
