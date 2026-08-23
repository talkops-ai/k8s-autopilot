"""Middleware for injecting local context into system prompt.

Detects git state, project structure, package managers, runtimes, and
directory layout by running a bash script via the backend. Because the
script executes inside the backend (local shell or remote sandbox), the
same detection logic works regardless of where the agent runs.
"""

from __future__ import annotations

import asyncio
import json
from k8s_autopilot.utils.logger import AgentLogger
import unicodedata
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    NotRequired,
    Protocol,
    cast,
    runtime_checkable,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from deepagents.backends.protocol import ExecuteResponse
    from deepagents.middleware.summarization import SummarizationEvent
    from langgraph.runtime import Runtime


# ---------------------------------------------------------------------------
# Sanitize / Security Helpers (Ported inline from unicode_security.py)
# ---------------------------------------------------------------------------

_DANGEROUS_CODEPOINTS: frozenset[int] = frozenset(
    {
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
        0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF,
        0x00AD, 0x034F, 0x115F, 0x1160,
    }
)
_DANGEROUS_CHARACTERS: frozenset[str] = frozenset(
    chr(codepoint) for codepoint in _DANGEROUS_CODEPOINTS
)


def strip_dangerous_unicode(text: str) -> str:
    """Remove known dangerous/invisible Unicode characters from text."""
    return "".join(ch for ch in text if ch not in _DANGEROUS_CHARACTERS)


def sanitize_control_chars(
    text: str,
    *,
    keep_newlines: bool = False,
    collapse_whitespace: bool = True,
    max_length: int | None = None,
) -> str:
    """Neutralize control characters and deceptive Unicode in untrusted text."""
    allowed = {" ", "\n"} if keep_newlines else {" "}
    cleaned = "".join(
        ch if ch in allowed or not unicodedata.category(ch).startswith("C") else " "
        for ch in strip_dangerous_unicode(text)
    )
    if collapse_whitespace:
        if keep_newlines:
            cleaned = "\n".join(" ".join(line.split()) for line in cleaned.split("\n"))
        else:
            cleaned = " ".join(cleaned.split())
    if max_length is not None and len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 1].rstrip() + "…"
    return cleaned


# ---------------------------------------------------------------------------
# Dataclasses & Protocols
# ---------------------------------------------------------------------------

@dataclass
class MCPServerInfo:
    """Metadata describing a connected MCP server."""
    name: str
    transport: str
    tools: list[Any]
    status: str = "ok"
    error: str | None = None


@runtime_checkable
class _ExecutableBackend(Protocol):
    """Any backend that supports `execute(command) -> ExecuteResponse`."""

    def execute(
        self, command: str, *, timeout: int | None = None
    ) -> ExecuteResponse: ...


@runtime_checkable
class _AsyncExecutableBackend(Protocol):
    """Any backend that provides an async `aexecute` method."""

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse: ...


logger = AgentLogger("LocalContextMW")

_TOOL_NAME_DISPLAY_LIMIT = 10
_DETECT_SCRIPT_TIMEOUT = 30
_MCP_ERROR_DETAIL_LIMIT = 200
_TRACING_PROJECT_NAME_LIMIT = 200


def _sanitize_error_detail(error: str | None) -> str:
    if not error:
        return "unknown error"
    sanitized = sanitize_control_chars(error, max_length=_MCP_ERROR_DETAIL_LIMIT)
    return sanitized or "unknown error"


def _sanitize_tracing_project_name(project: str) -> str:
    sanitized = sanitize_control_chars(project, max_length=_TRACING_PROJECT_NAME_LIMIT)
    return sanitized or "unknown project"


def _quote_tracing_project_name(project: str) -> str:
    return json.dumps(project, ensure_ascii=False)


