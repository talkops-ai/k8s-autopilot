"""
Helm Operator Deep Agent Coordinator.

Production-grade implementation of the deep agent pattern for Helm chart
generation, updates, and live cluster operations. Wires backends, MCP tools,
and subagents via the ``BaseDeepAgent`` abstract class.

Architecture (dcode-aligned):
    - **Dynamic subagent discovery**: Filesystem-based ``SubagentRegistry``
      reads ``memory/helm-operator/agents/{name}/AGENTS.md`` to discover
      subagent specs with YAML frontmatter.
    - **Startup MCP sessions**: ``MCPSessionManager`` connects to all
      domain-bound MCP servers at ``build_agent()`` time — no JIT connections.
      Graceful degradation: if a server is unreachable, the agent logs the
      error and continues with available servers.
    - **Frontmatter-driven middleware**: HITL gates, PTC allowlists, and
      extra tools are assembled from frontmatter fields — no hardcoded
      if/else chains.
    - ``CompositeBackend`` with route-based storage (memories → StoreBackend,
      workspace → StateBackend)
    - Middleware safety nets (tool/model call limits)

Reference: dcode/code/agent.py, aws-orchestrator-agent tf_operator/tf_cordinator.py
Docs: https://docs.langchain.com/oss/python/deepagents/customization
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from typing import TYPE_CHECKING, cast
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage

from langchain_core.runnables.config import RunnableConfig
from langchain.tools import tool, ToolRuntime
from langchain_core.tools import StructuredTool
from langgraph.types import Command, interrupt
from langgraph.store.memory import InMemoryStore
from k8s_autopilot.core.hitl.checkpointer import get_checkpointer
from langchain_core.messages import ToolMessage
from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data
from k8s_autopilot.core.agents.types import BaseDeepAgent
from deepagents import DeepAgentState
from k8s_autopilot.utils.llm import create_model, create_model_with_result
from k8s_autopilot.core.prompts import PromptContext
from k8s_autopilot.utils.user_input_tool import (
    create_user_input_tool,
)
from k8s_autopilot.utils.operations_context import create_log_operation_tool
from k8s_autopilot.utils.escalate_tool import create_escalate_to_supervisor_tool
from k8s_autopilot.core.state.helm_operator_state import HelmOperatorContext
from k8s_autopilot.core.agents.helm_operator.middleware import (
    build_dynamic_subagent_spec,
)
from k8s_autopilot.core.agents.registry import (
    list_subagents,
    get_domain_agents_dir,
)
from k8s_autopilot.core.mcp.session_manager import MCPSessionManager
import k8s_autopilot.core.agents.profiles  # noqa: F401 — side-effect registration
from k8s_autopilot.core.agents.profiles import register_domain_profiles
register_domain_profiles("helm")
from k8s_autopilot.core.backend import (
    K8sBackendMixin,
    get_project_root,
    sync_workspace_to_disk,
)
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.utils.domain_summary import extract_domain_summary
from k8s_autopilot.core.state.handoff_contracts import extract_handoff_from_text

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("HelmOperatorCoordinator")



def _build_dynamic_capabilities(domain_name: str) -> str:
    import re
    from k8s_autopilot.core.agents.registry import get_domain_agents_dir, list_subagents
    try:
        agents_dir = get_domain_agents_dir(domain_name)
        subagent_metas = list_subagents(agents_dirs=[agents_dir])
        
        lines = ["<capabilities>"]
        for meta in subagent_metas:
            desc = (meta.description or "").strip().replace("\n", " ")
            lines.append(f"- {meta.name}: {desc}")
            
        lines.append("\nSub-agents auto-load their SKILL.md files. You do NOT need to instruct them to read skills.")
        lines.append("The `task` tool REQUIRES a `ctx` parameter — always pass `{}`.")
        lines.append("All sub-agents have access to `request_human_input` for HITL gates.")
        lines.append("</capabilities>")
        return "\n".join(lines)
    except Exception:
        return "<capabilities></capabilities>"


def _get_static_coordinator_prompt() -> str:
    import re
    from pathlib import Path
    from k8s_autopilot.core.backend import get_project_root
    from k8s_autopilot.core.prompts import create_default_resolver, PromptSlot, PromptContext

    root = get_project_root()
    coord_file = root / "plugins" / "helm-operator" / "prompts" / "coordinator.md"
    domain_sections = coord_file.read_text(encoding="utf-8") if coord_file.exists() else ""

    # Dynamically inject capabilities XML block
    dyn_caps = _build_dynamic_capabilities("helm-operator")
    domain_sections = re.sub(
        r"<capabilities>.*?</capabilities>",
        dyn_caps,
        domain_sections,
        flags=re.DOTALL
    )

    template_path = Path(__file__).parent.parent.parent / "prompts" / "templates" / "helm_coordinator.md"
    resolver = create_default_resolver(template_path)

    resolver.register_slot(PromptSlot(
        name="domain_sections",
        resolver=lambda _ctx: domain_sections,
    ))

    # Resolve with empty/default context
    return resolver.resolve(PromptContext())

HELM_COORDINATOR_PROMPT = _get_static_coordinator_prompt()




import hashlib
import subprocess
import json

def compute_workspace_hash(workspace_dir: str) -> str:
    """Recursively compute SHA-256 hash of all files in workspace, excluding metadata."""
    workspace_path = Path(workspace_dir)
    if not workspace_path.exists():
        return ""
    
    sha = hashlib.sha256()
    for root, dirs, files in os.walk(workspace_path):
        if ".git" in root or ".last-update.json" in root:
            continue
        for file in sorted(files):
            if file == ".last-update.json" or file.startswith("."):
                continue
            file_path = Path(root) / file
            try:
                rel_path = file_path.relative_to(workspace_path).as_posix()
                sha.update(rel_path.encode("utf-8"))
                sha.update(file_path.read_bytes())
            except Exception:
                pass
    return sha.hexdigest()

def is_git_clean(workspace_dir: str) -> bool:
    """Check if git status is clean for the workspace directory."""
    try:
        res = subprocess.run(
            ["git", "status", "--short", "--", workspace_dir],
            capture_output=True,
            text=True,
            check=False
        )
        return len(res.stdout.strip()) == 0
    except Exception:
        return True

class HelmOperatorState(DeepAgentState):
    directory_hash: str
    last_execution_status: str

# ---------------------------------------------------------------------------
# Helm Resource Description (for read_mcp_resource tool)
# ---------------------------------------------------------------------------

_HELM_RESOURCE_DESCRIPTION = (
    "Read content of a specific MCP resource by URI "
    "(server: helm_mcp_server). Use this to read "
    "helm releases, chart metadata, and cluster state natively.\n\n"
    "STRICT URI FORMAT RULES:\n"
    "You MUST use exactly one of these formats. DO NOT append `/values`, `?namespace=`, or guess URIs.\n"
    "- `helm://releases`\n"
    "- `helm://releases/[release_name]` (WARNING: namespace filtering is NOT supported. NEVER put namespace in URI)\n"
    "- `helm://charts`\n"
    "- `helm://charts/[repo]/[name]`\n"
    "- `helm://charts/[repo]/[name]/readme`\n"
    "- `kubernetes://cluster-info`\n"
    "- `kubernetes://namespaces`\n"
    "- `helm://best_practices`"
)


# ---------------------------------------------------------------------------
# HelmOperatorCoordinator — the deep agent
# ---------------------------------------------------------------------------

class HelmOperatorCoordinator(BaseDeepAgent):
    """
    Helm Operator Deep Agent Coordinator.

    Production implementation of the deep agent pattern that:
    - Inherits lifecycle from ``BaseDeepAgent``
    - Uses ``K8sOperatorBackendMixin`` for Helm-specific backend routing
    - Dynamically discovers subagents from filesystem ``AGENTS.md`` files
    - Connects to MCP servers at startup (graceful degradation on failure)
    - Manages sub-agents with frontmatter-driven tool/middleware injection
    - Supports HITL approval gates before destructive operations
    - Implements ``input_transform`` / ``output_transform`` for subgraph state bridging
    """
    domain_name = "helm-operator"

    def __init__(
        self,
        config: Optional["Config"] = None,
        *,
        mcp_server_filter: Optional[List[str]] = None,
    ) -> None:
        super().__init__(config=config)
        self._mcp_server_filter = mcp_server_filter
        self._mcp_session_manager: Optional[MCPSessionManager] = None

        logger.info("HelmOperatorCoordinator initialized")

    @property
    def model_result(self) -> Any:
        if not hasattr(self, "_model_result_cached"):
            self._model_result_cached = create_model_with_result(self._config.get_llm_deepagent_config())
        return self._model_result_cached

    @property
    def validator_result(self) -> Any:
        if not hasattr(self, "_validator_result_cached"):
            self._validator_result_cached = create_model_with_result(self._config.get_llm_config())
        return self._validator_result_cached

    # ── Abstract implementations — Properties ────────────────────────────

    @property
    def name(self) -> str:
        return "helm-operator-coordinator"

    @property
    def system_prompt(self) -> str:
        ctx = PromptContext(
            mode=self.get_interaction_mode(),
            model_name=self.model_result.model_name,
            model_provider=self.model_result.provider,
            context_limit=self.model_result.context_limit,
            unsupported_modalities=list(self.model_result.unsupported_modalities),
            skill_paths=self.get_skill_paths(),
            config=self._config
        )

        import re
        from k8s_autopilot.core.backend import get_project_root
        from k8s_autopilot.core.prompts import create_default_resolver, PromptSlot
        
        coord_file = get_project_root() / "plugins" / self.domain_name / "prompts" / "coordinator.md"
        domain_sections = coord_file.read_text(encoding="utf-8") if coord_file.exists() else ""
        
        # Inject dynamic capabilities
        dyn_caps = _build_dynamic_capabilities(self.domain_name)
        domain_sections = re.sub(
            r"<capabilities>.*?</capabilities>",
            dyn_caps,
            domain_sections,
            flags=re.DOTALL
        )
        
        template_path = Path(__file__).parent.parent.parent / "prompts" / "templates" / "helm_coordinator.md"
        resolver = create_default_resolver(template_path)
        
        resolver.register_slot(PromptSlot(
            name="domain_sections",
            resolver=lambda _ctx: domain_sections,
        ))
        
        return resolver.resolve(ctx)

    def get_task_categories(self) -> str:
        """Helm Operator domain-specific task categories."""
        return """\
