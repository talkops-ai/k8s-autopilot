from abc import ABC, abstractmethod
from pydantic import BaseModel, Field, ConfigDict
from typing import Dict, List, Optional, Any, Union, Annotated, AsyncGenerator, Set
from enum import Enum
from datetime import datetime, timezone
import uuid

# LangGraph imports for proper message handling
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages

class AgentResponse(BaseModel):
    """
    Response from an agent during execution.
    
    This represents a single response item from the agent's stream,
    containing the content, metadata, and control flags.
    """
    
    model_config = ConfigDict(extra="allow")
    
    content: Any = Field(..., description="The response content (text or data)")
    response_type: str = Field(default="text", description="Type of response: 'text' or 'data'")
    is_task_complete: bool = Field(default=False, description="Whether this response indicates task completion")
    require_user_input: bool = Field(default=False, description="Whether this response requires user input to continue")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata about the response")
    root: Optional[Any] = Field(default=None, description="Root object for A2A protocol integration")


class BaseAgent(ABC):
    """
    Base interface for all AWS Orchestrator Agent implementations.
    
    This abstract base class defines the contract that all agent implementations
    must follow to work with the A2A protocol integration.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """
        Get the name of the agent.
        
        Returns:
            The agent's name
        """
        pass
    
    @abstractmethod
    async def stream(
        self, 
        query: str, 
        context_id: str, 
        task_id: str
    ) -> AsyncGenerator[AgentResponse, None]:
        """
        Stream responses for a given query.
        
        This method should implement the core agent logic and yield
        AgentResponse objects as the agent processes the query.
        
        Args:
            query: The user query to process
            context_id: The A2A context ID
            task_id: The A2A task ID
            
        Yields:
            AgentResponse objects representing the agent's progress
        """
        pass
    
    async def initialize(self) -> None:
        """
        Initialize the agent.
        
        This method can be overridden to perform any initialization
        required by the agent implementation.
        """
        pass
    
    async def cleanup(self) -> None:
        """
        Clean up resources used by the agent.
        
        This method can be overridden to perform any cleanup
        required by the agent implementation.
        """
        pass
