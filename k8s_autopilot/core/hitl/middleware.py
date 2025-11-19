"""
HITL middleware implementations.

This module provides middleware for tool-level approvals using LangChain's
HumanInTheLoopMiddleware. This enables automatic interrupts on sensitive tool calls.
"""

from typing import Dict, Any, List, Optional, Literal
from langchain.agents import create_agent
from langchain_core.tools import BaseTool

from k8s_autopilot.core.hitl.checkpointer import create_checkpointer, get_checkpointer
from k8s_autopilot.config.config import Config
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for HITL middleware
hitl_logger = AgentLogger("k8sAutopilotHITLMiddleware")

# Optional HumanInTheLoopMiddleware import
try:
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    HITL_MIDDLEWARE_AVAILABLE = True
except ImportError:
    HumanInTheLoopMiddleware = None  # type: ignore
    HITL_MIDDLEWARE_AVAILABLE = False
    hitl_logger.log_structured(
        level="WARNING",
        message="HumanInTheLoopMiddleware not available. Tool-level HITL will be disabled.",
        extra={"fallback": "custom_gates_only"}
    )


# Default tool interrupt configurations
DEFAULT_HITL_TOOLS = {
    "deploy_to_cluster": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "🚨 Cluster deployment requires approval"
    },
    "create_argocd_application": {
        "allowed_decisions": ["approve", "reject"],
        "description": "🚨 ArgoCD configuration requires approval"
    },
    "apply_kubernetes_manifest": {
        "allowed_decisions": ["approve", "edit", "reject"],
        "description": "🚨 Kubernetes resource creation requires approval"
    },
    "delete_kubernetes_resource": {
        "allowed_decisions": ["approve", "reject"],
        "description": "🚨 Kubernetes resource deletion requires approval"
    }
}


def create_hitl_middleware_config(
    tools: Optional[Dict[str, Dict[str, Any]]] = None,
    description_prefix: str = "Action pending review"
) -> Optional[Any]:
    """
    Create HITL middleware configuration.
    
    Args:
        tools: Dictionary mapping tool names to interrupt configurations.
               Each config should have:
               - "allowed_decisions": List of allowed decisions (e.g., ["approve", "reject"])
               - "description": Description shown to reviewer
        description_prefix: Prefix for interrupt descriptions
        
    Returns:
        HumanInTheLoopMiddleware instance or None if not available
        
    Example:
        config = create_hitl_middleware_config({
            "deploy_to_cluster": {
                "allowed_decisions": ["approve", "edit", "reject"],
                "description": "🚨 Deployment requires approval"
            }
        })
    """
    if not HITL_MIDDLEWARE_AVAILABLE or HumanInTheLoopMiddleware is None:
        hitl_logger.log_structured(
            level="WARNING",
            message="HumanInTheLoopMiddleware not available, returning None",
            extra={"fallback": "custom_gates_only"}
        )
        return None
    
    # Use provided tools or default
    interrupt_config = tools or DEFAULT_HITL_TOOLS
    
    try:
        middleware = HumanInTheLoopMiddleware(
            interrupt_on=interrupt_config,
            description_prefix=description_prefix
        )
        
        hitl_logger.log_structured(
            level="INFO",
            message="Created HITL middleware configuration",
            extra={
                "tool_count": len(interrupt_config),
                "tools": list(interrupt_config.keys())
            }
        )
        
        return middleware
        
    except Exception as e:
        hitl_logger.log_structured(
            level="ERROR",
            message=f"Failed to create HITL middleware: {e}",
            extra={"error": str(e), "error_type": type(e).__name__}
        )
        return None


def get_tool_names(tools: List[BaseTool]) -> List[str]:
    """
    Extract tool names from a list of BaseTool instances.
    
    Args:
        tools: List of tool instances
        
    Returns:
        List of tool names
    """
    tool_names = []
    for tool in tools:
        if hasattr(tool, "name"):
            tool_names.append(tool.name)
        elif hasattr(tool, "__name__"):
            tool_names.append(tool.__name__)
        else:
            tool_names.append(str(tool))
    
    return tool_names


