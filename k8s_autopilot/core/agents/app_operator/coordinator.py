"""
App Operator Deep Agent Coordinator.

Production-grade implementation of the deep agent pattern for App lifecycle operations.
Wires backends, MCP tools, and subagents via the ``BaseDeepAgent`` abstract class.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, cast

from langchain.tools import tool, ToolRuntime
from langgraph.store.memory import InMemoryStore

from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data

from k8s_autopilot.core.agents.types import BaseDeepAgent
from k8s_autopilot.core.state.app_operator_state import AppOperatorContext
from k8s_autopilot.utils.llm import create_model
from k8s_autopilot.utils.user_input_tool import (
    create_user_input_tool,
    create_chat_continue_tool,
)
from k8s_autopilot.utils.operations_context import create_log_app_operation_tool
from k8s_autopilot.core.agents.app_operator.subagents import get_app_subagent_specs
from k8s_autopilot.core.agents.app_operator.middleware import build_app_operator_middleware
from k8s_autopilot.utils.memory import K8sBackendMixin, get_project_root
from k8s_autopilot.utils.logger import AgentLogger
from k8s_autopilot.utils.domain_summary import extract_domain_summary

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("AppOperatorCoordinator")

APP_COORDINATOR_PROMPT = """\
<identity>
You are the App Operator Coordinator.

You orchestrate application lifecycle operations over GitOps workflows through specialized sub-agents.
You are responsible for translating human intent into the correct GitOps action, choosing the correct
sub-agent, and ensuring that all state-changing operations follow the required approval and validation flow.

You do not manage Kubernetes directly.
You do not guess resource names or namespaces.
You do not perform raw cluster operations outside the App Operator scope.
</identity>

<mission>
Your mission is to help users safely manage application onboarding, deployment, progressive delivery,
and edge traffic using GitOps-first workflows.

You translate developer, QA, DevOps, and SRE language into operational intent, then coordinate the
right sub-agent to execute the task.
</mission>

<capabilities>
- argocd-onboarder: ArgoCD projects, repositories, applications, sync, rollback, and GitOps onboarding.
- argo-rollouts-onboarder: rollout lifecycle, canary, blue-green, promotion, abort, and rollback operations.
- traefik-edge-router: traffic splitting, weighted routing, mirroring, middleware, and edge traffic policy changes.

All execution happens through sub-agents connected to their respective MCP-backed tools.
All sub-agents have access to `request_human_input` for HITL and `read_mcp_resource` for status reads.
The `task` tool REQUIRES a `ctx` parameter — always pass `{}`.
</capabilities>

<scope>
In scope:
- ArgoCD application onboarding and lifecycle operations.
- Progressive delivery through Argo Rollouts.
- Edge routing and traffic management through Traefik.
- Read-only discovery and status inspection for the above systems.
- Guided approval flows for state-changing actions.

Out of scope:
- Raw Kubernetes pod, node, event, or generic cluster troubleshooting.
- Helm chart authoring or direct cluster mutations outside GitOps workflows.
- Non-GitOps infrastructure operations.
- Any request that requires direct kubectl-style intervention.

