"""Middleware for injecting local context into system prompt.

Detects local host environment, git state, project structure, K8s cluster context,
and tracing info, caching it in state during before_agent / abefore_agent and
injecting into the system prompt for the model calls.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import platform
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, Protocol, cast, runtime_checkable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import SystemMessage

from k8s_autopilot.middleware.registry import register_middleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from langgraph.runtime import Runtime

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_DETECT_SCRIPT_TIMEOUT = 10


@runtime_checkable
class _ExecutableBackend(Protocol):
    """Any backend that supports execute(command) -> ExecuteResponse."""

    def execute(
        self, command: str, *, timeout: int | None = None
    ) -> Any: ...


@runtime_checkable
class _AsyncExecutableBackend(Protocol):
    """Any backend that provides an async aexecute method."""

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> Any: ...


def _section_header() -> str:
    return r"""CWD="$(pwd)"
echo "## Local Context"
echo ""
echo "**Current Directory**: \`${CWD}\`"
echo ""

# --- Check git and resolve its root once ---
IN_GIT=false
ROOT=""
if command -v git >/dev/null 2>&1; then
  GIT_INFO="$(git rev-parse --is-inside-work-tree --show-toplevel 2>/dev/null)"
  GIT_MODE="${GIT_INFO%%$'\n'*}"
  case "$GIT_MODE" in
    true)
      IN_GIT=true
      ROOT="${GIT_INFO#*$'\n'}"
      ;;
    false) IN_GIT=true ;;
  esac
fi"""


def _section_project() -> str:
    return r"""# --- Project ---
PROJ_LANG=""
[ -f pyproject.toml ] || [ -f setup.py ] && PROJ_LANG="python"
[ -z "$PROJ_LANG" ] && [ -f package.json ] && PROJ_LANG="javascript/typescript"
[ -z "$PROJ_LANG" ] && [ -f Cargo.toml ] && PROJ_LANG="rust"
[ -z "$PROJ_LANG" ] && [ -f go.mod ] && PROJ_LANG="go"
[ -z "$PROJ_LANG" ] && { [ -f pom.xml ] || [ -f build.gradle ]; } && PROJ_LANG="java"

MONOREPO=false
{ [ -f lerna.json ] || [ -f pnpm-workspace.yaml ] \
  || [ -d packages ] || { [ -d libs ] && [ -d apps ]; } \
  || [ -d workspaces ]; } && MONOREPO=true

ENVS=""
{ [ -d .venv ] || [ -d venv ]; } && ENVS=".venv"
[ -d node_modules ] && ENVS="${ENVS:+${ENVS}, }node_modules"

HAS_PROJECT=false
{ [ -n "$PROJ_LANG" ] || { [ -n "$ROOT" ] && [ "$ROOT" != "$CWD" ]; } \
  || $MONOREPO || [ -n "$ENVS" ]; } && HAS_PROJECT=true

if $HAS_PROJECT; then
  echo "**Project**:"
  [ -n "$PROJ_LANG" ] && echo "- Language: ${PROJ_LANG}"
  [ -n "$ROOT" ] && [ "$ROOT" != "$CWD" ] && echo "- Project root: \`${ROOT}\`"
  $MONOREPO && echo "- Monorepo: yes"
  [ -n "$ENVS" ] && echo "- Environments: ${ENVS}"
  echo ""
fi"""


def _section_package_managers() -> str:
    return r"""# --- Package managers ---
PKG=""
if [ -f uv.lock ]; then PKG="Python: uv"
elif [ -f poetry.lock ]; then PKG="Python: poetry"
elif [ -f Pipfile.lock ] || [ -f Pipfile ]; then PKG="Python: pipenv"
elif [ -f pyproject.toml ]; then
  if grep -q '\[tool\.uv\]' pyproject.toml 2>/dev/null; then PKG="Python: uv"
  elif grep -q '\[tool\.poetry\]' pyproject.toml 2>/dev/null; then PKG="Python: poetry"
  else PKG="Python: pip"
  fi
elif [ -f requirements.txt ]; then PKG="Python: pip"
fi

NODE_PKG=""
if [ -f bun.lockb ] || [ -f bun.lock ]; then NODE_PKG="Node: bun"
elif [ -f pnpm-lock.yaml ]; then NODE_PKG="Node: pnpm"
elif [ -f yarn.lock ]; then NODE_PKG="Node: yarn"
elif [ -f package-lock.json ] || [ -f package.json ]; then NODE_PKG="Node: npm"
fi
[ -n "$NODE_PKG" ] && PKG="${PKG:+${PKG}, }${NODE_PKG}"
[ -n "$PKG" ] && echo "**Package Manager**: ${PKG}" && echo ""
"""


def _section_runtimes() -> str:
    return r"""# --- Runtimes ---
