"""
Helm Operator — Modular middleware and subagent assembly.

Provides config-driven middleware builders for Helm operator subagents and
HITL (Human-in-the-Loop) gates.  The coordinator's middleware stack is now
built inline in ``HelmOperatorCoordinator._build_middleware()`` (dcode pattern).

Subagent middleware catalogue:
    SkillsMiddleware          — auto-loads SKILL.md from config.yaml paths
    MemoryMiddleware          — injects AGENTS.md from plugin directories
    CodeInterpreterMiddleware — QuickJS PTC with config-driven allowlist

Usage::

    from k8s_autopilot.core.agents.helm_operator.middleware import (
        build_dynamic_subagent_spec,
    )

    spec = build_dynamic_subagent_spec(subagent_spec, mcp_tools=tools)

Reference: aws-orchestrator-agent tf_operator/middleware.py
"""
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING, cast

from k8s_autopilot.utils.logger import AgentLogger

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("K8sMiddleware")


# ---------------------------------------------------------------------------
# Layer 1: SkillExistsShortcut — deterministic planner bypass
# ---------------------------------------------------------------------------
#
# When skill files (e.g. /skills/helm-operator/nginx-chart-generator/SKILL.md)
# already exist in state["files"], the helm-planner and helm-skill-builder
# sub-agents are redundant.  This middleware:
#
#   1. REMOVES helm-planner & helm-skill-builder from the model's tool list
#      → the LLM physically cannot call them (deterministic guarantee)
#   2. INJECTS a SystemMessage directing the coordinator to skip to generator
#      → reinforces the routing decision (belt & suspenders)
#
# This fixes a production bug where the coordinator prompt's skill-exists
# check was unreliably followed by the model (it would call helm-planner
# even when skills were pre-loaded in state).
#
# Pattern: LangChain docs — Filtering pre-registered tools
#          https://docs.langchain.com/oss/python/langchain/tools#filtering-pre-registered-tools
# Pattern: LangChain docs — Dynamic prompt via @wrap_model_call
#          https://docs.langchain.com/oss/python/langchain/middleware/custom#dynamic-prompt
# ---------------------------------------------------------------------------

# Custom middlewares migrated to k8s_autopilot/core/middleware/helm_operator.py


# ---------------------------------------------------------------------------
# Helm HITL — Official HumanInTheLoopMiddleware
# ---------------------------------------------------------------------------
#
# Uses LangChain's built-in HumanInTheLoopMiddleware instead of a custom
# interrupt()-based implementation. This provides:
#   - Structured decision handling (approve / edit / reject)
#   - Proper Command(resume={"decisions": [...]}) integration
#   - Action batching when multiple tools trigger simultaneously
#   - Standard deep-agent harness compatibility
#
# The ``description`` callables generate rich, per-operation approval cards
# so the human reviewer sees chart name, release, namespace, etc.
#
# Reference: https://docs.langchain.com/oss/python/langchain/human-in-the-loop
# ---------------------------------------------------------------------------

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig



def _build_approval_description(tool_name: str, tool_args: Dict[str, Any]) -> str:
    """Build a dynamic, generic, human-readable description for any tool approval card.

    Formats the header based on the action, and outputs all arguments as formatted key-value pairs.
    """
    action = tool_name.replace("_", " ").upper()
    if "UNINSTALL" in action:
        header = "UNINSTALL APPROVAL REQUIRED"
    elif "INSTALL" in action:
        header = "INSTALLATION APPROVAL REQUIRED"
    elif "UPGRADE" in action:
        header = "UPGRADE APPROVAL REQUIRED"
    elif "ROLLBACK" in action:
        header = "ROLLBACK APPROVAL REQUIRED"
    else:
        header = f"{action} APPROVAL REQUIRED"

    args_lines = []
    for k, v in sorted(tool_args.items()):
        key_display = k.replace("_", " ").title()
        args_lines.append(f"**{key_display}**: {v}")

    args_block = "\n".join(args_lines)
    return f"⚠️ **{header}**\n\n{args_block}"



# ---------------------------------------------------------------------------
# Dynamic subagent spec builder — frontmatter-driven assembly
# ---------------------------------------------------------------------------

# Prompt composer registry — maps frontmatter ``prompt_composer`` values
# to callables.  Add entries here when new subagents need dynamic prompts.
_PROMPT_COMPOSERS: Dict[str, Callable[..., str]] = {}


def register_prompt_composer(name: str, composer: Callable[..., str]) -> None:
    """Register a callable that generates a subagent's system prompt dynamically.

    Called once during module initialisation from prompt_sections.py.
    """
    _PROMPT_COMPOSERS[name] = composer


# Register known helm prompt composers on first import
def _ensure_prompt_composers_registered() -> None:
    """Lazy registration to avoid circular imports."""
    pass


from k8s_autopilot.core.tools.registry import get_tool_registry

def register_extra_tool_factory(name: str, factory: Callable[..., Any]) -> None:
    """Register a callable that creates a named extra tool."""
    get_tool_registry().register(name, factory)



