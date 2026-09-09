"""Centralized filesystem-path definitions for K8s Autopilot.

This module is the **single source of truth** for every directory and file
k8s_autopilot reads or writes at runtime. It is intentionally dependency-free.
"""

from __future__ import annotations

from enum import StrEnum
import errno
from pathlib import Path
from typing import Final

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────

ENV_PREFIX: Final[str] = "K8S_AUTOPILOT_"
"""All K8s Autopilot specific env vars use this prefix."""

DEFAULT_ASSISTANT_ID: Final[str] = "k8s-autopilot"
"""Default assistant identifier."""

# ── Root directories ─────────────────────────────────────

DATA_DIR: Final[Path] = Path.home() / ".k8s_autopilot"
"""``~/.k8s_autopilot/`` — Top-level user data directory."""

STATE_DIR: Final[Path] = DATA_DIR / ".state"
"""``~/.k8s_autopilot/.state/`` — Machine-managed state."""

# ── User-facing config files ─────────────────────────────

CONFIG_PATH: Final[Path] = DATA_DIR / "config.toml"
"""``~/.k8s_autopilot/config.toml`` — Main configuration file."""

GLOBAL_ENV_PATH: Final[Path] = DATA_DIR / ".env"
"""``~/.k8s_autopilot/.env`` — Global env vars / API keys."""

GLOBAL_MCP_PATH: Final[Path] = DATA_DIR / ".mcp.json"
"""``~/.k8s_autopilot/.mcp.json`` — Global MCP server definitions."""

CONVERSATION_HISTORY_DIR: Final[Path] = DATA_DIR / "conversation_history"
"""``~/.k8s_autopilot/conversation_history/`` — Conversation logs."""

PLUGINS_DIR: Final[Path] = DATA_DIR / "plugins"
"""``~/.k8s_autopilot/plugins/`` — Installed plugins and marketplaces directory."""

# ── Managed state files (.state/) ────────────────────────

SESSIONS_DB_PATH: Final[Path] = STATE_DIR / "sessions.db"
"""``~/.k8s_autopilot/.state/sessions.db`` — SQLite conversation checkpoints."""

HISTORY_PATH: Final[Path] = STATE_DIR / "history.jsonl"
"""``~/.k8s_autopilot/.state/history.jsonl`` — Command input history."""

MCP_TRUST_PATH: Final[Path] = STATE_DIR / "mcp_trust.json"
"""``~/.k8s_autopilot/.state/mcp_trust.json`` — Saved MCP project approvals."""

SKILL_TRUST_PATH: Final[Path] = STATE_DIR / "skill_trust.json"
"""``~/.k8s_autopilot/.state/skill_trust.json`` — Skill trust decisions."""

# ── Project root markers ─────────────────────────────────

PROJECT_ROOT_MARKERS: Final[tuple[str, ...]] = (
    "Chart.yaml",
    "kustomization.yaml",
    "kustomization.yml",
    "helmfile.yaml",
    "helmfile.yml",
    "skaffold.yaml",
    ".argocd",
    "kubernetes",
    "k8s",
    "deploy",
    ".git",
    "pyproject.toml",
    "Makefile",
)

# ── Keys denied in .env files ────────────────────────────

DOTENV_DENIED_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "DISPLAY",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "NODE_PATH",
        "NODE_OPTIONS",
        "HISTFILE",
        "HISTSIZE",
        "SSH_AUTH_SOCK",
        "GPG_AGENT_INFO",
        "TMPDIR",
        "TEMP",
        "TMP",
    }
)

# ── Fields reloadable via /reload ────────────────────────

RELOADABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "groq_api_key",
        "deepseek_api_key",
        "tavily_api_key",
        "kubeconfig",
        "kube_context",
        "kube_namespace",
        "project_root",
        "shell_allow_list",
    }
)

