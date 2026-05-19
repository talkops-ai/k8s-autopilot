"""
Helm Operator Deep Agent Coordinator.

Production-grade implementation of the deep agent pattern for Helm chart
generation and updates. Wires backends, MCP tools, and subagents via the
``BaseDeepAgent`` abstract class.

Workflows:
    **New Chart**:  helm-planner → helm-skill-builder → helm-generator → chart-validator → HITL → github-agent
    **Update Chart**: (future) update-planner → helm-updater → chart-validator → HITL → github-agent

Architecture:
    - Single ``create_deep_agent()`` with chart-generation sub-agents registered
    - Planner subgraph mounted as ``CompiledSubAgent`` via ``RunnableLambda`` wrapper
    - MCP tools loaded JIT (lazy) per sub-agent execution
    - ``CompositeBackend`` with route-based storage (memories → StoreBackend, skills → StateBackend)
    - Middleware safety nets (tool/model call limits)

Reference: aws-orchestrator-agent tf_operator/tf_cordinator.py
Docs: https://docs.langchain.com/oss/python/deepagents/customization
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, cast

from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import RunnableConfig
from langchain.tools import tool, ToolRuntime
from langgraph.store.memory import InMemoryStore
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import ToolMessage
from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data
from k8s_autopilot.core.agents.types import BaseDeepAgent
from k8s_autopilot.core.state.helm_planner_state import (
    HelmPlannerState,
    HelmPlannerWorkflowState,
)
from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.utils.user_input_tool import (
    create_user_input_tool,
    create_chat_continue_tool,
)
from k8s_autopilot.utils.operations_context import create_log_operation_tool
from k8s_autopilot.core.state.helm_operator_state import HelmOperatorContext
from k8s_autopilot.core.agents.helm_operator.subagents import get_helm_subagent_specs
from k8s_autopilot.core.agents.helm_operator.middleware import build_k8s_middleware
from k8s_autopilot.utils.memory import (
    K8sBackendMixin,
    get_project_root,
    sync_workspace_to_disk,
)
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.utils.domain_summary import extract_domain_summary

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("HelmOperatorCoordinator")


# ---------------------------------------------------------------------------
# Coordinator System Prompt
# ---------------------------------------------------------------------------

HELM_COORDINATOR_PROMPT = """\
\
You are the Helm Chart Generator coordinator.
You orchestrate creating and updating Helm charts via specialized sub-agents.

## Sub-Agent Skills
Each sub-agent has its own specialized skills loaded automatically:
- helm-generator: generates Helm chart files using skill-defined patterns, values, and templates
- helm-validator: runs helm lint / helm template in sandbox
- github-agent: GitHub commit workflow via MCP tools
- helm-skill-builder: creates new skill directories when no skill exists for an app type
- helm-updater: fetches and patches existing charts
- helm-planner: orchestrates requirements analysis → architecture planning
- helm-operation: performs live Helm operations (install, upgrade, rollback) on clusters

You do NOT need to tell sub-agents to read skill files — they load automatically.

## Tool: request_user_input
You have a generic HITL tool to pause and ask the user anything.
Call it whenever you need human input.  Arguments:
  - question (str, required): the question to ask
  - title (str): card header
  - context (str): extra context shown below the question
  - options (list of dicts): buttons for the user, each with:
      {"key": "...", "label": "...", "primary": true/false}
  - input_fields (list of dicts): text fields to collect data, each with:
      {"key": "...", "label": "...", "default": "...", "required": true/false}

The user's response comes back as a ToolMessage.  Read it and act accordingly.

## HITL Policy
At session start, read /memory/helm-operator/hitl-policies.md for the complete HITL policy.
That file defines WHEN you MUST call request_user_input (mandatory gates) and
WHEN you MAY call it (optional gates, at your discretion).
The workflow steps below show HOW to call it (which options and fields to use).

## CRITICAL: Query Classification
Before doing anything, classify the user request:

**CONVERSATIONAL / END-OF-WORKFLOW** (e.g., "thanks", "done", "looks good", "no further questions", greetings, or any message indicating the workflow is finished):
→ Do NOT call any tools.
→ Just reply directly with a polite conversational message. This signals to the supervisor that your workflow is complete.