_RT_TMP="${_DCT:-}"
_RT_CLEANUP=false
if [ -z "$_RT_TMP" ]; then
  _RT_TMP="$(mktemp -d)" || exit 1
  _RT_CLEANUP=true
fi

HAS_PYTHON=false
if command -v python3 >/dev/null 2>&1; then
  python3 --version > "$_RT_TMP/runtime_python" 2>/dev/null &
  HAS_PYTHON=true
fi
HAS_NODE=false
if command -v node >/dev/null 2>&1; then
  node --version > "$_RT_TMP/runtime_node" 2>/dev/null &
  HAS_NODE=true
fi
wait

RT=""
if $HAS_PYTHON && [ -s "$_RT_TMP/runtime_python" ]; then
  IFS= read -r PV < "$_RT_TMP/runtime_python"
  PV="${PV#* }"
  PV="${PV%% *}"
  [ -n "$PV" ] && RT="Python ${PV}"
fi
if $HAS_NODE && [ -s "$_RT_TMP/runtime_node" ]; then
  IFS= read -r NV < "$_RT_TMP/runtime_node"
  NV="${NV#v}"
  [ -n "$NV" ] && RT="${RT:+${RT}, }Node ${NV}"
fi
$_RT_CLEANUP && rm -rf "$_RT_TMP"
[ -n "$RT" ] && echo "**Detected Runtimes**: ${RT}" && echo ""
"""


def _section_git() -> str:
    return r"""# --- Git ---
if $IN_GIT; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [ "$BRANCH" = "HEAD" ]; then
    COMMIT="$(git rev-parse --short HEAD 2>/dev/null)"
    GT="**Git**: Detached HEAD at \`${COMMIT}\`"
  else
    GT="**Git**: Current branch \`${BRANCH}\`"
  fi

  MAINS=""
  for b in $(git for-each-ref --format='%(refname:short)' \
      refs/heads/main refs/heads/master 2>/dev/null); do
    case "$b" in
      main) MAINS="${MAINS:+${MAINS}, }\`main\`" ;;
      master) MAINS="${MAINS:+${MAINS}, }\`master\`" ;;
    esac
  done
  [ -n "$MAINS" ] && GT="${GT}, ${MAINS} available"

  DC=$(git status --porcelain 2>/dev/null | awk 'END { print NR }')
  if [ "$DC" -gt 0 ]; then
    if [ "$DC" -eq 1 ]; then GT="${GT}, 1 uncommitted change"
    else GT="${GT}, ${DC} uncommitted changes"
    fi
  fi

  echo "$GT"
  echo ""
