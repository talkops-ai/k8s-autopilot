"""
Custom exceptions for K8s Autopilot Agent.

Comprehensive exception hierarchy for all K8s Autopilot Agent components including
agent operations, MCP client, GitHub integration, policy validation, and more.
"""

from typing import Optional, Any


# ============================================================================
# Base Exceptions
# ============================================================================

class K8sAutoPilotAgentError(Exception):
    """Base exception for all K8s Autopilot Agent errors."""
    pass


# ============================================================================
# Validation Exceptions
# ============================================================================

class ValidationError(K8sAutoPilotAgentError):
    """Raised for validation errors in messages or data."""
    
    def __init__(self, message: str, field: Optional[str] = None, value: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.value = value


class MessageFormatError(ValidationError):
    """Raised for invalid message format or structure."""
    
    def __init__(self, message: str, expected_format: str, received_format: str) -> None:
        super().__init__(message)
        self.expected_format = expected_format
        self.received_format = received_format


class JsonRpcValidationError(ValidationError):
    """Raised for JSON-RPC 2.0 specification violations."""
    
    def __init__(self, message: str, rpc_error_code: int, field: Optional[str] = None) -> None:
        super().__init__(message, field)
        self.rpc_error_code = rpc_error_code


class A2AProtocolError(ValidationError):
    """Raised for A2A protocol-specific violations."""
    
    def __init__(self, message: str, protocol_version: str, agent_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.protocol_version = protocol_version
        self.agent_id = agent_id


class StateValidationError(ValidationError):
    """Raised when CI pipeline state validation fails."""
    
    def __init__(self, message: str, state_field: Optional[str] = None, state_value: Any = None) -> None:
        super().__init__(message, state_field, state_value)


# ============================================================================
# Configuration Exceptions
# ============================================================================

class ConfigError(K8sAutoPilotAgentError):
    """Raised for configuration-related errors."""
    pass


class LLMConfigurationError(ConfigError):
    """Raised when LLM configuration is invalid."""
    pass


class UnsupportedProviderError(ConfigError):
    """Raised when an unsupported LLM provider is requested."""
    pass


class CIConfigurationError(ConfigError):
    """Raised when CI-specific configuration is invalid or missing."""
    
    def __init__(self, message: str, config_key: Optional[str] = None) -> None:
        super().__init__(message)
        self.config_key = config_key


# ============================================================================
# MCP Client Exceptions
# ============================================================================

class MCPClientError(K8sAutoPilotAgentError):
    """Exception raised for MCP client errors."""
    pass


class MCPConnectionError(MCPClientError):
    """Raised when MCP server connection fails."""
    
    def __init__(self, message: str, server_name: Optional[str] = None, url: Optional[str] = None) -> None:
        super().__init__(message)
        self.server_name = server_name
        self.url = url


class MCPToolExecutionError(MCPClientError):
    """Raised when MCP tool execution fails."""
    
    def __init__(self, message: str, tool_name: Optional[str] = None, server_name: Optional[str] = None) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.server_name = server_name


class MCPResourceError(MCPClientError):
    """Raised when MCP resource fetching fails."""
    
    def __init__(self, message: str, resource_uri: Optional[str] = None) -> None:
        super().__init__(message)
        self.resource_uri = resource_uri


# ============================================================================
# Context Detection Exceptions
# ============================================================================

class ContextDetectionError(K8sAutoPilotAgentError):
    """Raised when context detection fails."""
    pass


class RuntimeDetectionError(ContextDetectionError):
    """Raised when runtime detection fails."""
    
    def __init__(self, message: str, detected_files: Optional[list] = None) -> None:
        super().__init__(message)
        self.detected_files = detected_files


class DependencyExtractionError(ContextDetectionError):
    """Raised when dependency extraction fails."""
    
    def __init__(self, message: str, manifest_file: Optional[str] = None) -> None:
        super().__init__(message)
        self.manifest_file = manifest_file


# ============================================================================
# Approval/HITL Exceptions
# ============================================================================

class ApprovalError(K8sAutoPilotAgentError):
    """Base exception for approval/HITL errors."""
    pass


class ApprovalRejectedError(ApprovalError):
    """Raised when user rejects an approval request."""
    
    def __init__(self, message: str, rejection_reason: Optional[str] = None) -> None:
        super().__init__(message)
        self.rejection_reason = rejection_reason


class ApprovalTimeoutError(ApprovalError):
    """Raised when approval request times out."""
    
    def __init__(self, message: str, timeout_seconds: Optional[int] = None) -> None:
        super().__init__(message)
        self.timeout_seconds = timeout_seconds


# ============================================================================
# Agent Workflow Exceptions
# ============================================================================

class WorkflowError(K8sAutoPilotAgentError):
    """Base exception for agent workflow errors."""
    pass


class HandoffError(WorkflowError):
    """Raised when agent handoff fails."""
    
    def __init__(
        self, 
        message: str, 
        from_agent: Optional[str] = None, 
        to_agent: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.from_agent = from_agent
        self.to_agent = to_agent


class StateTransitionError(WorkflowError):
    """Raised when state transition fails."""
    
    def __init__(
        self, 
        message: str, 
        from_step: Optional[str] = None, 
        to_step: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.from_step = from_step
        self.to_step = to_step


class AgentExecutionError(WorkflowError):
    """Raised when agent execution fails."""
    
    def __init__(self, message: str, agent_name: Optional[str] = None, step: Optional[str] = None) -> None:
        super().__init__(message)
        self.agent_name = agent_name
        self.step = step


# ============================================================================
# A2UI Exceptions
# ============================================================================

class A2UIError(K8sAutoPilotAgentError):
    """Base exception for A2UI-related errors."""
    pass


class A2UIRenderError(A2UIError):
    """Raised when A2UI rendering fails."""
    
    def __init__(self, message: str, component_type: Optional[str] = None) -> None:
        super().__init__(message)
        self.component_type = component_type


class A2UIValidationError(A2UIError):
    """Raised when A2UI message validation fails."""
    
    def __init__(self, message: str, message_type: Optional[str] = None) -> None:
        super().__init__(message)
        self.message_type = message_type