def _build_mcp_context(servers: list[MCPServerInfo]) -> str:
    if not servers:
        return ""

    total_tools = sum(len(s.tools) for s in servers)
    lines = [f"**MCP Servers** ({len(servers)} servers, {total_tools} tools):"]

    for server in servers:
        if not server.tools:
            if server.status == "error":
                detail = _sanitize_error_detail(server.error)
                lines.append(
                    f"- **{server.name}** ({server.transport}): "
                    f"FAILED TO LOAD — <error>{detail}</error>. "
                    "Treat this integration as temporarily unavailable; "
                    "tell the user the server failed to load and suggest "
                    "restarting the MCP server."
                )
            elif server.status == "unauthenticated":
                detail = _sanitize_error_detail(server.error)
                lines.append(
                    f"- **{server.name}** ({server.transport}): "
                    f"NEEDS LOGIN — <error>{detail}</error>. "
                    "This integration requires authentication before its "
                    "tools are available; tell the user and suggest running "
                    "`/mcp` to log in."
                )
            elif server.status == "disabled":
                lines.append(
                    f"- **{server.name}** ({server.transport}): (disabled by user)"
                )
            else:
                lines.append(
                    f"- **{server.name}** ({server.transport}): (no tools registered)"
                )
            continue

        names = [getattr(t, "name", str(t)) for t in server.tools]
        if len(names) > _TOOL_NAME_DISPLAY_LIMIT:
            shown = ", ".join(names[:_TOOL_NAME_DISPLAY_LIMIT])
            remaining = len(names) - _TOOL_NAME_DISPLAY_LIMIT
            lines.append(
                f"- **{server.name}** ({server.transport}): "
                f"{shown}, and {remaining} more"
            )
        else:
            lines.append(
                f"- **{server.name}** ({server.transport}): {', '.join(names)}"
            )

    return "\n".join(lines)


