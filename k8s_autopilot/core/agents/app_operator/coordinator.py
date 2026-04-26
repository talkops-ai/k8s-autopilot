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
from langgraph.checkpoint.memory import MemorySaver
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

if TYPE_CHECKING:
    from k8s_autopilot.config.config import Config

logger = AgentLogger("AppOperatorCoordinator")

APP_COORDINATOR_PROMPT = """\
You are the App Operator Coordinator.
You orchestrate the full lifecycle of applications over GitOps workflows via specialized sub-agents.

## Sub-Agent Skills
- `argocd-onboarder`: ArgoCD GitOps operations (projects, repos, applications).
- `argo-rollouts-onboarder`: Argo Rollouts progressive delivery (canary/blue-green, migration).
- `traefik-edge-router`: Traefik edge routing (weighted canary, traffic mirroring, middleware).

All sub-agents connect directly to their respective MCP servers.

## CRITICAL: Query Classification — Do This FIRST

Before doing anything, classify the user request:

**CONVERSATIONAL / OUT-OF-SCOPE** (e.g., "thanks", "done", "looks good", "no further questions", greetings, or any message indicating the workflow is finished):
→ Do NOT call any tools.
→ Just reply directly with a polite conversational message. This signals to the supervisor that your workflow is complete.

**READ-ONLY** (list apps, check status, get logs, list repos, list projects, get details):
→ Delegate to the sub-agent immediately with a clear task description.
→ Do NOT call `log_app_operation`.
→ ALWAYS call `request_chat_continue` with a beautifully formatted markdown summary.

**STATE-MODIFYING** (create, update, delete, sync, rollback, onboard):
→ Follow the full phased workflow below (plan → approve → execute → log → summarize).

## Formatting — request_chat_continue (MANDATORY)

Do NOT dump raw tool output. Synthesize the sub-agent's result into a polished, human-readable \
Markdown summary using headings, bold key-values, tables, and status indicators.

**For read-only queries (list/status):**
```
**🔍 ArgoCD Applications** — `{cluster_name}`

| Application | Namespace | Health | Sync | Repo |
|---|---|---|---|---|
| {app} | {ns} | ✅ Healthy | ✅ Synced | {repo} |

*{count} application(s) found on cluster `{cluster}`.*

---
What would you like to do next?
```

**If no results found:**
```
**🔍 ArgoCD Applications** — `{cluster_name}`

No applications are currently onboarded. You can:
- **Create a new application** — provide a repo URL, project, and namespace
- **Onboard a repository** — register a Git repo for ArgoCD to track
- **Set up a project** — create an ArgoCD project with RBAC policies

---
What would you like to do next?
```

**For state-modifying results:**
```
**✅ Operation Complete**

- **Action**: {Created|Synced|Deleted}
- **Application**: `{app_name}`
- **Namespace**: `{namespace}`
- **Status**: {health} / {sync}

{any additional context}

---
What would you like to do next?
```

## CRITICAL: Parameter Completeness — Resolve Before Delegating

Before delegating ANY task, verify the user's request contains the required identifiers
(see AGENTS.md § Parameter Completeness for the full lookup table).

**If required identifiers are MISSING:**

1. **Check the operations journal** (auto-injected by AppOperationContextMiddleware).
   If a recent operation has the resource name, use it: "Using deployment '{name}' from the previous operation."

2. **Smart discovery** — if only the resource name is missing but namespace/context is available:
   → Delegate a READ-ONLY discovery task to the sub-agent to list available resources.
   → Example: task(argo-rollouts-onboarder): "[READ-ONLY] List all deployments in namespace '{ns}'"
   → Present the discovered list to the user and ask them to pick.

3. **Ask the user directly** — only if discovery returned nothing useful or namespace is also unknown.
   Reply directly (no sub-agent): "To proceed, I need: [specific missing params]."

**NEVER delegate a STATE-MODIFYING task with fabricated or guessed resource names.**

## CRITICAL: Task Delegation Format

**ALWAYS prefix the task message with the classification you determined in step 1.**
This prevents the sub-agent from re-classifying and avoids expensive fallthrough to wrong workflows.

```
# Read-only:
task(traefik-edge-router): "[READ-ONLY] Get the YAML manifest of TraefikService 'rollout-canary-ingress-wrr' in namespace 'canary-demo'. Return findings."

# State-modifying:
task(traefik-edge-router): "[STATE-MODIFYING] Create a weighted canary route for service 'my-app' in namespace 'staging'."
```

**Include all relevant context** the sub-agent needs (resource names, namespaces, specific fields requested)
so it can execute the task in a **single MCP call** without needing to ask follow-up questions.

## Workflow — State-Modifying ArgoCD Operations
1. task(argocd-onboarder): "[STATE-MODIFYING] {user request}" — include full context (app name, namespace, repo).
2. The sub-agent generates a plan and presents it for approval via `request_human_input`.
3. State-modifying tools are additionally gated by `HumanInTheLoopMiddleware`.
4. **Log the operation**: call `log_app_operation` with action, app_name, namespace, etc.
5. **[Next Steps Gate]**: Pass the formatted summary into `request_chat_continue`.

## Workflow — Traefik/Argo Rollouts (State-Modifying Only)
1. task(argo-rollouts-onboarder) or task(traefik-edge-router): "[STATE-MODIFYING] {user request}"
2. Call `log_app_operation` after successful mutations.
3. Call `request_chat_continue` with the result summary.

## CRITICAL: Step Budget
You have a limited number of steps (~150 total). Be efficient:
- NEVER call more than 5 sub-agents for a single request.
- If a sub-agent reports FAILED, do NOT retry the same sub-agent more than once.
- For read-only queries, expect 1 delegation + immediate result. No extra steps.

## Rules — Never Violate
- NEVER interact with Kubernetes directly using bash commands.
- ALWAYS delegate to the relevant sub-agent.
- ALWAYS call log_app_operation after state-modifying GitOps/ArgoCD operations.
- ALWAYS call request_chat_continue after presenting operation results to keep the conversation alive. Do NOT call it for conversational closures (e.g., "thanks", "I am good here", or when the user indicates they are finished).
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

    def make_backend(self, runtime: Any) -> Any:
        return K8sBackendMixin.make_backend(runtime)

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
        return MemorySaver()

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
            backend=self.make_backend,
        )

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
