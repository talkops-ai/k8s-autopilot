"""
K8s Autopilot Agent — Default Configuration.

╔═══════════════════════════════════════════════════════════════════════════╗
║  THIS IS THE FILE YOU EDIT to change default values.                     ║
║  Every key here can be overridden by an environment variable of the      ║
║  same name (e.g.  export LLM_PROVIDER=anthropic) or by passing a dict   ║
║  to Config({"LLM_PROVIDER": "anthropic"}).                               ║
╚═══════════════════════════════════════════════════════════════════════════╝

Type annotations are required — they drive the automatic env-var type
coercion in ``config.py``.  If you add a new key, make sure to annotate it.
"""

from typing import Any, Dict, List, Optional

class DefaultConfig:
    """All default values for the K8s Autopilot Agent."""

    # ── LLM: Standard (fast, cost-effective) ──────────────────────────────
    LLM_PROVIDER: str = "openai"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_TOKENS: int = 15000

    # ── LLM: Higher-tier (complex reasoning) ─────────────────────────────
    LLM_HIGHER_PROVIDER: str = "openai"
    LLM_HIGHER_MODEL: str = "gpt-5-mini"
    LLM_HIGHER_TEMPERATURE: float = 0.0
    LLM_HIGHER_MAX_TOKENS: int = 15000

    # ── LLM: DeepAgent (multi-step agentic workflows) ────────────────────
    LLM_DEEPAGENT_PROVIDER: str = "openai"
    LLM_DEEPAGENT_MODEL: str = "o4-mini"
    LLM_DEEPAGENT_TEMPERATURE: float = 1.0
    LLM_DEEPAGENT_MAX_TOKENS: int = 25000

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "k8s_autopilot_agent.log"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"
    LOG_TO_CONSOLE: bool = True
    LOG_TO_FILE: bool = True
    LOG_STRUCTURED_JSON: bool = False

    # ── A2A Server ────────────────────────────────────────────────────────
    A2A_SERVER_HOST: str = "localhost"
    A2A_SERVER_PORT: int = 10102

    # ── LangGraph ─────────────────────────────────────────────────────────
    RECURSION_LIMIT: int = 50

    # ── Supervisor Context Engineering ────────────────────────────────
    # Controls the supervisor's context summarization middleware.
    # Industry standard: trigger at ~75% of effective context budget,
    # keep 4–6 messages (last 2–3 coordinator round-trips).
    SUPERVISOR_SUMMARIZATION_TRIGGER_TOKENS: int = 4000
    SUPERVISOR_SUMMARIZATION_KEEP_MESSAGES: int = 6
    SUPERVISOR_MODEL_CALL_LIMIT: int = 15

    # ── MCP Servers ─────────────────────────────────────────────────────────
    # Default transport is **stdio** for all TalkOps MCP servers (PyPI
    # binaries installed in the venv).  To fall back to HTTP transport
    # (e.g. when running MCP servers as separate containers), override
    # MCP_SERVERS via the environment variable with a JSON array containing
    # HTTP entries:
    #   {"name": "helm_mcp_server", "url": "http://host:9000/mcp",
    #    "transport": "http", ...}
    #
    # Stdio entry keys:
    #   name, command, transport="stdio", args, env (optional dict)
    # HTTP entry keys:
    #   name, url, transport="http", disabled, headers, auth_token_env_var
    MCP_SERVERS: List[Dict[str, Any]] = [
        # ── Remote HTTP (GitHub Copilot — always HTTP) ────────────────
        {
            "name": "github_mcp",
            "url": "https://api.githubcopilot.com/mcp/",
            "transport": "http",
            "disabled": False,
            "headers": {},
            "auth_token_env_var": "GITHUB_PERSONAL_ACCESS_TOKEN",
        },
        # ── Stdio: TalkOps MCP servers (PyPI binaries) ────────────────
        {
            "name": "helm_mcp_server",
            "command": "helm-mcp-server",
            "transport": "stdio",
            "args": [],
            "env": {"MCP_ALLOW_WRITE": "true"},
        },
        {
            "name": "argocd_mcp_server",
            "command": "argocd-mcp-server",
            "transport": "stdio",
            "args": [],
            "env": {"MCP_ALLOW_WRITE": "true"},
        },
        {
            "name": "traefik_mcp_server",
            "command": "traefik-mcp-server",
            "transport": "stdio",
            "args": [],
            "env": {},
        },
        {
            "name": "argo_rollout_mcp_server",
            "command": "argo-rollout-mcp-server",
            "transport": "stdio",
            "args": [],
            "env": {},
        },
        {
            "name": "prometheus-mcp-server",
            "command": "prometheus-mcp-server",
            "transport": "stdio",
            "args": [],
            "env": {},
        },
        {
            "name": "alertmanager-mcp-server",
            "command": "alertmanager-mcp-server",
            "transport": "stdio",
            "args": [],
            "env": {},
        },
        # ── Stdio: third-party (npx — unchanged) ─────────────────────
        {
            "name": "kubernetes_mcp_server",
            "command": "npx",
            "transport": "stdio",
            "args": ["-y", "kubernetes-mcp-server@latest"],
        },
    ]

    MCP_DEFAULT_HOST: str = "localhost"
    MCP_DEFAULT_TRANSPORT: str = "sse"
    MCP_TIMEOUT_TOTAL: float = 600.0    # 10 min
    MCP_TIMEOUT_CONNECT: float = 300.0   # 5 min