fi"""


def build_detect_script() -> str:
    serial_prefix = f"{_section_header()}\n{_section_project()}"
    parallel_sections = [
        ("02_pkgmgr", _section_package_managers()),
        ("03_runtimes", _section_runtimes()),
        ("04_git", _section_git()),
    ]
    parallel_setup = "_DCT=$(mktemp -d) || exit 1\ntrap 'rm -rf \"$_DCT\"' EXIT"
    parallel_block = "\n".join(
        f'(\n{body}\n) > "$_DCT/{name}" 2>/dev/null &'
        for name, body in parallel_sections
    )
    cat_line = "cat " + " ".join(f'"$_DCT/{name}"' for name, _ in parallel_sections)
    body = f"{serial_prefix}\n{parallel_setup}\n{parallel_block}\nwait\n{cat_line}"
    return f"bash <<'__DETECT_CONTEXT_EOF__'\n{body}\n__DETECT_CONTEXT_EOF__\n"


DETECT_CONTEXT_SCRIPT = build_detect_script()


def _build_k8s_context_section(
    working_dir: str = ".",
    tracing_project: str | None = None,
    user_tracing_project: str | None = None,
) -> str:
    """Build a static context section with K8s environment info and tracing project."""
    lines: list[str] = []
    lines.append("### Kubernetes Environment")
    lines.append(f"Host Platform: {platform.system()} {platform.release()}")

    if tracing_project:
        lines.append(f"LangSmith Tracing Project: {tracing_project}")
        if user_tracing_project and user_tracing_project != tracing_project:
            lines.append(f"User Tracing Project: {user_tracing_project}")

    kubeconfig = os.environ.get("KUBECONFIG", "")
    kube_context = os.environ.get("KUBE_CONTEXT", "")
    kube_namespace = os.environ.get("KUBE_NAMESPACE", "default")

    if kubeconfig:
        lines.append(f"KUBECONFIG: {kubeconfig}")
    if kube_context:
        lines.append(f"Kubernetes context: {kube_context}")
    lines.append(f"Kubernetes namespace: {kube_namespace}")

    helm_home = os.environ.get("HELM_HOME", "")
    if helm_home:
        lines.append(f"HELM_HOME: {helm_home}")

    argocd_server = os.environ.get("ARGOCD_SERVER", "")
    if argocd_server:
        lines.append(f"ArgoCD server: {argocd_server}")

    for tool_name, env_var in [
        ("Prometheus", "PROMETHEUS_URL"),
        ("Grafana", "GRAFANA_URL"),
        ("Loki", "LOKI_URL"),
        ("Tempo", "TEMPO_URL"),
        ("Alertmanager", "ALERTMANAGER_URL"),
    ]:
        url = os.environ.get(env_var, "")
        if url:
            lines.append(f"{tool_name}: {url}")

    return "\n".join(lines)


class LocalContextState(AgentState):
    """State for local context middleware."""

    _local_context: NotRequired[Annotated[str, PrivateStateAttr]]
    _local_context_refreshed_at_cutoff: NotRequired[Annotated[int, PrivateStateAttr]]


@register_middleware(name="local_context")
class LocalContextMiddleware(AgentMiddleware[LocalContextState, Any]):
    """Inject local context (git state, project structure, K8s cluster) into the system prompt."""

    state_schema = LocalContextState

    def __init__(
        self,
        backend: Any | None = None,
        *,
        working_dir: str | Path = ".",
        tracing_project: str | None = None,
        user_tracing_project: str | None = None,
    ) -> None:
        self.backend = backend
        self._working_dir = str(working_dir)
        self._tracing_project = tracing_project
        self._user_tracing_project = user_tracing_project
        self._static_k8s_context = _build_k8s_context_section(
            self._working_dir,
            tracing_project=self._tracing_project,
            user_tracing_project=self._user_tracing_project,
        )

    def _run_detect_script(self) -> str | None:
        backend = self.backend
        if backend is None:
            return f"**Platform**: {platform.system()} {platform.release()}\n**Working Directory**: `{self._working_dir}`"
        if not isinstance(backend, _ExecutableBackend):
            return None
        try:
            result = backend.execute(
                DETECT_CONTEXT_SCRIPT, timeout=_DETECT_SCRIPT_TIMEOUT
            )
            output = getattr(result, "output", "") or ""
            return output.strip() or None
        except Exception:
            logger.warning(
                "Local context detection failed (backend: %s)",
                type(backend).__name__,
                exc_info=True,
            )
            return None

    async def _arun_detect_script(self) -> str | None:
        backend = self.backend
        if backend is None:
            return f"**Platform**: {platform.system()} {platform.release()}\n**Working Directory**: `{self._working_dir}`"
        if isinstance(backend, _AsyncExecutableBackend) and inspect.iscoroutinefunction(backend.aexecute):
            try:
                result = await backend.aexecute(
                    DETECT_CONTEXT_SCRIPT, timeout=_DETECT_SCRIPT_TIMEOUT
                )
                output = getattr(result, "output", "") or ""
                return output.strip() or None
            except Exception:
                logger.warning(
                    "Async local context detection failed (backend: %s)",
                    type(backend).__name__,
                    exc_info=True,
                )
                return None
        return await asyncio.to_thread(self._run_detect_script)

    def before_agent(
        self,
        state: LocalContextState,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        if state.get("_local_context"):
            return None
        output = self._run_detect_script()
        if output:
            return {"_local_context": output}
        return None

    async def abefore_agent(
        self,
        state: LocalContextState,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        if state.get("_local_context"):
            return None
        output = await self._arun_detect_script()
        if output:
            return {"_local_context": output}
        return None

    def _get_modified_request(self, request: ModelRequest) -> ModelRequest | None:
        state = cast("LocalContextState", request.state)
        local_context = state.get("_local_context", "") if isinstance(state, dict) else ""
        system_prompt = request.system_prompt or ""

        parts = [system_prompt]
        if local_context:
            parts.append(local_context)
        if self._static_k8s_context:
            parts.append(self._static_k8s_context)

        return request.override(
            system_message=SystemMessage(content="\n\n".join(filter(None, parts)))
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        modified_request = self._get_modified_request(request)
        return handler(modified_request or request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        modified_request = self._get_modified_request(request)
        return await handler(modified_request or request)


__all__ = ["LocalContextMiddleware", "LocalContextState"]
