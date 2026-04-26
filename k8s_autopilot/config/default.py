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

    # ── MCP Servers (Helm & ArgoCD) ───────────────────────────────────────
    # Each entry: {name, url, transport, disabled, headers, auth_token_env_var}
    MCP_SERVERS: List[Dict[str, Any]] = [
        {
            "name": "github_mcp",
            "url": "https://api.githubcopilot.com/mcp/",
            "transport": "http",
            "disabled": False,
            "headers": {},
            "auth_token_env_var": "GITHUB_PERSONAL_ACCESS_TOKEN",
        },
        {
            "name": "helm_mcp_server",
            "url": "http://localhost:9000/mcp",
            "transport": "http",
            "disabled": False,
            "headers": {},
            "auth_token_env_var": None,
        },
        {
            "name": "argocd_mcp_server",
            "url": "http://localhost:8770/mcp",
            "transport": "http",
            "disabled": False,
            "headers": {},
            "auth_token_env_var": None,
        },
        {
            "name": "traefik_mcp_server",
            "url": "http://localhost:8769/mcp",
            "transport": "http",
            "disabled": False,
            "headers": {},
            "auth_token_env_var": None,
        },
        {
            "name": "argo_rollout_mcp_server",
            "url": "http://localhost:8768/mcp",
            "transport": "http",
            "disabled": False,
            "headers": {},
            "auth_token_env_var": None,
        },
        {
            "name": "kubernetes_mcp_server",
            "url": "http://localhost:8003/mcp",
            "transport": "http",
            "disabled": False,
            "headers": {},
            "auth_token_env_var": None,
        }
    ]

    MCP_DEFAULT_HOST: str = "localhost"
    MCP_DEFAULT_TRANSPORT: str = "sse"
    MCP_TIMEOUT_TOTAL: float = 600.0    # 10 min
    MCP_TIMEOUT_CONNECT: float = 300.0   # 5 min
