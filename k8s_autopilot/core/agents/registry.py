"""
Filesystem-based subagent discovery for K8s Autopilot deep agents.

Ported from dcode's ``subagents.py``, upgraded to use the pydantic
``AgentConfig`` schema from ``agent_config.py``.

Directory convention (plugin architecture)::

    plugins/{domain}/agents/{subagent_name}/
        config.yaml        ← validated by AgentConfig pydantic schema
        prompts/system.md  ← static system prompt (or fallback for composer)
        AGENTS.md          ← role-specific memory (injected into context)
        skills/            ← agent-specific skills

Design:
    - Generic and reusable across ALL deep agent coordinators
    - No domain-specific logic — config.yaml schema drives behaviour
    - Parsing errors are logged-and-skipped, never crash the agent
    - ``SubagentSpec`` pairs the validated ``AgentConfig`` with the
      loaded system prompt and filesystem metadata

Reference: dcode/code/subagents.py, k8s_autopilot/core/agents/agent_config.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from k8s_autopilot.core.agents.agent_config import AgentConfig, load_agent_config
from k8s_autopilot.utils.logger import AgentLogger

logger = AgentLogger("SubagentRegistry")


# ---------------------------------------------------------------------------
# SubagentSpec — paired config + system prompt from filesystem
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SubagentSpec:
    """Resolved subagent: validated ``AgentConfig`` + system prompt.

    The ``config`` holds everything from ``config.yaml`` (identity, tools,
    HITL, middleware).  The ``system_prompt`` is loaded from
    ``prompts/system.md`` alongside it.  The ``memory`` is loaded from
    ``AGENTS.md`` at the agent root.

    Attributes:
        config: Validated pydantic ``AgentConfig`` from ``config.yaml``.
        system_prompt: Content of ``prompts/system.md``.
        agent_memory: Content of ``AGENTS.md`` (role-specific memory).
        source: Discovery source label (e.g., ``"plugins"``).
        path: Absolute path to the agent directory.
    """

    config: AgentConfig
    system_prompt: str
    agent_memory: str
    source: str
    path: str

    # Convenience properties that delegate to config
    @property
    def name(self) -> str:
        return self.config.name

    @property
    def description(self) -> str:
        return self.config.description

    @property
    def prompt_composer(self) -> str | None:
        return self.config.prompt_composer


# ---------------------------------------------------------------------------
# Parsing — load config.yaml + prompts/system.md from an agent directory
# ---------------------------------------------------------------------------

def _load_subagent_from_dir(
    agent_dir: Path,
    *,
    fallback_name: str | None = None,
) -> SubagentSpec | None:
    """Load a subagent from its directory.

    Expected structure::

        agent_dir/
            config.yaml        (required)
            prompts/system.md  (required)
            AGENTS.md          (optional — role-specific memory)

    Args:
        agent_dir: Path to the agent directory.
        fallback_name: Name to use if config.yaml omits ``name``.

    Returns:
        ``SubagentSpec`` if loading succeeds, ``None`` otherwise.
    """
    config_path = agent_dir / "config.yaml"
    prompt_path = agent_dir / "prompts" / "system.md"
    memory_path = agent_dir / "AGENTS.md"

    # ── Validate required files exist ──────────────────────────────────
    if not config_path.exists():
        logger.warning(
            f"Skipping agent {agent_dir.name}: missing config.yaml"
        )
        return None

    if not prompt_path.exists():
        logger.warning(
            f"Skipping agent {agent_dir.name}: missing prompts/system.md"
        )
        return None

    # ── Load and validate config.yaml ──────────────────────────────────
    try:
        config = load_agent_config(config_path)
    except ValidationError as exc:
        logger.warning(
            f"Skipping agent {agent_dir.name}: config.yaml validation failed — {exc}"
        )
        return None
    except Exception as exc:
        logger.warning(
            f"Skipping agent {agent_dir.name}: failed to load config.yaml — {exc}"
        )
        return None

    # ── Load system prompt ─────────────────────────────────────────────
    try:
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(
            f"Skipping agent {agent_dir.name}: could not read prompts/system.md — {exc}"
        )
        return None

    # ── Load agent memory (optional) ───────────────────────────────────
    agent_memory = ""
    if memory_path.exists():
        try:
            agent_memory = memory_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                f"Agent {agent_dir.name}: could not read AGENTS.md — {exc}"
            )

    return SubagentSpec(
        config=config,
        system_prompt=system_prompt,
        agent_memory=agent_memory,
        source="",  # Set by caller
        path=str(agent_dir),
    )


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------

def _load_subagents_from_dir(
    agents_dir: Path,
    source: str,
) -> dict[str, SubagentSpec]:
    """Load subagents from ``agents_dir/{name}/config.yaml``.

    Mirrors dcode's ``_load_subagents_from_dir`` with identical collision
    detection and stray-file warnings, but parses ``config.yaml`` +
    ``prompts/system.md`` instead of ``AGENTS.md`` frontmatter.
    """
    subagents: dict[str, SubagentSpec] = {}

    if not agents_dir.is_dir():
        return subagents

    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir():
            continue

        spec = _load_subagent_from_dir(entry, fallback_name=entry.name)
        if not spec:
            continue

        # Replace source field (frozen dataclass, so reconstruct)
        spec = SubagentSpec(
            config=spec.config,
            system_prompt=spec.system_prompt,
            agent_memory=spec.agent_memory,
            source=source,
            path=spec.path,
        )

        existing = subagents.get(spec.name)
        if existing is not None:
            logger.warning(
                f"Subagent name collision in {agents_dir}: "
                f"{existing.path} and {spec.path} both resolve to "
                f"name={spec.name!r}. Using {spec.path}."
            )
        subagents[spec.name] = spec

    return subagents


# ---------------------------------------------------------------------------
# Public API — list_subagents
# ---------------------------------------------------------------------------

def list_subagents(
    *,
    agents_dirs: list[Path] | None = None,
) -> list[SubagentSpec]:
    """Discover subagent definitions from one or more directories.

    Directories are scanned in order; later directories override earlier
    ones when names collide (project overrides user/global).

    Args:
        agents_dirs: Ordered list of directories to scan.
            Each must contain ``{subagent_name}/config.yaml`` subdirectories.

    Returns:
        List of ``SubagentSpec`` in discovery order.
    """
    all_subagents: dict[str, SubagentSpec] = {}

    for agents_dir in (agents_dirs or []):
        source = agents_dir.parent.name  # e.g. "helm-operator"
        all_subagents.update(_load_subagents_from_dir(agents_dir, source))

    logger.info(
        f"SubagentRegistry: discovered {len(all_subagents)} subagent(s) "
        f"from {len(agents_dirs or [])} directory(ies): "
        f"{[s.name for s in all_subagents.values()]}"
    )
    return list(all_subagents.values())

def get_domain_agents_dir(domain: str) -> Path:
    """Return the conventional agents directory for a domain.

    Convention: ``<project_root>/plugins/<domain>/agents/``
    """
    from k8s_autopilot.core.backend import get_project_root

    return get_project_root() / "plugins" / domain / "agents"


# Compatibility alias
SubagentMetadata = SubagentSpec