# K8s and DevOps specific env vars to preserve in shell execution
K8S_PRESERVE_ENV_VARS: Final[tuple[str, ...]] = (
    "KUBECONFIG",
    "KUBE_CONTEXT",
    "KUBE_NAMESPACE",
    "KUBE_CLUSTER",
    "HELM_HOME",
    "HELM_CACHE_HOME",
    "HELM_CONFIG_HOME",
    "HELM_DATA_HOME",
    "HELM_DRIVER",
    "HELM_REGISTRY_CONFIG",
    "HELM_REPOSITORY_CONFIG",
    "ARGOCD_SERVER",
    "ARGOCD_AUTH_TOKEN",
    "ARGOCD_OPTS",
    "ARGOCD_GRPC_WEB",
    "ARGOCD_SERVER_NAME",
    "PROMETHEUS_URL",
    "ALERTMANAGER_URL",
    "LOKI_URL",
    "TEMPO_URL",
    "GRAFANA_URL",
    "GRAFANA_TOKEN",
    "TRAEFIK_API_URL",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_SHARED_CREDENTIALS_FILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_TENANT_ID",
)


# ── Directory helpers ────────────────────────────────────


def agent_dir(name: str = DEFAULT_ASSISTANT_ID) -> Path:
    """Return ``~/.k8s_autopilot/{name}/``."""
    return DATA_DIR / name


def user_skills_dir(name: str = DEFAULT_ASSISTANT_ID) -> Path:
    """Return ``~/.k8s_autopilot/{name}/skills/``."""
    return agent_dir(name) / "skills"


def user_agents_dir(name: str = DEFAULT_ASSISTANT_ID) -> Path:
    """Return ``~/.k8s_autopilot/{name}/agents/``."""
    return agent_dir(name) / "agents"


def user_agent_md(name: str = DEFAULT_ASSISTANT_ID) -> Path:
    """Return ``~/.k8s_autopilot/{name}/AGENTS.md``."""
    return agent_dir(name) / "AGENTS.md"


def primary_agent_md() -> Path:
    """Return ``~/.k8s_autopilot/AGENTS.md``."""
    return DATA_DIR / "AGENTS.md"


def ensure_user_agent_md(name: str = DEFAULT_ASSISTANT_ID) -> Path:
    """Ensure ``~/.k8s_autopilot/AGENTS.md`` and ``~/.k8s_autopilot/{name}/AGENTS.md`` exist on disk.

    Returns:
        The primary persistent memory path (``~/.k8s_autopilot/AGENTS.md``).
    """
    ensure_data_dir()
    root_md = primary_agent_md()
    if not root_md.exists():
        try:
            root_md.touch()
        except OSError as e:
            logger.warning("Could not touch primary memory file %s: %s", root_md, e)
    d = ensure_agent_dir(name)
    agent_md = d / "AGENTS.md"
    if not agent_md.exists():
        try:
            agent_md.touch()
        except OSError as e:
            logger.warning("Could not touch assistant memory file %s: %s", agent_md, e)
    return root_md


def project_k8s_autopilot_dir(project_root: Path) -> Path:
    """Return ``{project_root}/.k8s_autopilot/``."""
    return project_root / ".k8s_autopilot"


def project_skills_dir(project_root: Path) -> Path:
    """Return ``{project_root}/.k8s_autopilot/skills/``."""
    return project_k8s_autopilot_dir(project_root) / "skills"


def project_agents_dir(project_root: Path) -> Path:
    """Return ``{project_root}/.k8s_autopilot/agents/``."""
    return project_k8s_autopilot_dir(project_root) / "agents"


