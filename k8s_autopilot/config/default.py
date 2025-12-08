class DefaultConfig:
    """Default configuration for the AWS Orchestrator Agent."""
    # LLM Configuration
    LLM_PROVIDER: str = "openai"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_TOKENS: int = 15000
    
    # Higher LLM Configuration (for complex reasoning tasks)
    LLM_HIGHER_PROVIDER: str = "openai"
    LLM_HIGHER_MODEL: str = "gpt-5-mini"
    LLM_HIGHER_TEMPERATURE: float = 0.0
    LLM_HIGHER_MAX_TOKENS: int = 15000

    LLM_DEEPAGENT_PROVIDER: str = "openai"
    LLM_DEEPAGENT_MODEL: str = "gpt-4.1-mini"
    LLM_DEEPAGENT_TEMPERATURE: float = 0.0
    LLM_DEEPAGENT_MAX_TOKENS: int = 25000

    
    # Logging Configuration
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "k8s_autopilot_agent.log"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"
    LOG_TO_CONSOLE: bool = True
    LOG_TO_FILE: bool = True
    LOG_STRUCTURED_JSON: bool = False
    
    # MCP Server Configuration
    ARGOCD_MCP_SERVER_HOST: str = "localhost"
    ARGOCD_MCP_SERVER_PORT: int = 8000
    ARGOCD_MCP_SERVER_TRANSPORT: str = "sse"
    ARGOCD_MCP_SERVER_DISABLED: bool = False
    AGENTS_MCP_SERVER_AUTO_APPROVE: list = []
    
    # LangGraph Configuration
    RECURSION_LIMIT: int = 50  # Maximum recursion depth for agent workflows (default: 25, increased for multi-phase workflows)
    