def build_dynamic_subagent_spec(
    spec: Any,  # SubagentSpec from registry
    *,
    mcp_tools: Optional[List[Any]] = None,
    resource_reader: Optional[Any] = None,
    coordinator_model: Optional[Any] = None,
    validator_model: Optional[Any] = None,
    config: Optional[Any] = None,
    backend: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a complete subagent dict spec from ``SubagentSpec``.

    This replaces the old ``get_helm_subagent_specs()`` + ``build_mcp_subagent()``
    pattern with a single, config.yaml-driven builder that:

    1. Resolves ``prompt_composer`` → dynamic system prompt (or uses static)
    2. Injects pre-resolved MCP tools from ``MCPSessionManager``
    3. Resolves ``tools.extra`` from the tool factory registry with safe parameter passing
    4. Assembles middleware from the declarative ``middleware`` list via MiddlewareRegistry
    5. Translates ``human_in_the_loop`` middleware into the ``interrupt_on`` configuration dict
    6. Sets model override from config.yaml or coordinator fallback
    7. Appends agent memory (AGENTS.md) to the system prompt

    No if/else chains — all behaviour is driven by the config.yaml schema.

    Args:
        spec: ``SubagentSpec`` from the filesystem registry (wraps ``AgentConfig``).
        mcp_tools: Pre-resolved MCP tools to inject (from session manager filter).
        resource_reader: A ``read_mcp_resource`` tool for this subagent.
        coordinator_model: Default model for the subagent.
        validator_model: Cheaper model for validator subagents.

    Returns:
        A dict spec ready for ``create_deep_agent(subagents=[...])``.
    """
    import inspect
    from k8s_autopilot.core.middleware.registry import get_middleware_registry

    _ensure_prompt_composers_registered()

    cfg = spec.config  # AgentConfig from config.yaml

    # ── 1. System prompt (composed or static) ─────────────────────────
    system_prompt = spec.system_prompt
    if cfg.prompt_composer and cfg.prompt_composer in _PROMPT_COMPOSERS:
        system_prompt = _PROMPT_COMPOSERS[cfg.prompt_composer]()

    # ── 2. Model selection ────────────────────────────────────────────
    model = cfg.model
    if not model:
        is_validator = "validator" in cfg.name.lower()
        model = validator_model if is_validator else coordinator_model

    # ── 3. Tools assembly ─────────────────────────────────────────────
    tools: List[Any] = list(mcp_tools or [])

    if resource_reader:
        tools.append(resource_reader)

    for tool_spec in cfg.tools.extra:
        try:
            tool_instance = get_tool_registry().build_tool(
                name=tool_spec.name,
                **tool_spec.config
            )
            tools.append(tool_instance)
        except KeyError:
            logger.warning(
                f"Subagent {cfg.name}: unknown extra_tool '{tool_spec.name}'. "
                f"Register it via get_tool_registry().register()."
            )

    # ── 4. Build spec dict ────────────────────────────────────────────
    result: Dict[str, Any] = {
        "name": cfg.name,
        "description": cfg.description,
        "system_prompt": system_prompt,
        "tools": tools,
    }

    if model:
        result["model"] = model

    # ── 5. HITL — pass interrupt_on config (not explicit middleware) ───
    hitl_spec = next((mw for mw in cfg.middleware if mw.name == "human_in_the_loop"), None)
    if hitl_spec:
        tools_config = hitl_spec.config.get("tools", [])
        interrupt_on_dict = {}
        for tool_item in tools_config:
            if isinstance(tool_item, dict):
                tool_name = tool_item.get("name")
                decisions = tool_item.get("decisions", ["approve", "reject"])
            else:
                tool_name = tool_item
                decisions = ["approve", "reject"]
            
            if tool_name:
                interrupt_on_dict[tool_name] = InterruptOnConfig(
                    allowed_decisions=cast(Any, decisions),
                    description=lambda tool_call, state, runtime, tn=tool_name: _build_approval_description(
                        tn, tool_call.get("args", {}),
                    ),
                )
        result["interrupt_on"] = interrupt_on_dict

    # ── 6. Dynamically build all middleware listed in config.yaml ─────
    middleware_instances: List[Any] = []
    for mw_spec in cfg.middleware:
        if mw_spec.name == "human_in_the_loop":
            continue  # Handled above via interrupt_on
        
        try:
            mw_instance = get_middleware_registry().build_middleware(
                name=mw_spec.name,
                model=model,
                config=config,
                backend=backend,
                **mw_spec.config
            )
            middleware_instances.append(mw_instance)
        except Exception as e:
            logger.error(f"Failed to build middleware '{mw_spec.name}': {e}")
            raise

    if middleware_instances:
        result["middleware"] = middleware_instances

    logger.info(
        f"build_dynamic_subagent_spec: {cfg.name} → "
        f"{len(tools)} tool(s), {len(middleware_instances)} middleware(s), "
        f"interrupt_on={'yes' if 'interrupt_on' in result else 'no'}, "
        f"prompt_composer={'yes' if cfg.prompt_composer else 'no'}"
    )

    return result


def _extract_domain_from_name(subagent_name: str) -> str:
    """Extract the domain prefix from a subagent name.

    Examples:
        'helm-operation' → 'helm'
        'github-agent' → 'github'
        'helm-generator' → 'helm'
    """
    return subagent_name.split("-")[0]