def create_supervisor_with_hitl(
    model: Any,
    tools: List[BaseTool],
    hitl_tools: Optional[Dict[str, Dict[str, Any]]] = None,
    checkpointer_type: Literal["postgres", "memory"] = "memory",
    config: Optional[Config] = None,
    description_prefix: str = "Action pending review",
    **agent_kwargs
) -> Any:
    """
    Creates main supervisor with HITL middleware for tool-level approvals.
    
    This function wraps create_agent() with HITL middleware that automatically
    interrupts on sensitive tool calls, requiring human approval before execution.
    
    Args:
        model: LLM model instance (e.g., from LLMProvider)
        tools: List of tools available to the agent
        hitl_tools: Optional dictionary mapping tool names to interrupt configs.
                   If None, uses DEFAULT_HITL_TOOLS
        checkpointer_type: Type of checkpointer ("postgres" or "memory")
        config: Optional Config instance
        description_prefix: Prefix for interrupt descriptions
        **agent_kwargs: Additional arguments passed to create_agent()
        
    Returns:
        Agent instance with HITL middleware configured
        
    Example:
        from k8s_autopilot.core.llm.llm_provider import LLMProvider
        from k8s_autopilot.tools.deployment import deploy_to_cluster_tool
        
        model = LLMProvider.create_llm(...)
        agent = create_supervisor_with_hitl(
            model=model,
            tools=[deploy_to_cluster_tool],
            hitl_tools={
                "deploy_to_cluster": {
                    "allowed_decisions": ["approve", "edit", "reject"],
                    "description": "🚨 Deployment requires approval"
                }
            }
        )
    """
    # Get checkpointer (required for HITL)
    checkpointer = create_checkpointer(
        checkpointer_type=checkpointer_type,
        config=config
    )
    
    # Create HITL middleware
    middleware_config = create_hitl_middleware_config(
        tools=hitl_tools,
        description_prefix=description_prefix
    )
    
    # Prepare middleware list
    middleware_list = []
    if middleware_config:
        middleware_list.append(middleware_config)
    
    # Get tool names for logging
    tool_names = get_tool_names(tools)
    
    hitl_logger.log_structured(
        level="INFO",
        message="Creating supervisor agent with HITL middleware",
        extra={
            "tool_count": len(tools),
            "tool_names": tool_names,
            "hitl_enabled": middleware_config is not None,
            "checkpointer_type": checkpointer_type
        }
    )
    
    # Create agent with middleware
    try:
        agent = create_agent(
            model=model,
            tools=tools,
            middleware=middleware_list if middleware_list else None,
            checkpointer=checkpointer,
            **agent_kwargs
        )
        
        hitl_logger.log_structured(
            level="INFO",
            message="Successfully created supervisor agent with HITL",
            extra={
                "has_middleware": middleware_config is not None,
                "has_checkpointer": checkpointer is not None
            }
        )
        
        return agent
        
    except Exception as e:
        hitl_logger.log_structured(
            level="ERROR",
            message=f"Failed to create agent with HITL middleware: {e}",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "fallback": "creating_agent_without_middleware"
            }
        )
        # Fallback: create agent without middleware
        return create_agent(
            model=model,
            tools=tools,
            checkpointer=checkpointer,
            **agent_kwargs
        )


def add_hitl_to_existing_agent(
    agent: Any,
    hitl_tools: Optional[Dict[str, Dict[str, Any]]] = None,
    description_prefix: str = "Action pending review"
) -> Any:
    """
    Add HITL middleware to an existing agent.
    
    Note: This may not work for all agent types. It's better to use
    create_supervisor_with_hitl() when creating new agents.
    
    Args:
        agent: Existing agent instance
        hitl_tools: Optional dictionary mapping tool names to interrupt configs
        description_prefix: Prefix for interrupt descriptions
        
    Returns:
        Agent with HITL middleware added (or original agent if not possible)
    """
    if not HITL_MIDDLEWARE_AVAILABLE:
        hitl_logger.log_structured(
            level="WARNING",
            message="Cannot add HITL middleware - not available",
            extra={"returning_original": True}
        )
        return agent
    
    middleware_config = create_hitl_middleware_config(
        tools=hitl_tools,
        description_prefix=description_prefix
    )
    
    if middleware_config and hasattr(agent, "middleware"):
        # Try to add middleware to existing agent
        if isinstance(agent.middleware, list):
            agent.middleware.append(middleware_config)
        else:
            agent.middleware = [middleware_config]
        
        hitl_logger.log_structured(
            level="INFO",
            message="Added HITL middleware to existing agent",
            extra={"has_middleware": True}
        )
    
    return agent


def is_hitl_middleware_available() -> bool:
    """
    Check if HITL middleware is available.
    
    Returns:
        True if HumanInTheLoopMiddleware can be imported, False otherwise
    """
    return HITL_MIDDLEWARE_AVAILABLE