- **Discovery**: Chart metadata lookup, release listing, version discovery
- **Configuration**: Requirements analysis, architecture planning, skill building
- **Validation**: helm lint, helm template, schema validation, dry-run
- **Live Apply**: Helm install, upgrade, rollback, uninstall
- **Health Check**: Release status verification, pod health, rollout readiness
- **Summary**: Generate walkthrough narrative from execution results"""

    @property
    def context_schema(self) -> type:
        return HelmOperatorContext

    # ── Abstract implementations — Model ─────────────────────────────────

    def get_model(self) -> Any:
        """Return an initialized deep-agent tier LLM model."""
        return self.model_result.model

    def _get_validator_model(self) -> Any:
        """Return an initialized standard-tier LLM for the chart-validator."""
        return self.validator_result.model

    # ── Abstract implementations — Sub-agents (DYNAMIC) ───────────────────

    async def get_subagent_specs(self) -> List[Any]:
        """
        Build sub-agent specs dynamically from filesystem AGENTS.md files.

        Replaces the legacy ``get_helm_subagent_specs()`` with:
        1. ``SubagentRegistry.list_subagents()`` — discover from filesystem
        2. ``MCPSessionManager.filter_tools_by_server()`` — inject pre-resolved MCP tools
        3. ``build_dynamic_subagent_spec()`` — frontmatter-driven assembly
        """
        from pydantic import BaseModel, Field
        from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig
        from k8s_autopilot.core.middleware.registry import get_middleware_registry
        from k8s_autopilot.core.agents.helm_operator.middleware import _build_approval_description

        agents_dir = get_domain_agents_dir(self.domain_name)
        subagent_metas = list_subagents(agents_dirs=[agents_dir])

        specs: List[Any] = []
        self._nested_agent_tools = []

        for meta in subagent_metas:
            # Filter MCP tools for this subagent based on its mcp_servers frontmatter
            mcp_tools: List[Any] = []
            resource_reader = None

            meta_mcp_servers = [m.server_name for m in meta.config.tools.mcp]
            if meta_mcp_servers and self._mcp_session_manager:
                mcp_tools = self._mcp_session_manager.filter_tools_by_server(
                    meta_mcp_servers
                )
                # Create resource reader for the primary MCP server
                primary_server = meta_mcp_servers[0]
                resource_desc = (
                    _HELM_RESOURCE_DESCRIPTION
                    if primary_server == "helm_mcp_server"
                    else None
                )
                resource_reader = self._mcp_session_manager.create_resource_reader(
                    primary_server,
                    description=resource_desc,
                )

            if meta.config.agent_type == "deep":
                # 1. Scan its nested agents/ directory
                nested_dir = Path(meta.path) / "agents"
                nested_metas = list_subagents(agents_dirs=[nested_dir])

                # 2. Compile each child recursively
                from deepagents import SubAgent
                compiled_children: List[SubAgent] = []
                for child_meta in nested_metas:
                    child_mcp_tools = []
                    child_resource_reader = None
                    child_mcp_servers = [m.server_name for m in child_meta.config.tools.mcp]
                    if child_mcp_servers and self._mcp_session_manager:
                        child_mcp_tools = self._mcp_session_manager.filter_tools_by_server(child_mcp_servers)
                        child_primary = child_mcp_servers[0]
                        child_resource_desc = (
                            _HELM_RESOURCE_DESCRIPTION
                            if child_primary == "helm_mcp_server"
                            else None
                        )
                        child_resource_reader = self._mcp_session_manager.create_resource_reader(
                            child_primary,
                            description=child_resource_desc,
                        )

                    child_spec = build_dynamic_subagent_spec(
                        child_meta,
                        mcp_tools=child_mcp_tools,
                        resource_reader=child_resource_reader,
                        coordinator_model=self.get_model(),
                        validator_model=self._get_validator_model(),
                        config=self._config,
                        backend=self.make_backend(),
                    )
                    compiled_children.append(cast(SubAgent, child_spec))

                # 3. Build spec for the coordinator (helm-coder)
                parent_spec = build_dynamic_subagent_spec(
                    meta,
                    mcp_tools=mcp_tools,
                    resource_reader=resource_reader,
                    coordinator_model=self.get_model(),
                    validator_model=self._get_validator_model(),
                    config=self._config,
                    backend=self.make_backend(),
                )

                # 4. Compile the nested subagents list using create_deep_agent
                compiled_nested_agent = create_deep_agent(
                    model=parent_spec.get("model", self.get_model()),
                    name=meta.name,
                    system_prompt=parent_spec["system_prompt"],
                    tools=parent_spec.get("tools", []),
                    subagents=cast(Any, compiled_children),
                    backend=self.make_backend(),
                    store=getattr(self, "_store", None),
                    checkpointer=self.build_checkpointer(),
                    middleware=parent_spec.get("middleware", []),
                )

                # 5. Wrap in CompiledSubAgent and append to specs
                from deepagents import CompiledSubAgent
                nested_agent_spec = CompiledSubAgent(
                    name=meta.name,
                    description=meta.description,
                    runnable=compiled_nested_agent,
                )
                specs.append(nested_agent_spec)
            else:
                spec = build_dynamic_subagent_spec(
                    meta,
                    mcp_tools=mcp_tools,
                    resource_reader=resource_reader,
                    coordinator_model=self.get_model(),
                    validator_model=self._get_validator_model(),
                    config=self._config,
                    backend=self.make_backend(),
                )
                specs.append(spec)

        logger.info(
            f"get_subagent_specs: {len(specs)} subagent(s) assembled dynamically",
            extra={"names": [s["name"] for s in specs]},
        )
        return specs

    # ── Virtual overrides ─────────────────────────────────────────────────

    async def get_tools(self) -> List[Any]:
        """
        Coordinator-level tools.

        - ``sync_workspace``: Materialises virtual /workspace/ files to the
          real filesystem.
        - ``request_user_input``: Generic HITL gate — pause and ask the user
          anything (commit approval, next steps, clarification, etc.).
        """

        @tool
        def sync_workspace(
            runtime: ToolRuntime,
        ) -> str:
            """Sync virtual /workspace/ files to real disk.

            MUST be called after helm-generator finishes and BEFORE helm-validator runs.
            This materialises the generated chart files from the virtual filesystem
            to the real project directory so helm CLI commands can access them.

            Returns a summary of synced files.
            """
            # Read the files dict from the deep agent's current state
            state_files: Dict[str, Any] = {}
            if hasattr(runtime, "state") and isinstance(runtime.state, dict):
                state_files = runtime.state.get("files", {})
            elif hasattr(runtime, "state") and hasattr(runtime.state, "get"):
                state_files = runtime.state.get("files", {})

            if not state_files:
                return (
                    "No /workspace/ files found in state to sync. "
                    "Ensure helm-generator has completed before calling sync_workspace."
                )

            synced = sync_workspace_to_disk(state_files)

            if not synced:
                return (
                    "No /workspace/ files found in state to sync. "
                    "Files may use a different path prefix."
                )

            synced_list = "\n".join(
                f"  - {vpath} → {real}" for vpath, real in synced.items()
            )
            return (
                f"Synced {len(synced)} file(s) to disk:\n{synced_list}\n\n"
                "helm-validator can now run helm lint against the real filesystem. "
                "Use execute() with relative paths (no leading /) for helm commands."
            )

        # Build the generic user input HITL tool
        user_input = create_user_input_tool()
        
        # Build the operations journal tool for context persistence
        log_operation = create_log_operation_tool()

        # Build the escalation tool for cross-domain re-routing
        escalate = create_escalate_to_supervisor_tool()

        return [sync_workspace, user_input, log_operation, escalate]

    def get_skill_paths(self) -> List[str]:
        from k8s_autopilot.core.skills.registry import get_skill_registry
        registry = get_skill_registry()
        paths = registry.get_skill_sources_for_domain(self.domain_name)
        paths.extend(registry.get_skill_sources_for_domain("global"))
        paths.extend(registry.get_skill_sources_for_domain("shared"))
        return sorted(set(paths))

    def get_memory_paths(self) -> List[str]:
        from k8s_autopilot.core.memory import get_memory_registry
        registry = get_memory_registry()
        paths = []
        paths.extend(registry.get_memory_paths_for_domain_and_role(self.domain_name, "coordinator"))
        paths.extend(registry.get_memory_paths_for_domain("global"))
        paths.extend(registry.get_memory_paths_for_domain("project"))
        paths.extend(registry.get_memory_paths_for_domain("user"))
        return sorted(paths)

    def get_interrupt_config(self) -> Dict[str, Any]:
        """HITL gates: require approval before destructive file operations."""
        return {
            "delete_module": {
                "allowed_decisions": cast(list, ["approve", "edit", "reject"]),
            },
        }

    # ── Abstract implementations — Backend & Storage ─────────────────────

    def make_backend(self) -> Any:
        """Use Helm-specific backend with LocalShellBackend for CLI."""
        return K8sBackendMixin.make_backend()

    def build_store(self) -> Any:
        """InMemoryStore for cross-thread long-term memory (dev mode).

        Pre-seeds the store with memory files from disk so the deep agent's
        ``MemoryMiddleware`` can read ``/memories/helm-operator/AGENTS.md`` etc. on startup.

        Reference: TFCoordinator.build_store()
        """
        from deepagents.backends.utils import create_file_data

        store = InMemoryStore()
        project_root = get_project_root()
        memory_dir = project_root / "memory"

        # Namespace must match the StoreBackend route in make_backend().
        namespace = ("default_org",)
        
        if memory_dir.exists():
            for path in memory_dir.rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    key = path.relative_to(memory_dir).as_posix()
                    try:
                        store.put(
                            namespace,
                            key,
                            dict(create_file_data(path.read_text(encoding="utf-8"))),
                        )
                    except UnicodeDecodeError:
                        pass
            logger.info(
                "build_store: pre-seeded InMemoryStore with memory files",
                extra={"namespace": namespace, "memory_dir": str(memory_dir)},
            )
        return store

    def build_checkpointer(self) -> Any:
        """Return Postgres-backed per-thread multi-turn memory checkpointer."""
        return get_checkpointer(self._config, prefer_postgres=True)

    # ── Middleware — dcode pattern: inline, config-driven ─────────────────

    def _build_middleware(self) -> list[Any]:
        """Build the full middleware stack for the Helm coordinator.

        Follows the dcode pattern: ``SkillsMiddleware`` and ``MemoryMiddleware``
        are assembled inline with config-driven path resolution.  No shared
        middleware module dependency.

        Sources:
            Skills:  ``plugins/helm-operator/`` (contains ``skills/`` subdir)
            Memory:  Resolved via ``MemoryRegistry`` from domain registrations
            Safety:  ``ToolCallLimit``, ``ModelCallLimit`` via env vars
            Domain:  operation context, plan lock, a2ui buffer, memory guard
        """
        from deepagents.middleware import SkillsMiddleware, MemoryMiddleware
        from deepagents.backends.filesystem import FilesystemBackend
        from langchain.agents.middleware import (
            ToolCallLimitMiddleware,
            ModelCallLimitMiddleware,
        )
        from k8s_autopilot.core.backend import get_project_root
        from k8s_autopilot.core.memory import get_memory_registry, get_user_memory_path
        from k8s_autopilot.core.middleware.registry import get_middleware_registry

        middleware: list[Any] = []
        root = get_project_root()

        # ── 1. SkillsMiddleware (dcode pattern: virtual CompositeBackend) ────
        # Point to virtual path so it is routed to SkillsFilesystemBackend and maps correctly
        middleware.append(
            SkillsMiddleware(
                backend=K8sBackendMixin.make_backend(),
                sources=["/skills/helm-operator"],
            )
        )
        logger.info("Coordinator SkillsMiddleware initialized with virtual source: /skills/helm-operator")

        # ── 2. MemoryMiddleware (dcode pattern: resolve from registry) ────────
        mem_registry = get_memory_registry()
        memory_vpaths: list[str] = []
        memory_vpaths.extend(mem_registry.get_memory_paths_for_domain_and_role("helm-operator", "coordinator"))
        memory_vpaths.extend(mem_registry.get_memory_paths_for_domain("global"))
        memory_vpaths.extend(mem_registry.get_memory_paths_for_domain("project"))
        memory_vpaths.extend(mem_registry.get_memory_paths_for_domain("user"))

        physical_sources: list[str] = []
        for vp in memory_vpaths:
            phys = mem_registry.resolve_virtual_path(vp)
            if phys and phys.is_file():
                physical_sources.append(str(phys))

        if physical_sources:
            middleware.append(
                MemoryMiddleware(
                    backend=FilesystemBackend(virtual_mode=False),
                    sources=sorted(set(physical_sources)),
                    add_cache_control=True,
                )
            )
            logger.info(f"Coordinator MemoryMiddleware: {len(physical_sources)} source(s)")

        # ── 3. Domain-specific middleware from registry ───────────────────────
        try:
            mem_registry._ensure_user_memory_initialized()
            guarded_paths = [get_user_memory_path()]
        except Exception:
            guarded_paths = []

        mw_registry = get_middleware_registry()
        middleware.extend(mw_registry.build_middlewares(
            [
                "plan_lock",
                "a2ui_buffer",
                ("memory_guard", {"guarded_paths": guarded_paths}),
            ],
            config=self._config,
            model=self.get_model(),
            backend=self.make_backend(),
        ))

        # ── 4. Safety middleware (env-var overridable limits) ──────────────────
        wf_limit = int(os.getenv("K8S_WRITE_FILE_RUN_LIMIT", "20"))
        middleware.append(
            ToolCallLimitMiddleware(tool_name="write_file", run_limit=wf_limit, exit_behavior="end")
        )

        gt_limit = int(os.getenv("K8S_GLOBAL_TOOL_RUN_LIMIT", "60"))
        middleware.append(
            ToolCallLimitMiddleware(run_limit=gt_limit, exit_behavior="end")
        )

        mc_limit = int(os.getenv("K8S_MODEL_CALL_LIMIT", "40"))
        middleware.append(
            ModelCallLimitMiddleware(run_limit=mc_limit, exit_behavior="end")
        )

        logger.info(
            "Middleware stack assembled",
            extra={
                "total": len(middleware),
                "types": [type(m).__name__ for m in middleware],
            },
        )
        return middleware

    # ── Abstract implementations — build_agent & seed_files ──────────────

    async def build_agent(self) -> Any:  # CompiledStateGraph
        """
        Assemble all components into a ``create_deep_agent()`` call.

        dcode-aligned lifecycle:
        1. Start ``MCPSessionManager`` → resolve all domain-bound MCP tools
        2. Discover subagents from filesystem via ``SubagentRegistry``
        3. Build dynamic subagent specs with pre-resolved tools
        4. Wire model, prompt, tools, subagents, backend, store, middleware

        MCP graceful degradation: if any server fails, the agent still
        starts with whatever tools are available.  The error is logged
        and can be surfaced to the user via ``mcp_session_manager.error_summary``.
        """
        if getattr(self, "_agent", None):
            return self._agent

        logger.info("Building Helm Operator deep agent graph (dcode-aligned)")

        # Discover required MCP servers dynamically from sub-agents
        agents_dir = get_domain_agents_dir(self.domain_name)
        subagent_metas = list_subagents(agents_dirs=[agents_dir])
        required_servers = set()
        for meta in subagent_metas:
            meta_mcp_servers = [m.server_name for m in meta.config.tools.mcp]
            if meta_mcp_servers:
                required_servers.update(meta_mcp_servers)

        # Merge with coordinator filter if specified
        if self._mcp_server_filter is not None:
            mcp_servers = list(set(self._mcp_server_filter) & required_servers)
        else:
            mcp_servers = list(required_servers)

        logger.info(f"Discovered required MCP servers from sub-agent configs: {mcp_servers}")

        # ── 1. Start MCP sessions ────────────────────────────────────────
        self._mcp_session_manager = MCPSessionManager(
            self._config,
            domain=None,
            server_filter=mcp_servers,
        )
        await self._mcp_session_manager.__aenter__()

        mcp_tools, server_results = await self._mcp_session_manager.resolve_tools()

        # Log any MCP connection failures (graceful degradation)
        error_summary = self._mcp_session_manager.error_summary
        if error_summary:
            logger.warning(f"MCP session warnings:\n{error_summary}")

        # ── 2. Build components ──────────────────────────────────────────
        self._store = self.build_store()
        checkpointer = self.build_checkpointer()
        subagents = await self.get_subagent_specs()
        tools = await self.get_tools()
        middleware = self._build_middleware()

        # ── 3. Assemble deep agent ───────────────────────────────────────
        self._agent = create_deep_agent(
            model=self.get_model(),
            name=self.name,
            system_prompt=self.system_prompt,
            tools=tools,
            subagents=subagents,
            backend=self.make_backend(),
            store=self._store,
            checkpointer=checkpointer,
            interrupt_on=self.get_interrupt_config(),
            context_schema=self.context_schema,
            state_schema=HelmOperatorState,
            middleware=middleware,
        )

        logger.info(
            "Helm Operator deep agent built successfully",
            extra={
                "subagent_count": len(subagents),
                "mcp_tool_count": len(mcp_tools),
                "mcp_servers_ok": sum(1 for r in server_results if r.status == "ok"),
                "mcp_servers_err": sum(1 for r in server_results if r.status == "error"),
            },
        )
        return self._agent

    async def cleanup(self) -> None:
        """Clean up MCP sessions when the coordinator is shut down."""
        if self._mcp_session_manager:
            await self._mcp_session_manager.cleanup()
            self._mcp_session_manager = None
            logger.info("MCP session manager cleaned up")

    def seed_files(
        self,
        skills_dir: Optional[Any] = None,
        memory_dir: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Load selectively filtered skill and memory files.
        """
        return K8sBackendMixin.seed_files(
            skill_paths=self.get_skill_paths(),
            memory_paths=self.get_memory_paths(),
        )

    # ── Supervisor-level state transforms ────────────────────────────────
    #
    # These transforms bridge SupervisorState ↔ deep agent invocation state.
    # They are called by the `transfer_to_helm_operator` @tool in the supervisor
    # when delegating work to the HelmOperatorCoordinator deep agent.

    def input_transform(self, send_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Returns:
            Deep agent graph input: ``{messages: [...], files: {...}}``

        Reference: TFCoordinator.input_transform
        """
        user_query = send_payload.get("user_query", "")
        messages: List[BaseMessage] = []
        
        domain_summaries = send_payload.get("domain_summaries")
        cross_domain = send_payload.get("cross_domain_context")
        
        context_parts = []
        if domain_summaries:
            summary_lines = []
            for s in domain_summaries:
                if isinstance(s, dict):
                    domain = s.get("domain", "unknown")
                    outcome = s.get("outcome", "completed")
                    detail = s.get("detail", "")
                    summary_lines.append(f"- {domain}: {outcome} — {detail}")
            if summary_lines:
                context_parts.append("Recent tasks completed by other domains:\n" + "\n".join(summary_lines))
                
        if cross_domain and isinstance(cross_domain, dict):
            context_parts.append(f"Deferred task context: {cross_domain}")
            
        if context_parts:
            messages.append(SystemMessage(content="Cross-Domain Context:\n\n" + "\n\n".join(context_parts)))
            
        if user_query:
            messages.append(HumanMessage(content=user_query))
            
        files = self.seed_files()

        # ── OpenWiki No-Op Skip Hashing ─────────────────────────────────────
        workspace_dir = os.getenv("HELM_WORKSPACE", "./workspace/helm-charts")
        last_exec_status = ""
        current_hash = ""
        
        # Only evaluate No-Op Skip for chart creation/update tasks
        is_generation_task = any(kw in user_query.lower() for kw in ["generate", "scaffold", "create", "update", "patch"])
        
        if is_generation_task:
            current_hash = compute_workspace_hash(workspace_dir)
            metadata_file = Path(workspace_dir) / ".last-update.json"
            
            if metadata_file.exists() and current_hash:
                try:
                    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                    saved_hash = metadata.get("hash", "")
                    if saved_hash == current_hash and is_git_clean(workspace_dir):
                        last_exec_status = "skipped"
                        logger.info(
                            "OpenWiki No-Op Skip: workspace and Git HEAD are unmodified",
                            extra={"hash": current_hash}
                        )
                except Exception as e:
                    logger.warning(f"Failed to read .last-update.json: {e}")

        transformed: Dict[str, Any] = {
            "messages": messages,
            "directory_hash": current_hash,
            "last_execution_status": last_exec_status,
        }

        # Only include files if there are any to seed
        if files:
            transformed["files"] = files

        logger.info(
            "input_transform: SupervisorState → deep agent input",
            extra={
                "message_count": len(messages),
                "file_count": len(files),
                "last_execution_status": last_exec_status,
            },
        )

        return transformed

    def build_context(
        self,
        supervisor_state: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Build the ``K8sOperatorContext`` dict for ``config["context"]``.

        Three-way state bridge between the supervisor and the deep agent:

        1. ``input_transform``  → deep agent **graph state** (messages, files)
        2. ``build_context``    → deep agent **runtime config** (K8sOperatorContext)
        3. ``output_transform`` → supervisor **graph state**

        Reference: TFCoordinator.build_context
        """
        state = supervisor_state or {}

        # ── 1. Central configuration base ────────────────────────────────
        ctx: Dict[str, Any] = {
            # GitHub
            "github_repo":          self.config.GITHUB_REPO or "",
            "github_branch":        self.config.GITHUB_BRANCH or "main",
            # Workspace
            "workspace_dir":        self.config.HELM_WORKSPACE or "./workspace/helm-charts",
            # Organization
            "org_name":             self.config.ORG_NAME or "default_org",
            "environment":          self.config.ENVIRONMENT or "development",
            # Cluster coordinates
            "cluster_context":      self.config.K8S_CONTEXT or "",
            "kubeconfig_path":      self.config.KUBECONFIG or "",
            "default_namespace":    self.config.K8S_DEFAULT_NAMESPACE or "default",
        }

        # ── 2. Supervisor runtime state ───────────────────────────────────
        if state.get("session_id"):
            ctx["session_id"] = state["session_id"]
        if state.get("task_id"):
            ctx["task_id"] = state["task_id"]

        # ── 3. Caller-injected context (highest priority) ─────────────────
        caller_ctx: Dict[str, Any] = state.get("context") or {}
        if isinstance(caller_ctx, dict):
            ctx.update({k: v for k, v in caller_ctx.items() if v is not None and v != ""})

        # Strip empty-string optional fields
        for key in ("github_repo", "org_name", "cluster_context", "kubeconfig_path"):
            if ctx.get(key) == "":
                ctx.pop(key, None)

        # ── Cross-domain context ──────────────────────────────────────
        # If the supervisor routed here after another coordinator deferred
        # with "outside my scope", inject the structured prior context so
        # the Helm agent can use it instead of asking the user.
        cross_domain = state.get("cross_domain_context")
        if isinstance(cross_domain, dict) and cross_domain:
            ctx["cross_domain_context"] = cross_domain

        # Propagate accumulated domain summaries for the blackboard pattern
        domain_summaries = state.get("domain_summaries")
        if isinstance(domain_summaries, list) and domain_summaries:
            ctx["domain_summaries"] = domain_summaries

        # ── MCP status (surface connection errors to the model) ───────
        if self._mcp_session_manager and self._mcp_session_manager.error_summary:
            ctx["mcp_status"] = self._mcp_session_manager.error_summary

        logger.info(
            "build_context: K8sOperatorContext assembled",
            extra={
                "fields": sorted(ctx.keys()),
                "session_id": ctx.get("session_id"),
                "task_id": ctx.get("task_id"),
            },
        )

        return ctx

    def output_transform(
        self,
        agent_state: Dict[str, Any],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Transform deep agent final state → supervisor-mergeable payload.

        Args:
            agent_state: The dict returned by ``deep_agent.ainvoke()``.

        Reference: TFCoordinator.output_transform
        """

        # Handle both dict and Pydantic model
        state: Dict[str, Any] = agent_state
        if not isinstance(agent_state, dict) and hasattr(agent_state, "model_dump"):
            state = agent_state.model_dump()

        # ── Sync virtual /workspace/ files to real disk ───────────────────
        # Only sync when there are actual /workspace/ files (e.g. from chart
        # generation).  Rollback/list/status operations only have /skills/ and
        # /memories/ in state — syncing those would produce a spurious WARNING.
        files: Dict[str, Any] = state.get("files", {})
        workspace_files = {k: v for k, v in files.items() if k.startswith("/workspace/")}
        synced: Dict[str, Any] = {}
        if workspace_files:
            try:
                synced = sync_workspace_to_disk(files)
                # Compute new directory hash and write to .last-update.json
                workspace_dir = os.getenv("HELM_WORKSPACE", "./workspace/helm-charts")
                new_hash = compute_workspace_hash(workspace_dir)
                if new_hash:
                    metadata_file = Path(workspace_dir) / ".last-update.json"
                    metadata_file.parent.mkdir(parents=True, exist_ok=True)
                    metadata_file.write_text(json.dumps({"hash": new_hash}), encoding="utf-8")
                    logger.info("OpenWiki Snapshot: Updated .last-update.json with new hash", extra={"hash": new_hash})
            except Exception as e:
                logger.error(
                    "output_transform: failed to sync workspace files or update hash",
                    extra={"error": str(e)},
                )

        # Extract the user-facing AI response from the deep agent's messages.
        # In the sub-agent architecture, the messages array contains both
        # sub-agent ToolMessages (raw execution output) and the coordinator's
        # own AI messages (formatted user-facing response).
        # _extract_final_ai_text walks backwards to find only the coordinator's
        # last AI response, skipping internal sub-agent results.
        messages = state.get("messages", [])
        final_message = self._extract_final_ai_text(messages)

        # Build supervisor-compatible update dict (CoordinatorResult contract)
        output: Dict[str, Any] = {
            "summary_text": final_message or "Helm operator completed.",
            "status": "completed",
            "ui_payload": {
                "type": "helm_operation_result",
                "content": final_message or "Helm operator completed.",
            },
            "artifacts": {
                "messages": messages,
                "files": files,
                "collected_inputs": state.get("collected_inputs", {}),
                "workflow_state": state.get("workflow_state", {}),
                "pending_interrupt": state.get("pending_interrupt"),
                "synced_paths": {k: str(v) for k, v in synced.items()},
                "structured_response": state.get("structured_response"),
            },
            "domain_summary": extract_domain_summary(
                domain="helm",
                final_message=final_message,
            ),
        }
        
        logger.info(
            "output_transform: deep agent → supervisor state",
            extra={
                "final_message_preview": (final_message or "")[:200],
                "file_count": len(files),
                "synced_count": len(synced),
            },
        )

        return output


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_helm_coordinator(
    config: Optional["Config"] = None,
    mcp_server_filter: Optional[List[str]] = None,
) -> HelmOperatorCoordinator:
    """
    Create a HelmOperatorCoordinator instance.

    Usage::

        from k8s_autopilot.core.agents.helm_operator.coordinator import create_helm_coordinator
        coordinator = create_helm_coordinator(config)
    """
    return HelmOperatorCoordinator(config=config, mcp_server_filter=mcp_server_filter)