def _build_tracing_context(
    agent_project: str | None,
    user_project: str | None,
) -> str:
    if not agent_project:
        return ""

    safe_agent_project = _sanitize_tracing_project_name(agent_project)
    quoted_agent_project = _quote_tracing_project_name(safe_agent_project)
    lines = [
        "**LangSmith Tracing**:",
        f"- Agent traces: project {quoted_agent_project}",
    ]
    if user_project:
        safe_user_project = _sanitize_tracing_project_name(user_project)
        if safe_user_project != safe_agent_project:
            quoted_user_project = _quote_tracing_project_name(safe_user_project)
            lines.append(f"- Shell-command traces: project {quoted_user_project}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context detection scripts
# ---------------------------------------------------------------------------

def _section_header() -> str:
    return r"""CWD="$(pwd)"
echo "## Local Context"
echo ""
echo "**Current Directory**: \`${CWD}\`"
echo ""

# --- Check git once ---
IN_GIT=false
if command -v git >/dev/null 2>&1 \
    && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  IN_GIT=true
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

ROOT=""
$IN_GIT && ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"

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
RT=""
if command -v python3 >/dev/null 2>&1; then
  PV="$(python3 --version 2>/dev/null | awk '{print $2}')"
  [ -n "$PV" ] && RT="Python ${PV}"
fi
if command -v node >/dev/null 2>&1; then
  NV="$(node --version 2>/dev/null | sed 's/^v//')"
  [ -n "$NV" ] && RT="${RT:+${RT}, }Node ${NV}"
fi
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
  for b in $(git branch 2>/dev/null | sed 's/^[* ]*//'); do
    case "$b" in
      main) MAINS="${MAINS:+${MAINS}, }\`main\`" ;;
      master) MAINS="${MAINS:+${MAINS}, }\`master\`" ;;
    esac
  done
  [ -n "$MAINS" ] && GT="${GT}, ${MAINS} available"

  DC=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  if [ "$DC" -gt 0 ]; then
    if [ "$DC" -eq 1 ]; then GT="${GT}, 1 uncommitted change"
    else GT="${GT}, ${DC} uncommitted changes"
    fi
  fi

  echo "$GT"
  echo ""
fi"""


def _section_gh_cli() -> str:
    return r"""# --- GitHub CLI ---
if command -v gh >/dev/null 2>&1; then
  _gh_json_fields() {
    gh search "$1" --help 2>/dev/null \
      | awk '
        /^JSON FIELDS/ { in_fields = 1; next }
        in_fields && /^$/ { exit }
        in_fields { gsub(/^  /, ""); print }
      ' \
      | tr '\n' ' ' \
      | sed 's/  */ /g; s/^ //; s/ $//'
  }

  GH_PRS_FIELDS="$(_gh_json_fields prs)"
  GH_ISSUES_FIELDS="$(_gh_json_fields issues)"
  if [ -n "$GH_PRS_FIELDS" ] || [ -n "$GH_ISSUES_FIELDS" ]; then
    echo "**GitHub CLI**:"
    [ -n "$GH_PRS_FIELDS" ] \
      && echo "- \`gh search prs --json\` fields: ${GH_PRS_FIELDS}"
    [ -n "$GH_ISSUES_FIELDS" ] \
      && echo "- \`gh search issues --json\` fields: ${GH_ISSUES_FIELDS}"
    case ",$GH_PRS_FIELDS," in
      *mergedAt*) ;;
      *) echo "- \`gh search prs --json\` does not expose \`mergedAt\`;"
         echo "  use \`gh pr view --json mergedAt\` per PR for merge timestamps." ;;
    esac
    echo ""
  fi
fi"""


def _section_test_command() -> str:
    return r"""# --- Test command ---
TC=""
if [ -f Makefile ] && grep -qE '^tests?:' Makefile 2>/dev/null; then TC="make test"
elif [ -f pyproject.toml ]; then
  if grep -q '\[tool\.pytest' pyproject.toml 2>/dev/null \
      || [ -f pytest.ini ] || [ -d tests ] || [ -d test ]; then
    TC="pytest"
  fi
elif [ -f package.json ] \
    && grep -q '"test"' package.json 2>/dev/null; then
  TC="npm test"
fi
[ -n "$TC" ] && echo "**Run Tests**: \`${TC}\`" && echo ""
"""


def _section_files() -> str:
    return r"""# --- Files ---
EXCL='node_modules|__pycache__|\.pytest_cache'
EXCL="${EXCL}|\.mypy_cache|\.ruff_cache|\.tox"
EXCL="${EXCL}|\.coverage|\.eggs|dist|build"
FILES=$(
  { ls -1 2>/dev/null; [ -e .deepagents ] && echo .deepagents; } |
  grep -vE "^(${EXCL})$" |
  sort -u
)
if [ -n "$FILES" ]; then
  TOTAL=$(echo "$FILES" | wc -l | tr -d ' ')
  SHOWN_FILES=$(echo "$FILES" | head -20)
  SHOWN=$(echo "$SHOWN_FILES" | wc -l | tr -d ' ')
  TOTAL=${TOTAL:-0}
  SHOWN=${SHOWN:-0}
  if [ "$SHOWN" -lt "$TOTAL" ]; then
    echo "**Files** (showing ${SHOWN} of ${TOTAL}):"
  else
    echo "**Files** (${TOTAL}):"
  fi
  echo "$SHOWN_FILES" | while IFS= read -r f; do
    if [ -d "$f" ]; then echo "- ${f}/"
    else echo "- ${f}"
    fi
  done
  echo ""
fi"""


def _section_tree() -> str:
    return r"""# --- Tree ---
if command -v tree >/dev/null 2>&1; then
  TREE_EXCL='node_modules|.venv|__pycache__|.pytest_cache'
  TREE_EXCL="${TREE_EXCL}|.git|.mypy_cache|.ruff_cache"
  TREE_EXCL="${TREE_EXCL}|.tox|.coverage|.eggs|dist|build"
  T_PREVIEW=$(tree -L 3 --noreport --dirsfirst \
    -I "$TREE_EXCL" 2>/dev/null | sed -n '1,22p;23{p;q;}')
  if [ -n "$T_PREVIEW" ]; then
    PREVIEW_LINES=$(echo "$T_PREVIEW" | wc -l | tr -d ' ')
    PREVIEW_LINES=${PREVIEW_LINES:-0}
    T="$T_PREVIEW"
    TREE_TRUNCATED=false
    if [ "$PREVIEW_LINES" -gt 22 ]; then
      T=$(echo "$T_PREVIEW" | head -22)
      TREE_TRUNCATED=true
    fi
    echo "**Tree** (3 levels):"
    echo '```text'
    echo "$T"
    $TREE_TRUNCATED && echo "... (more lines truncated)"
    echo '```'
    echo ""
  fi
fi"""


def _section_makefile() -> str:
    return r"""# --- Makefile ---
MK=""
if [ -f Makefile ]; then
  MK="Makefile"
elif [ -n "$ROOT" ] && [ "$ROOT" != "$CWD" ] && [ -f "${ROOT}/Makefile" ]; then
  MK="${ROOT}/Makefile"
fi
if [ -n "$MK" ]; then
  echo "**Makefile** (\`${MK}\`, first 20 lines):"
  echo '```makefile'
  head -20 "$MK"
  TL=$(wc -l < "$MK" | tr -d ' ')
  [ "$TL" -gt 20 ] && echo "... (truncated)"
  echo '```'
fi"""


def _section_k8s_context() -> str:
    """K8s cluster context detection — current context and namespace."""
    return r"""# --- K8s Context ---
if command -v kubectl >/dev/null 2>&1; then
  K8S_CTX="$(kubectl config current-context 2>/dev/null)"
  K8S_NS="$(kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}' 2>/dev/null)"
  [ -z "$K8S_NS" ] && K8S_NS="default"
  if [ -n "$K8S_CTX" ]; then
    echo "**Kubernetes**: Context \`${K8S_CTX}\`, namespace \`${K8S_NS}\`"
    echo ""
  fi
fi"""


def _section_helm_releases() -> str:
    """Helm releases detection — list deployed releases."""
    return r"""# --- Helm Releases ---
if command -v helm >/dev/null 2>&1; then
  HR="$(helm list --short 2>/dev/null | head -10)"
  if [ -n "$HR" ]; then
    HC=$(echo "$HR" | wc -l | tr -d ' ')
    echo "**Helm Releases** (${HC} shown):"
    echo "$HR" | while IFS= read -r r; do echo "- ${r}"; done
    echo ""
  fi
fi"""


def build_detect_script() -> str:
    serial_prefix = f"{_section_header()}\n{_section_project()}"
    parallel_sections = [
        ("02_pkgmgr", _section_package_managers()),
        ("03_runtimes", _section_runtimes()),
        ("04_git", _section_git()),
        ("05_gh_cli", _section_gh_cli()),
        ("06_testcmd", _section_test_command()),
        ("07_files", _section_files()),
        ("08_tree", _section_tree()),
        ("09_makefile", _section_makefile()),
        ("10_k8s_context", _section_k8s_context()),
        ("11_helm_releases", _section_helm_releases()),
    ]

    parallel_setup = "_DCT=$(mktemp -d) || exit 1\ntrap 'rm -rf \"$_DCT\"' EXIT"
    parallel_block = "\n".join(
        f'(\n{body}\n) > "$_DCT/{name}" 2>"$_DCT/{name}.err" &'
        for name, body in parallel_sections
    )
    cat_line = "cat " + " ".join(f'"$_DCT/{name}"' for name, _ in parallel_sections)

    body = f"{serial_prefix}\n{parallel_setup}\n{parallel_block}\nwait\n{cat_line}"
    return f"bash <<'__DETECT_CONTEXT_EOF__'\n{body}\n__DETECT_CONTEXT_EOF__\n"


DETECT_CONTEXT_SCRIPT = build_detect_script()


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class LocalContextState(AgentState):
    """State for local context middleware."""
    _local_context: NotRequired[Annotated[str, PrivateStateAttr]]
    _local_context_refreshed_at_cutoff: NotRequired[Annotated[int, PrivateStateAttr]]


# ---------------------------------------------------------------------------
# Middleware Implementation
# ---------------------------------------------------------------------------

from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware


@register_middleware(name="local_context")
class LocalContextMiddleware(BaseAgentMiddleware):
    """Inject local context (git state, project structure, etc.) into the system prompt."""

    state_schema = LocalContextState

    def __init__(
        self,
        backend: Any = None,
        *,
        mcp_server_info: list[MCPServerInfo] | None = None,
        tracing_project: str | None = None,
        user_tracing_project: str | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self._mcp_context = _build_mcp_context(mcp_server_info or [])
        self._tracing_context = _build_tracing_context(
            tracing_project, user_tracing_project
        )

    @staticmethod
    def _handle_detect_result(result: Any) -> str | None:
        output = getattr(result, "output", "").strip()
        exit_code = getattr(result, "exit_code", 0)
        if exit_code is None or exit_code != 0:
            logger.warning(
                "Local context detection script failed; context will be omitted."
            )
            return None
        return output or None

    def _run_detect_script(self) -> str | None:
        backend = self.backend
        if backend is None or not hasattr(backend, "execute"):
            return None
        try:
            result = backend.execute(
                DETECT_CONTEXT_SCRIPT, timeout=_DETECT_SCRIPT_TIMEOUT
            )
            return LocalContextMiddleware._handle_detect_result(result)
        except Exception:
            logger.warning("Local context detection failed", exc_info=True)
            return None

    async def _arun_detect_script(self) -> str | None:
        backend = self.backend
        if backend is None:
            return None
        if hasattr(backend, "aexecute"):
            try:
                result = await backend.aexecute(
                    DETECT_CONTEXT_SCRIPT, timeout=_DETECT_SCRIPT_TIMEOUT
                )
                return LocalContextMiddleware._handle_detect_result(result)
            except Exception:
                pass
        if hasattr(backend, "execute"):
            try:
                return await asyncio.to_thread(self._run_detect_script)
            except Exception:
                pass
        return None

    def before_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        raw_event = state.get("_summarization_event")
        if raw_event is not None:
            event: Any = raw_event
            cutoff = event.get("cutoff_index") if isinstance(event, dict) else getattr(event, "cutoff_index", None)
            refreshed_cutoff = state.get("_local_context_refreshed_at_cutoff")
            if cutoff != refreshed_cutoff:
                output = self._run_detect_script()
                if output:
                    return {
                        "_local_context": output,
                        "_local_context_refreshed_at_cutoff": cutoff,
                    }
                return {"_local_context_refreshed_at_cutoff": cutoff}

        if state.get("_local_context"):
            return None

        output = self._run_detect_script()
        if output:
            return {"_local_context": output}
        return None

    async def abefore_agent(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        raw_event = state.get("_summarization_event")
        if raw_event is not None:
            event: Any = raw_event
            cutoff = event.get("cutoff_index") if isinstance(event, dict) else getattr(event, "cutoff_index", None)
            refreshed_cutoff = state.get("_local_context_refreshed_at_cutoff")
            if cutoff != refreshed_cutoff:
                output = await self._arun_detect_script()
                if output:
                    return {
                        "_local_context": output,
                        "_local_context_refreshed_at_cutoff": cutoff,
                    }
                return {"_local_context_refreshed_at_cutoff": cutoff}

        if state.get("_local_context"):
            return None

        output = await self._arun_detect_script()
        if output:
            return {"_local_context": output}
        return None
    def _get_modified_request(self, request: ModelRequest) -> ModelRequest | None:
        state = cast("LocalContextState", request.state)
        local_context = state.get("_local_context", "")

        parts = [
            p for p in (local_context, self._tracing_context, self._mcp_context) if p
        ]
        if not parts:
            return None

        from langchain_core.messages import SystemMessage
        system_prompt = request.system_prompt or ""
        new_prompt = system_prompt + "\n\n" + "\n\n".join(parts)
        return request.override(system_message=SystemMessage(content=new_prompt))

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