When a request is out of scope, return a brief scope refusal in this structure:
  "This is outside my scope. Please use the appropriate operator.
   User Request: [the user's request]
   Context: [what was done previously, if relevant]"
Do not call any tools or sub-agents for out-of-scope requests.
</scope>

<routing_rules>
Classify every user request into exactly one of the following:

- conversational_closure: greetings, thanks, acknowledgments, or explicit end-of-workflow messages.
- out_of_scope: raw Kubernetes or non-App-Operator tasks.
- read_only: list, inspect, check status, view logs, or discover existing resources.
- state_mutation: create, update, delete, sync, onboard, rollback, promote, abort, or change traffic.

Prefer intent-based interpretation over keyword matching.
Examples:
- "deploy my app" → ArgoCD onboarding or sync, depending on whether the app already exists.
- "zero downtime rollout" → Argo Rollouts canary or blue-green.
- "split traffic 80/20" → Traefik weighted routing or rollout traffic change.
- "rollback this" → ArgoCD rollback or rollout rollback, depending on the active delivery model.
- "show app health" → read_only status check.

If intent is ambiguous, ask one concise clarifying question instead of guessing.
</routing_rules>

<decision_policy>
For conversational_closure:
- Do not call any sub-agent or tool.
- Reply briefly and politely. This signals end-of-workflow to the supervisor.

For out_of_scope:
- Do not call any sub-agent or tool.
- Return the scope refusal structure defined in <scope>.

For read_only:
- Delegate once to the most relevant sub-agent with a clear [READ-ONLY] prefixed task.
- Do not create a plan, write_todos, or approval gate.
- Call `request_chat_continue` with a polished markdown summary of the result.
- Do NOT call `log_app_operation` for read-only results.

For state_mutation:
- Follow the Plan → Approve → Execute workflow defined in <workflow_state_mutation>.
- Never delegate a mutation without first confirming all required identifiers.
- Never fabricate missing resource names, namespaces, or application details.
- NEVER list sync, delete, abort, rollback, promote, or traffic-weight-change as DIRECT EXECUTE.
- Always call `log_app_operation` after a successful state-mutating operation.
- Always call `request_chat_continue` after completing the operation.
</decision_policy>

<parameter_completeness>
Before delegating any state-changing task, verify that all required identifiers are known.
Required identifiers vary by operation — see AGENTS.md §Parameter Completeness for the full table.

Resolve missing identifiers in this order:
1. Check the operations journal (auto-injected by AppOperationContextMiddleware).
2. Perform a [READ-ONLY] discovery delegation to enumerate available resources.
3. Call `request_chat_continue` to ask the user for the missing information.

Never guess or invent parameters for state-mutating tasks.
</parameter_completeness>

<workflow_state_mutation>
For any state_mutation request, follow this flow. See AGENTS.md §Planning Workflow for full detail.

1. Interpret — Identify the operational goal, target sub-agent, and whether live traffic is affected.
2. Plan — Call `write_todos` with the step checklist. Mark mutation steps with [MUTATION].
3. Approve — Call `request_user_input` with the plan summary, blast radius, and options:
   approve (✅) / reject (❌) / modify (✏️). ALWAYS include `options` — calling without options is an error.
4. Execute — Delegate each TODO with [PLAN-APPROVED] prefix so the sub-agent skips its own plan gate.
   Update TODO status via `write_todos` as you proceed (pending → in_progress → completed).
5. Verify — Run a read-only follow-up to confirm health, sync, or routing state.
6. Report — Return a concise markdown summary via `request_chat_continue`. See AGENTS.md §Response Format.

The HITL middleware at the sub-agent tool level still fires as the mechanical safety net — that is correct.
Sub-agents receiving [PLAN-APPROVED] MUST skip their internal plan review.
</workflow_state_mutation>

<read_only_behavior>
For read_only requests:
- Prefer a single sub-agent delegation.
- Return a synthesized markdown summary — not raw tool output.
- Use tables for lists (applications, rollouts, routes).
- Include health, sync, namespace, version, and repository when available.
- End with a short next-step prompt if the workflow is not finished.
- Do not ask unnecessary follow-up questions unless required identifiers are missing.
</read_only_behavior>

<response_style>
- Be concise, structured, and operational.
- Use headings, bullet points, and tables where useful.
- Use ✅, ⚠️, ❌ for status indicators.
- Avoid dumping raw manifests or unprocessed tool output.
- Write for users who may not speak DevOps fluently — translate intent into action-oriented language.
</response_style>

<safety_and_guardrails>
- Never interact with Kubernetes directly or via bash.
- Never bypass approval for state-changing operations.
- Never guess resource names, namespaces, or application details.
- Never delegate a mutation if required identifiers are incomplete.
- Never mix read-only discovery with mutation in the same task delegation unless explicitly safe.
- HITL policy and the authoritative gate list live in hitl-policies.md — refer there for full details.
</safety_and_guardrails>

<examples>
User: "List all apps in staging"
Intent: read_only
Action: delegate a [READ-ONLY] ArgoCD discovery task to argocd-onboarder.

User: "Onboard my app to ArgoCD"
Intent: state_mutation
Action: plan onboarding (project + repo + app + verify), confirm identifiers, execute.

User: "Promote canary to 50%"
Intent: state_mutation
Action: confirm rollout and traffic context, plan → approve → execute via argo-rollouts-onboarder.

User: "Check rollout health for frontend"
Intent: read_only
Action: delegate a single [READ-ONLY] status check to argo-rollouts-onboarder.

User: "Delete the frontend app"
Intent: state_mutation
Action: plan with blast radius review, request approval, execute deletion via argocd-onboarder.
</examples>

<output_contract>
For read_only results:
- Concise markdown summary. Tables for multiple resources.
- End with a short next-action prompt.

For state_mutation results:
- Concise operation summary: action performed, target, namespace, result.
- Mention any follow-up validation outcome.

For ambiguous requests:
- One focused clarifying question with a small set of options.

For out_of_scope:
- Brief refusal. Direct user to the appropriate operator.
</output_contract>

<planning_mode>
Planning rules and detailed workflow templates (PATH A write_todos examples, PATH B direct execute,
walkthrough format, step budget, rejection protocol) are in AGENTS.md.
Read AGENTS.md at session start — it is pre-seeded into your memory.
</planning_mode>
"""

class AppOperatorCoordinator(BaseDeepAgent):
    """
    App Operator Deep Agent Coordinator.
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        *,
        mcp_server_filter: Optional[List[str]] = None,
    ) -> None:
        super().__init__(config=config)
        self._mcp_server_filter = mcp_server_filter       

        logger.info("AppOperatorCoordinator initialized")

    @property
    def name(self) -> str:
        return "app-operator-coordinator"

    @property
    def system_prompt(self) -> str:
        return APP_COORDINATOR_PROMPT

    def get_task_categories(self) -> str:
        """App Operator domain-specific task categories."""
        return """\
- **Discovery**: Read-only ArgoCD/Rollout/Traefik inspection (list apps, check health, get routes)
- **Configuration**: Manifest generation, project setup, repo registration
- **Validation**: Dry-run, diff preview, policy checks
- **Live Apply**: ArgoCD app creation/sync, Rollout migration, route creation
- **Rollout**: Progressive delivery steps (canary promote, blue-green switch)
- **Health Check**: Post-change health/sync verification, traffic validation
- **Summary**: Generate walkthrough narrative from execution results"""

    @property
    def context_schema(self) -> type:
        return AppOperatorContext

    def get_model(self) -> Any:
        return create_model(self._config.get_llm_deepagent_config())

    async def get_subagent_specs(self) -> List[Any]:
        return get_app_subagent_specs(coordinator_model=self.get_model())

    async def get_tools(self) -> List[Any]:
        user_input = create_user_input_tool()
        chat_continue = create_chat_continue_tool()
        log_operation = create_log_app_operation_tool()
        return [user_input, chat_continue, log_operation]

    def get_skill_paths(self) -> List[str]:
        return [
            "/skills/app-operator/argocd-gitops",
            "/skills/app-operator/argo-rollouts-gitops",
            "/skills/app-operator/traefik-edge-routing",
        ]

    def get_memory_paths(self) -> List[str]:
        return [
            "/memories/app-operator/AGENTS.md",
            "/memories/app-operator/hitl-policies.md",
        ]

    def get_interrupt_config(self) -> Dict[str, Any]:
        return {}

    def make_backend(self) -> Any:
        from deepagents.backends import (
            CompositeBackend,
            FilesystemBackend,
            StateBackend,
            StoreBackend,
        )
        from k8s_autopilot.utils.memory import get_project_root
        
        root = get_project_root()
        default = FilesystemBackend(
            root_dir=str(root),
            virtual_mode=True,
        )

        _org = os.getenv("ORG_NAME", "default_org")

        return CompositeBackend(
            default=default,
            routes={
                "/memories/": StoreBackend(
                    namespace=lambda _rt: (_org,),
                ),
                "/shared/": StoreBackend(
                    namespace=lambda _rt: ("shared",),
                ),
                "/skills/": StateBackend(),
            },
        )

    def build_store(self) -> Any:
        store = InMemoryStore()
        project_root = get_project_root()
        memory_dir = project_root / "memory"

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
        
        # Pre-seed operations-log if not populated to prevent "File not found" read errors
        if store.get(namespace, "app-operator/operations-log.md") is None:
            empty_log = "# App Operations Journal\n\nAuto-generated log of operations performed in this session. Used by the coordinator to maintain context across conversation turns and after summarization.\n"
            store.put(namespace, "app-operator/operations-log.md", dict(create_file_data(empty_log)))

        return store

    def build_checkpointer(self) -> Any:
        """Return None to inherit the parent supervisor's checkpointer.

        Per-invocation mode (checkpointer=None) is the recommended pattern
        for subagents invoked as tools.  The child inherits the parent's
        checkpointer via the config passed to ainvoke(), enabling native
        interrupt()/resume support without manual bridging.

        Reference: LangGraph docs — Subgraph persistence / Per-invocation.
        """
        return None

    async def build_agent(self) -> Any:
        if getattr(self, "_agent", None):
            return self._agent

        logger.info("Building App Operator deep agent graph")

        self._store = self.build_store()
        checkpointer = self.build_checkpointer()
        tools = await self.get_tools()
        subagents = await self.get_subagent_specs()
        middleware = build_app_operator_middleware(
            config=self._config,
            model=self.get_model(),
            backend=self.make_backend(),
        )

        self._agent = create_deep_agent(
            model=self.get_model(),
            name=self.name,
            system_prompt=self.system_prompt,
            tools=tools,
            subagents=subagents,
            skills=self.get_skill_paths(),
            memory=self.get_memory_paths(),
            backend=self.make_backend(),
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
        return K8sBackendMixin.seed_files(
            skill_paths=self.get_skill_paths(),
            memory_paths=self.get_memory_paths(),
        )

    def input_transform(self, send_payload: Dict[str, Any]) -> Dict[str, Any]:
        messages = send_payload.get("messages", [])
        files = self.seed_files()
        transformed: Dict[str, Any] = {
            "messages": messages,
        }
        if files:
            transformed["files"] = files
        return transformed

    def build_context(
        self,
        supervisor_state: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        state = supervisor_state or {}

        ctx: Dict[str, Any] = {
            "argocd_server":        os.getenv("ARGOCD_SERVER", ""),
            "github_repo":          os.getenv("GITHUB_REPO", ""),
            "github_branch":        os.getenv("GITHUB_BRANCH", "main"),
            "workspace_path":       os.getenv("HELM_WORKSPACE", "./workspace/helm-charts"),
            "cluster_context":      os.getenv("K8S_CONTEXT", ""),
            "kubeconfig_path":      os.getenv("KUBECONFIG", ""),
            "default_namespace":    os.getenv("K8S_DEFAULT_NAMESPACE", "default"),
        }

        if state.get("session_id"):
            ctx["session_id"] = state["session_id"]
        if state.get("task_id"):
            ctx["task_id"] = state["task_id"]

        caller_ctx: Dict[str, Any] = state.get("context") or {}
        if isinstance(caller_ctx, dict):
            ctx.update({k: v for k, v in caller_ctx.items() if v is not None and v != ""})

        for key in ("github_repo", "argocd_server", "cluster_context", "kubeconfig_path"):
            if ctx.get(key) == "":
                ctx.pop(key, None)

        # ── Cross-domain context ──────────────────────────────────────
        # If the supervisor routed here after another coordinator deferred
        # with "outside my scope", inject the structured prior context so
        # the App agent can use it instead of asking the user.
        cross_domain = state.get("cross_domain_context")
        if isinstance(cross_domain, dict) and cross_domain:
            ctx["cross_domain_context"] = cross_domain

        # Propagate accumulated domain summaries for the blackboard pattern
        domain_summaries = state.get("domain_summaries")
        if isinstance(domain_summaries, list) and domain_summaries:
            ctx["domain_summaries"] = domain_summaries

        return ctx

    def output_transform(
        self,
        agent_state: Dict[str, Any],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        state: Dict[str, Any] = agent_state
        if not isinstance(agent_state, dict) and hasattr(agent_state, "model_dump"):
            state = agent_state.model_dump()

        final_message: Optional[str] = None
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            final_message = getattr(last_msg, "content", None) or (
                last_msg.get("content") if isinstance(last_msg, dict) else None
            )

        output: Dict[str, Any] = {
            "final_message": final_message or "App operator completed.",
            "status": "completed",
            "app_operator_output": {
                "messages": messages,
                "structured_response": state.get("structured_response"),
            },
            "domain_summary": extract_domain_summary(
                domain="app",
                final_message=final_message,
            ),
        }

        return output


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_app_operator_coordinator(
    config: Optional["Config"] = None,
    mcp_server_filter: Optional[List[str]] = None,
) -> AppOperatorCoordinator:
    """
    Create an AppOperatorCoordinator instance.

    Usage::

        from k8s_autopilot.core.agents.app_operator.coordinator import create_app_operator_coordinator
        coordinator = create_app_operator_coordinator(config)
    """
    return AppOperatorCoordinator(config=config, mcp_server_filter=mcp_server_filter)
