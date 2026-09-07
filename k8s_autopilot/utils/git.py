"""Lightweight git metadata helpers for state detection and repository inspection."""

from __future__ import annotations

from pathlib import Path
import re

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_GIT_DIR_POINTER_PREFIX = "gitdir: "
_GIT_HEAD_REF_PREFIX = "ref: "
_GIT_REF_PREFIXES = ("refs/heads/", "refs/remotes/", "refs/tags/", "refs/")
_git_dir_cache: dict[str, Path] = {}


def _abbreviate_git_ref(ref: str) -> str:
    for prefix in _GIT_REF_PREFIXES:
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)
    return ref


def _parse_git_dir_pointer(git_entry: Path) -> Path | None:
    try:
        raw = git_entry.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not raw.startswith(_GIT_DIR_POINTER_PREFIX):
        return None

    pointer = raw.removeprefix(_GIT_DIR_POINTER_PREFIX).strip()
    if not pointer:
        return None

    git_dir = Path(pointer)
    if not git_dir.is_absolute():
        git_dir = git_entry.parent / git_dir
    return git_dir.resolve(strict=False)


def _normalize_lookup_path(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except OSError:
        return Path(path).expanduser()


def find_git_dir(path: str | Path) -> Path | None:
    """Find the .git directory containing path."""
    normalized = _normalize_lookup_path(path)
    cache_key = str(normalized)
    if cache_key in _git_dir_cache:
        return _git_dir_cache[cache_key]

    current = normalized if normalized.is_dir() else normalized.parent
    for directory in (current, *current.parents):
        candidate = directory / ".git"
        if candidate.is_dir():
            _git_dir_cache[cache_key] = candidate
            return candidate
        if candidate.is_file():
            resolved = _parse_git_dir_pointer(candidate)
            if resolved is not None and resolved.is_dir():
                _git_dir_cache[cache_key] = resolved
                return resolved

    return None


def get_git_root(path: str | Path) -> Path | None:
    """Find the root directory of the git repository containing path."""
    git_dir = find_git_dir(path)
    if git_dir is None:
        return None
    if git_dir.name == ".git":
        return git_dir.parent
    # Worktrees / bare repos
    return git_dir.parent


def get_git_branch(path: str | Path) -> str | None:
    """Get the active git branch name for the repository containing path."""
    git_dir = find_git_dir(path)
    if git_dir is None:
        return None

    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        return None

    try:
        content = head_file.read_text(encoding="utf-8").strip()
        if content.startswith(_GIT_HEAD_REF_PREFIX):
            ref = content.removeprefix(_GIT_HEAD_REF_PREFIX).strip()
            return _abbreviate_git_ref(ref)
        if re.match(r"^[0-9a-f]{40}$", content, re.IGNORECASE):
            return content[:7]
    except OSError:
        pass

    return None


def get_git_remote_url(path: str | Path, remote: str = "origin") -> str | None:
    """Extract git remote URL from config without subprocess."""
    git_dir = find_git_dir(path)
    if git_dir is None:
        return None

    config_file = git_dir / "config"
    if not config_file.is_file():
        return None

    try:
        text = config_file.read_text(encoding="utf-8")
        pattern = rf'\[remote\s+"{remote}"\][^\[]*?url\s*=\s*([^\r\n]+)'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
    except OSError:
        pass

    return None