def project_mcp_paths(project_root: Path) -> list[Path]:
    """Return candidate MCP config paths for a project, in precedence order."""
    return [
        project_root / ".mcp.json",
        project_root / "mcp.json",
        project_k8s_autopilot_dir(project_root) / ".mcp.json",
        project_k8s_autopilot_dir(project_root) / "mcp.json",
    ]


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from start to find a Kubernetes or repository root."""
    current = Path(start or Path.cwd()).expanduser().resolve()
    for parent in [current, *current.parents]:
        for marker in PROJECT_ROOT_MARKERS:
            if (parent / marker).exists():
                return parent
    return current


# ── Ensure directories exist ─────────────────────────────


def ensure_data_dir() -> Path:
    """Create ``~/.k8s_autopilot/`` if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def ensure_state_dir() -> Path:
    """Create ``~/.k8s_autopilot/.state/`` if it doesn't exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def ensure_agent_dir(name: str = DEFAULT_ASSISTANT_ID) -> Path:
    """Create ``~/.k8s_autopilot/{name}/`` if it doesn't exist."""
    d = agent_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_conversation_history_dir() -> Path:
    """Create ``~/.k8s_autopilot/conversation_history/`` if it doesn't exist."""
    CONVERSATION_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return CONVERSATION_HISTORY_DIR


def plugins_dir() -> Path:
    """Return ``~/.k8s_autopilot/plugins/``."""
    return PLUGINS_DIR


def ensure_plugins_dir() -> Path:
    """Create ``~/.k8s_autopilot/plugins/`` if it doesn't exist."""
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    return PLUGINS_DIR


def upsert_env_vars(env_dict: dict[str, str], env_path: Path | None = None) -> bool:
    """Upsert key-value pairs into a .env file atomically."""
    import contextlib
    import tempfile

    target_path = env_path or GLOBAL_ENV_PATH
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if target_path.exists():
            content = target_path.read_text(encoding="utf-8")
            lines = content.splitlines()

        new_lines: list[str] = []
        seen_keys: set[str] = set()

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            raw_key = stripped.removeprefix("export ").split("=", 1)[0].strip()
            if not raw_key:
                new_lines.append(line)
                continue

            if raw_key in env_dict:
                if raw_key not in seen_keys:
                    new_lines.append(f"{raw_key}={env_dict[raw_key]}")
                    seen_keys.add(raw_key)
            else:
                if raw_key not in seen_keys:
                    new_lines.append(line)
                    seen_keys.add(raw_key)

        for k, v in env_dict.items():
            if k not in seen_keys:
                new_lines.append(f"{k}={v}")
                seen_keys.add(k)

        final_content = "\n".join(new_lines).strip() + "\n" if new_lines else ""

        fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(final_content)
            Path(tmp_path).replace(target_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
        return True
    except Exception as exc:
        logger.exception("Failed to upsert env vars in %s: %s", target_path, exc)
        return False


def delete_env_vars(keys: list[str] | tuple[str, ...], env_path: Path | None = None) -> bool:
    """Remove key-value pairs from a .env file atomically."""
    import contextlib
    import tempfile

    target_path = env_path or GLOBAL_ENV_PATH
    if not target_path.exists():
        return True

    keys_to_remove = set(keys)
    try:
        content = target_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            raw_key = stripped.removeprefix("export ").split("=", 1)[0].strip()
            if raw_key in keys_to_remove:
                continue
            new_lines.append(line)

        final_content = "\n".join(new_lines).strip() + "\n" if new_lines else ""
        fd, tmp_path = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(final_content)
            Path(tmp_path).replace(target_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
        return True
    except Exception as exc:
        logger.exception("Failed to delete env vars from %s: %s", target_path, exc)
        return False


# ── Path classification ──────────────────────────────────

_MISSING_ERRNOS = {errno.ENOENT, errno.ENOTDIR}


class PathState(StrEnum):
    """Whether a probed path exists, is absent, or could not be read."""

    EXISTS = "exists"
    MISSING = "missing"
    UNREADABLE = "unreadable"


def classify_path(path: Path) -> PathState:
    """Classify a path as existing, missing, or unreadable."""
    try:
        path.stat()
    except OSError as exc:
        if exc.errno in _MISSING_ERRNOS:
            return PathState.MISSING
        logger.debug("Could not stat %s", path, exc_info=True)
        return PathState.UNREADABLE
    else:
        return PathState.EXISTS