**DIFFERENT DOMAIN / OUT-OF-SCOPE TASKS** (e.g., requests for ArgoCD passwords, Traefik routes, raw K8s pods, scaling, or any non-Helm tasks):
→ Do NOT call any tools or delegate to any sub-agent.
→ Immediately return the following string verbatim (fill in the brackets):
"This is outside my scope. Please use the appropriate operator.
User Request: [The user's specific request or goal]
Context: [Briefly summarize what you previously did if relevant]"

## Workflow — New Chart
1. Check if skills exist for the requested application type:
   - Use read_file to check /skills/helm-operator/{app}-chart-generator/SKILL.md
   - If it exists: SKIP helm-planner and helm-skill-builder entirely. Go directly to step 3.
   - If it does NOT exist:
     task(helm-planner): "Plan Helm chart for: {request}" 
2. CHECK the planner's output message:
   - IF the planner output contains "Skills written for" → SKIP helm-skill-builder entirely.
   - IF the planner output does NOT mention skills written → task(helm-skill-builder): "Build skill files for: {request}"
3. task(helm-generator): "Generate Helm chart for {app}."
4. Call sync_workspace tool to materialise virtual files to disk before validation
5. task(helm-validator): "Validate chart at {chart-name}"
6. If INVALID: task(helm-generator): "Fix these errors: {errors}" → call sync_workspace again → repeat step 5
7. **[Commit Gate]** — (Mandatory). Call `request_user_input` using exactly the schema defined in **AGENTS.md §1 Commit Gate**.
8. **Handle the response:**
   - If user chose "push_to_github" and provided repository + branch:
     → task(github-agent): "Commit {app} chart to {repository} branch {branch}"
   - If user chose "keep_local" or provided no repository:
     → Report local file paths to user. Do NOT call github-agent.
9. **[Next Steps Gate]** — (Mandatory). Call `request_user_input` using exactly the schema defined in **AGENTS.md §2 Next Steps Gate**.

## Workflow — Update Chart
1. task(helm-planner): "Analyse {chart_path} on {repo}: {what to change}"
2. task(helm-updater): "Fetch and update {chart_path} on {repo}: {what to change}"
3. task(helm-validator): "Validate chart {chart_name}"
4. If INVALID: task(helm-updater): "Fix: {errors}" → repeat step 3
5. **[Commit Gate]** — (Mandatory). Call `request_user_input` using exactly the schema defined in **AGENTS.md §1 Commit Gate**.
6. **[Next Steps Gate]** — (Mandatory). Call `request_user_input` using exactly the schema defined in **AGENTS.md §2 Next Steps Gate**.

## Workflow — Helm Operation (Query or State-Modifying)
1. task(helm-operation): "{user request}"
   - The sub-agent determines whether this is a read-only query or a state-modifying operation.
   - For queries: it calls the tool and returns results directly.
   - For mutations: it follows its internal phased workflow (discovery → planning → execution → verification).
   - **CRITICAL for FOLLOW-UP operations** (upgrade, modify values, rollback):
     You MUST include ALL of the following in the task description:
     - The exact chart source (e.g., "oci://registry/chart" or "bitnami/nginx")
     - Release name and namespace
     - Previous values that were set
     - Example: task(helm-operation): "Upgrade release 'web-release' in namespace 'opstree'
       (chart source: oci://ghcr.io/stakater/charts/web, version: 0.1.0,
       current values: replicaCount=2, service.type=LoadBalancer).
       Change: set replicaCount=1. Use --reuse-values."
     - If you do not remember these details, read_file /memory/helm-operator/operations-log.md
2. **Present the result to the user:** Do NOT blindly dump raw console output or unformatted JSON blocks to the user. Instead, synthesize the sub-agent's return data into a beautifully structured, human-readable Markdown summary. Use Markdown tables for lists, bold text for key-values, and clean bullet points for notes—presenting the information exactly like a high-quality AI assistant.
3. **Log the operation**: For ALL state-modifying operations (install, upgrade, rollback, uninstall),
   call `log_helm_operation` with action, release_name, namespace, chart_source, values, version.
   This is MANDATORY — it preserves context for follow-up operations after conversation summarization.
4. **[Next Steps Gate]** — (Mandatory for operations). You MUST pass this beautifully formatted summary as the `message` argument into the `request_chat_continue` tool. Include a short follow-up like "What would you like to do next?" at the end of the text. Do NOT rely on normal chat output, as only the tool's message is displayed to the user during operations. Do NOT call this tool for conversational closures (e.g., "thanks", "I am good here", or when the user indicates they are finished).

## CRITICAL: Step Budget
You have a limited number of steps (~150 total). Be efficient:
- The typical flow is: helm-planner → helm-generator → helm-validator → request_user_input (3 sub-agent calls + 1–2 tool calls).
- If helm-planner writes skills, helm-skill-builder is NEVER needed — still 3 sub-agent calls.
- NEVER call more than 5 sub-agents for a single chart request (including github-agent if approved).
- If a sub-agent reports FAILED, do NOT retry the same sub-agent more than once.

## Memory Rules
- AGENTS.md is auto-loaded — do NOT read_file it (it is already in your prompt).
- For destructive operations: read_file /memory/helm-operator/hitl-policies.md for edge-case rules.
- After successful chart generation or update: update /memory/helm-operator/chart-index.md
  with the chart name, version, files generated, and timestamp.
- The operations journal at /memory/helm-operator/operations-log.md is auto-injected
  into your context before each model call. Use it for follow-up operations.

## Workspace Sync
Generated chart files are written to a virtual filesystem under /workspace/.
The coordinator automatically syncs them to the real disk for validation.
Do NOT ask helm-generator to re-write files just because helm-validator says
"directory not found" — the sync happens automatically.

## Rules — Never Violate
- NEVER write chart files yourself — always delegate to helm-generator or helm-updater
- NEVER run helm commands yourself — always delegate to helm-validator or helm-operation
- NEVER interact with GitHub yourself — always delegate to github-agent
- NEVER commit to GitHub without the user providing repository and branch
- ALWAYS follow the HITL policies in /memory/helm-operator/hitl-policies.md
- ALWAYS use the call patterns in /memory/helm-operator/AGENTS.md for request_user_input
- ALWAYS call log_helm_operation after state-modifying helm operations
- ALWAYS include full context (chart source, values, release) when delegating follow-up helm operations
- The DEFAULT outcome for the commit gate is always LOCAL — do NOT assume GitHub push
"""


# ---------------------------------------------------------------------------
# HelmOperatorCoordinator — the deep agent
# ---------------------------------------------------------------------------

class HelmOperatorCoordinator(BaseDeepAgent):
    """
    Helm Operator Deep Agent Coordinator.

    Production implementation of the deep agent pattern that:
    - Inherits lifecycle from ``BaseDeepAgent``
    - Uses ``K8sOperatorBackendMixin`` for Helm-specific backend routing
    - Connects to GitHub MCP server for file operations
    - Manages sub-agents (dict specs + CompiledSubAgent) for the chart pipeline
    - Supports HITL approval gates before GitHub commits
    - Implements ``input_transform`` / ``output_transform`` for subgraph state bridging

    Reference: aws-orchestrator-agent TFCoordinator
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        *,
        mcp_server_filter: Optional[List[str]] = None,
    ) -> None:
        super().__init__(config=config)
        self._mcp_server_filter = mcp_server_filter       

        logger.info("HelmOperatorCoordinator initialized")

    # ── Abstract implementations — Properties ────────────────────────────

    @property
    def name(self) -> str:
        return "helm-operator-coordinator"

    @property
    def system_prompt(self) -> str:
        return HELM_COORDINATOR_PROMPT

    @property
    def context_schema(self) -> type:
        return HelmOperatorContext

    # ── Abstract implementations — Model ─────────────────────────────────

    def get_model(self) -> Any:
        """Return an initialized deep-agent tier LLM model."""
        return create_model(self._config.get_llm_deepagent_config())

    def _get_validator_model(self) -> Any:
        """Return an initialized standard-tier LLM for the chart-validator."""
        return create_model(self._config.get_llm_config())

    # ── Abstract implementations — Sub-agents ────────────────────────────

    async def get_subagent_specs(self) -> List[Any]:
        """
        Build sub-agent specs.

        Returns a mixed list of:
        - Dict specs for simple sub-agents (helm-generator, chart-validator, etc.)
        - ``CompiledSubAgent`` wrappers for GitHub MCP-dependent agents (JIT nodes)
        - ``CompiledSubAgent`` for the planner supervisor (compiled LangGraph subgraph)

        **State bridging for HelmPlannerSupervisorAgent:**

        The deep agent framework invokes ``CompiledSubAgent.runnable.invoke(state)``
        where ``state = {parent_state_minus_excluded, messages: [HumanMessage(task_desc)]}``.
        Since ``HelmPlannerState`` has a different schema (``user_query``, ``workflow_state``,
        ``active_agent``, etc.), we wrap the compiled planner graph in a
        ``RunnableLambda`` that follows the official LangGraph
        "call a subgraph inside a node" pattern:

            1. ``planner.input_transform(state)`` → bridges deep-agent state → HelmPlannerState
            2. ``planner_graph.invoke(transformed)`` → runs the 2-phase pipeline
            3. ``planner.output_transform(result)`` → bridges HelmPlannerState → deep-agent state

        Reference: TFCoordinator.get_subagent_specs()
        """
        from deepagents.middleware.subagents import CompiledSubAgent

        from k8s_autopilot.core.agents.helm_operator.helm_planner import (
            HelmPlannerSupervisorAgent,
        )

        # Get basic and JIT-compiled subagents from the spec definitions
        specs: List[Any] = get_helm_subagent_specs(
            coordinator_model=self.get_model(),
            validator_model=self._get_validator_model(),
        )

        # Build the planner subgraph
        planner = HelmPlannerSupervisorAgent(config=self._config)
        planner_graph = planner.build_graph()

        # ── RunnableLambda wrapper (official LangGraph pattern) ───────────
        #
        # When parent and subgraph have different state schemas, the official
        # docs recommend wrapping the subgraph invocation in a node function
        # that explicitly transforms state in both directions.
        #
        # Here, HelmPlannerSupervisorAgent already has input_transform/output_transform
        # that handle the schema bridging.

        async def _planner_wrapper(
            state: Dict[str, Any],
            config: Optional[RunnableConfig] = None,
        ) -> Dict[str, Any]:
            """
            Bridge deep-agent state → HelmPlannerState → deep-agent state.

            Data flow:
                supervisor.build_context(runtime_state)
                  → config["context"]  (K8sOperatorContext)
                    → _planner_wrapper enriches `state` from context
                      → planner.input_transform(enriched_state)
                        → planner_graph.invoke(HelmPlannerState)
                          → planner.output_transform(result)
                            → deep-agent state update
            """
            # ── Extract session context injected by supervisor ─────────────
            coordinator_ctx: Dict[str, Any] = {}
            if config and hasattr(config, "get"):
                configurable = config.get("configurable") or {}
                ctx_raw = configurable.get("context") or config.get("context") or {}
                coordinator_ctx = ctx_raw if isinstance(ctx_raw, dict) else {}

            # ── Enrich state with context values ──────────────────────────
            enriched_state: Dict[str, Any] = {
                **state,
                "session_id": (
                    state.get("session_id")
                    or coordinator_ctx.get("session_id")
                ),
                "task_id": (
                    state.get("task_id")
                    or coordinator_ctx.get("task_id")
                ),
                "user_query": (
                    state.get("user_query")
                    or coordinator_ctx.get("user_query")
                    # last-resort: pull from the first human message content
                    or next(
                        (
                            getattr(m, "content", None)
                            or (m.get("content") if isinstance(m, dict) else None)
                            for m in reversed(state.get("messages") or [])
                            if (
                                getattr(m, "type", None) == "human"
                                or (isinstance(m, dict) and m.get("role") == "user")
                            )
                        ),
                        None,
                    )
                ),
            }

            logger.info(
                "_planner_wrapper: enriched state from HelmOperatorContext",
                extra={
                    "session_id": enriched_state.get("session_id"),
                    "task_id": enriched_state.get("task_id"),
                    "has_user_query": bool(enriched_state.get("user_query")),
                    "message_count": len(enriched_state.get("messages") or []),
                },
            )

            # 1. Enriched deep-agent state → HelmPlannerState input
            subgraph_input = planner.input_transform(enriched_state)

            # 2. Invoke the compiled planner subgraph
            subgraph_output = await planner_graph.ainvoke(subgraph_input, config=config)

            # 3. HelmPlannerState → CompiledSubAgent return value
            parent_files: Dict[str, Any] = enriched_state.get("files") or {}
            return planner.output_transform(subgraph_output, parent_files=parent_files)

        # Register as CompiledSubAgent with RunnableLambda as the runnable.
        planner_compiled = CompiledSubAgent(
            name="helm-planner",
            description=(
                "Orchestrates Helm chart planning through a 2-phase pipeline: "
                "requirements analysis → architecture planning. "
                "Use this when a NEW chart generation request arrives to produce a "
                "comprehensive plan before the helm-skill-builder and helm-generator run."
            ),
            runnable=RunnableLambda(_planner_wrapper),
        )
        specs.append(planner_compiled)

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
        
        # Build the chat continue tool for data presentation
        chat_continue = create_chat_continue_tool()

        # Build the operations journal tool for context persistence
        log_operation = create_log_operation_tool()
        
        return [sync_workspace, user_input, chat_continue, log_operation]

    def get_skill_paths(self) -> List[str]:
        return ["/skills/helm-operator"]

    def get_memory_paths(self) -> List[str]:
        return [
            "/memories/helm-operator/AGENTS.md",
            "/memories/helm-operator/hitl-policies.md",
        ]

    def get_interrupt_config(self) -> Dict[str, Any]:
        """HITL gates: require approval before destructive file operations."""
        return {
            "delete_module": {
                "allowed_decisions": cast(list, ["approve", "edit", "reject"]),
            },
        }

    # ── Abstract implementations — Backend & Storage ─────────────────────

    def make_backend(self, runtime: Any) -> Any:
        """Use Helm-specific backend with LocalShellBackend for CLI."""
        return K8sBackendMixin.make_backend(runtime)

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
            
        # Pre-seed operations-log if not populated to prevent "File not found" read errors
        if store.get(namespace, "helm-operator/operations-log.md") is None:
            empty_log = "# Helm Operations Journal\n\nAuto-generated log of operations performed in this session. Used by the coordinator to maintain context across conversation turns and after summarization.\n"
            store.put(namespace, "helm-operator/operations-log.md", dict(create_file_data(empty_log)))

        return store

    def build_checkpointer(self) -> Any:
        """Return None to inherit the parent supervisor's checkpointer natively."""
        return None

    # ── Abstract implementations — build_agent & seed_files ──────────────

    async def build_agent(self) -> Any:  # CompiledStateGraph
        """
        Assemble all components into a ``create_deep_agent()`` call.

        Wires model, prompt, tools, subagents (dict + CompiledSubAgent),
        skills, memory, backend, store, checkpointer, HITL config,
        and context schema into a compiled LangGraph.

        Reference: TFCoordinator.build_agent
        """
        if getattr(self, "_agent", None):
            return self._agent

        logger.info("Building Helm Operator deep agent graph")

        self._store = self.build_store()
        checkpointer = self.build_checkpointer()
        tools = await self.get_tools()
        subagents = await self.get_subagent_specs()
        middleware = build_k8s_middleware(config=self._config)

        self._agent = create_deep_agent(
            model=self.get_model(),
            name=self.name,
            system_prompt=self.system_prompt,
            tools=tools,
            subagents=subagents,
            skills=self.get_skill_paths(),
            memory=self.get_memory_paths(),
            backend=self.make_backend,
            store=self._store,
            checkpointer=checkpointer,
            interrupt_on=self.get_interrupt_config(),
            context_schema=self.context_schema,
            middleware=middleware,
        )
        return self._agent

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
        Transform supervisor ``@tool`` payload → deep agent graph input (state).

        Extracts ``messages`` and seeds the virtual filesystem so the deep agent
        starts with skills and memory already loaded.

        Args:
            send_payload: Dict from ``dict(runtime.state)`` in the tool wrapper,
                          with ``messages`` replaced by ``[HumanMessage(task_description)]``.

        Returns:
            Deep agent graph input: ``{messages: [...], files: {...}}``

        Reference: TFCoordinator.input_transform
        """
        messages = send_payload.get("messages", [])
        files = self.seed_files()

        transformed: Dict[str, Any] = {
            "messages": messages,
        }

        # Only include files if there are any to seed
        if files:
            transformed["files"] = files

        logger.info(
            "input_transform: SupervisorState → deep agent input",
            extra={
                "message_count": len(messages),
                "file_count": len(files),
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

        # ── 1. Env-var base ───────────────────────────────────────────────
        ctx: Dict[str, Any] = {
            # GitHub
            "github_repo":          os.getenv("GITHUB_REPO", ""),
            "github_branch":        os.getenv("GITHUB_BRANCH", "main"),
            # Workspace
            "workspace_dir":        os.getenv("HELM_WORKSPACE", "./workspace/helm-charts"),
            # Organization
            "org_name":             os.getenv("ORG_NAME", "default_org"),
            "environment":          os.getenv("ENVIRONMENT", "development"),
            # Cluster coordinates
            "cluster_context":      os.getenv("K8S_CONTEXT", ""),
            "kubeconfig_path":      os.getenv("KUBECONFIG", ""),
            "default_namespace":    os.getenv("K8S_DEFAULT_NAMESPACE", "default"),
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
        files: Dict[str, Any] = state.get("files", {})
        synced: Dict[str, Any] = {}
        if files:
            try:
                synced = sync_workspace_to_disk(files)
            except Exception as e:
                logger.error(
                    "output_transform: failed to sync workspace files to disk",
                    extra={"error": str(e)},
                )

        # Extract the final message from the deep agent's conversation
        final_message: Optional[str] = None
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            final_message = getattr(last_msg, "content", None) or (
                last_msg.get("content") if isinstance(last_msg, dict) else None
            )

        # Build supervisor-compatible update dict
        output: Dict[str, Any] = {
            "final_message": final_message or "Helm operator completed.",
            "status": "completed",
            "helm_operator_output": {
                "messages": messages,
                "files": files,
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
