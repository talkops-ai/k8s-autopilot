"""
A2UI Builder Module

Programmatically builds A2UI JSON structures based on response context.
This replaces LLM-generated A2UI for reliability and token efficiency.
"""
from typing import Any, Dict, List, Optional
from a2ui import create_a2ui_part
from a2a.types import Part, TextPart


def build_working_status_a2ui(content: str, status: str = "working") -> List[dict]:
    """
    Build A2UI for working/processing status updates.
    
    Args:
        content: The text content to display
        status: Current status string
        
    Returns:
        List of A2UI message dicts
    """
    return [
        {
            "beginRendering": {
                "surfaceId": "status",
                "root": "status-root",
                "styles": {"primaryColor": "#818cf8", "font": "Inter"}
            }
        },
        {
            "surfaceUpdate": {
                "surfaceId": "status",
                "components": [
                    {
                        "id": "status-root",
                        "component": {
                            "Card": {"child": "status-content"}
                        }
                    },
                    {
                        "id": "status-content",
                        "component": {
                            "Column": {
                                "children": {"explicitList": ["status-header", "status-text"]}
                            }
                        }
                    },
                    {
                        "id": "status-header",
                        "component": {
                            "Row": {
                                "children": {"explicitList": ["status-icon", "status-title"]},
                                "alignment": "center"
                            }
                        }
                    },
                    {
                        "id": "status-icon",
                        "component": {
                            "Icon": {"name": {"literalString": "settings"}}
                        }
                    },
                    {
                        "id": "status-title",
                        "component": {
                            "Text": {
                                "usageHint": "h4",
                                "text": {"path": "title"}
                            }
                        }
                    },
                    {
                        "id": "status-text",
                        "component": {
                            "Text": {
                                "usageHint": "body",
                                "text": {"path": "content"}
                            }
                        }
                    }
                ]
            }
        },
        {
            "dataModelUpdate": {
                "surfaceId": "status",
                "path": "/",
                "contents": [
                    {"key": "title", "valueString": f"⏳ {status.replace('_', ' ').title()}"},
                    {"key": "content", "valueString": content}
                ]
            }
        }
    ]


def build_hitl_approval_a2ui(
    question: str,
    phase: str = "unknown",
    context: str = "",
    phase_id: str = "requirements"
) -> List[dict]:
    """
    Build A2UI for HITL approval/feedback requests with Approve/Reject buttons.
    
    Args:
        question: The question or content to show user
        phase: Current phase name for display
        context: Additional context
        phase_id: Phase identifier for button actions
        
    Returns:
        List of A2UI message dicts with interactive buttons
    """
    return [
        {
            "beginRendering": {
                "surfaceId": "hitl-form",
                "root": "approval-root",
                "styles": {"primaryColor": "#818cf8", "font": "Inter"}
            }
        },
        {
            "surfaceUpdate": {
                "surfaceId": "hitl-form",
                "components": [
                    {
                        "id": "approval-root",
                        "component": {"Card": {"child": "approval-content"}}
                    },
                    {
                        "id": "approval-content",
                        "component": {
                            "Column": {
                                "children": {
                                    "explicitList": [
                                        "approval-header",
                                        "divider1",
                                        "question-text",
                                        "context-text",
                                        "divider2",
                                        "action-row"
                                    ]
                                }
                            }
                        }
                    },
                    {
                        "id": "approval-header",
                        "component": {
                            "Row": {
                                "children": {"explicitList": ["header-icon", "header-title"]},
                                "alignment": "center"
                            }
                        }
                    },
                    {
                        "id": "header-icon",
                        "component": {"Icon": {"name": {"literalString": "help"}}}
                    },
                    {
                        "id": "header-title",
                        "component": {
                            "Text": {
                                "usageHint": "h3",
                                "text": {"path": "title"}
                            }
                        }
                    },
                    {"id": "divider1", "component": {"Divider": {}}},
                    {
                        "id": "question-text",
                        "component": {
                            "Text": {
                                "usageHint": "body",
                                "text": {"path": "question"}
                            }
                        }
                    },
                    {
                        "id": "context-text",
                        "component": {
                            "Text": {
                                "usageHint": "caption",
                                "text": {"path": "context"}
                            }
                        }
                    },
                    {"id": "divider2", "component": {"Divider": {}}},
                    {
                        "id": "action-row",
                        "component": {
                            "Row": {
                                "children": {"explicitList": ["reject-btn", "approve-btn"]},
                                "distribution": "spaceEvenly"
                            }
                        }
                    },
                    {
                        "id": "reject-btn",
                        "component": {
                            "Button": {
                                "child": "reject-text",
                                "primary": False,
                                "action": {
                                    "name": "hitl_response",
                                    "context": [
                                        {"key": "decision", "value": {"literalString": "rejected"}},
                                        {"key": "phase", "value": {"path": "phaseId"}}
                                    ]
                                }
                            }
                        }
                    },
                    {
                        "id": "reject-text",
                        "component": {
                            "Text": {"text": {"literalString": "❌ Reject"}}
                        }
                    },
                    {
                        "id": "approve-btn",
                        "component": {
                            "Button": {
                                "child": "approve-text",
                                "primary": True,
                                "action": {
                                    "name": "hitl_response",
                                    "context": [
                                        {"key": "decision", "value": {"literalString": "approved"}},
                                        {"key": "phase", "value": {"path": "phaseId"}}
                                    ]
                                }
                            }
                        }
                    },
                    {
                        "id": "approve-text",
                        "component": {
                            "Text": {"text": {"literalString": "✅ Approve"}}
                        }
                    }
                ]
            }
        },
        {
            "dataModelUpdate": {
                "surfaceId": "hitl-form",
                "path": "/",
                "contents": [
                    {"key": "title", "valueString": f"🔔 Input Required - {phase.replace('_', ' ').title()}"},
                    {"key": "question", "valueString": question},
                    {"key": "context", "valueString": context if context else f"Phase: {phase}"},
                    {"key": "phaseId", "valueString": phase_id}
                ]
            }
        }
    ]


def build_completion_a2ui(
    message: str,
    metrics: Optional[Dict[str, Any]] = None
) -> List[dict]:
    """
    Build A2UI for workflow/task completion.
    
    Args:
        message: Completion message
        metrics: Optional completion metrics
        
    Returns:
        List of A2UI message dicts
    """
    return [
        {
            "beginRendering": {
                "surfaceId": "completion",
                "root": "completion-root",
                "styles": {"primaryColor": "#22C55E", "font": "Inter"}
            }
        },
        {
            "surfaceUpdate": {
                "surfaceId": "completion",
                "components": [
                    {
                        "id": "completion-root",
                        "component": {"Card": {"child": "completion-content"}}
                    },
                    {
                        "id": "completion-content",
                        "component": {
                            "Column": {
                                "children": {"explicitList": ["success-header", "divider", "message-text"]}
                            }
                        }
                    },
                    {
                        "id": "success-header",
                        "component": {
                            "Row": {
                                "children": {"explicitList": ["check-icon", "success-title"]},
                                "alignment": "center"
                            }
                        }
                    },
                    {
                        "id": "check-icon",
                        "component": {"Icon": {"name": {"literalString": "check"}}}
                    },
                    {
                        "id": "success-title",
                        "component": {
                            "Text": {
                                "usageHint": "h2",
                                "text": {"literalString": "✅ Complete"}
                            }
                        }
                    },
                    {"id": "divider", "component": {"Divider": {}}},
                    {
                        "id": "message-text",
                        "component": {
                            "Text": {
                                "usageHint": "body",
                                "text": {"path": "message"}
                            }
                        }
                    }
                ]
            }
        },
        {
            "dataModelUpdate": {
                "surfaceId": "completion",
                "path": "/",
                "contents": [
                    {"key": "message", "valueString": message}
                ]
            }
        }
    ]


def build_error_a2ui(error_message: str) -> List[dict]:
    """
    Build A2UI for error display.
    
    Args:
        error_message: Error message to display
        
    Returns:
        List of A2UI message dicts
    """
    return [
        {
            "beginRendering": {
                "surfaceId": "error",
                "root": "error-root",
                "styles": {"primaryColor": "#EF4444", "font": "Inter"}
            }
        },
        {
            "surfaceUpdate": {
                "surfaceId": "error",
                "components": [
                    {
                        "id": "error-root",
                        "component": {"Card": {"child": "error-content"}}
                    },
                    {
                        "id": "error-content",
                        "component": {
                            "Column": {
                                "children": {"explicitList": ["error-header", "error-text"]}
                            }
                        }
                    },
                    {
                        "id": "error-header",
                        "component": {
                            "Row": {
                                "children": {"explicitList": ["error-icon", "error-title"]},
                                "alignment": "center"
                            }
                        }
                    },
                    {
                        "id": "error-icon",
                        "component": {"Icon": {"name": {"literalString": "error"}}}
                    },
                    {
                        "id": "error-title",
                        "component": {
                            "Text": {
                                "usageHint": "h3",
                                "text": {"literalString": "❌ Error"}
                            }
                        }
                    },
                    {
                        "id": "error-text",
                        "component": {
                            "Text": {
                                "usageHint": "body",
                                "text": {"path": "error"}
                            }
                        }
                    }
                ]
            }
        },
        {
            "dataModelUpdate": {
                "surfaceId": "error",
                "path": "/",
                "contents": [
                    {"key": "error", "valueString": error_message}
                ]
            }
        }
    ]


def build_info_message_a2ui(message: str, title: str = "Agent Response") -> List[dict]:
    """
    Build A2UI for informational/clarification messages (no approval buttons).
    
    Use this when the agent is providing information, asking for clarification,
    or explaining its capabilities - NOT for approval requests.
    
    Args:
        message: The informational message
        title: Optional title for the card
        
    Returns:
        List of A2UI message dicts
    """
    return [
        {
            "beginRendering": {
                "surfaceId": "info",
                "root": "info-root",
                "styles": {"primaryColor": "#818cf8", "font": "Inter"}
            }
        },
        {
            "surfaceUpdate": {
                "surfaceId": "info",
                "components": [
                    {
                        "id": "info-root",
                        "component": {"Card": {"child": "info-content"}}
                    },
                    {
                        "id": "info-content",
                        "component": {
                            "Column": {
                                "children": {"explicitList": ["info-header", "divider", "info-text"]}
                            }
                        }
                    },
                    {
                        "id": "info-header",
                        "component": {
                            "Row": {
                                "children": {"explicitList": ["info-icon", "info-title"]},
                                "alignment": "center"
                            }
                        }
                    },
                    {
                        "id": "info-icon",
                        "component": {"Icon": {"name": {"literalString": "info"}}}
                    },
                    {
                        "id": "info-title",
                        "component": {
                            "Text": {
                                "usageHint": "h3",
                                "text": {"path": "title"}
                            }
                        }
                    },
                    {"id": "divider", "component": {"Divider": {}}},
                    {
                        "id": "info-text",
                        "component": {
                            "Text": {
                                "usageHint": "body",
                                "text": {"path": "message"}
                            }
                        }
                    }
                ]
            }
        },
        {
            "dataModelUpdate": {
                "surfaceId": "info",
                "path": "/",
                "contents": [
                    {"key": "title", "valueString": title},
                    {"key": "message", "valueString": message}
                ]
            }
        }
    ]


def _is_approval_request(content: Any, metadata: Dict[str, Any]) -> bool:
    """
    Detect if the HITL request is an approval request or just informational.
    
    Approval requests typically:
    - Come from HITL gates (planning_review, generation_review)
    - Contain keywords like "approve", "review", "confirm"
    - Have specific interrupt types
    
    Args:
        content: The response content
        metadata: Response metadata
        
    Returns:
        True if this is an approval request, False for informational
    """
    # Check interrupt type from metadata
    interrupt_type = metadata.get('interrupt_type', '')
    if interrupt_type in ('hitl_gate', 'planning_review', 'generation_review', 'tool_result_review'):
        return True
    
    # Check for approval-related keywords in content
    content_str = str(content).lower() if content else ""
    approval_keywords = [
        'approve', 'reject', 'confirm', 'review plan', 'review the',
        'proceed with', 'should i continue', 'ready to proceed',
        'please review', 'awaiting approval'
    ]
    
    for keyword in approval_keywords:
        if keyword in content_str:
            return True
    
    # Check for informational patterns (redirects, capability explanations)
    info_keywords = [
        'i specialize in', 'how can i help', 'i am designed for',
        'my capabilities', 'i can help with', 'what would you like',
        'please provide', 'could you clarify', 'more information'
    ]
    
    for keyword in info_keywords:
        if keyword in content_str:
            return False
    
    # Default to informational (safer - no unexpected buttons)
    return False


def build_a2ui_for_response(
    content: Any,
    is_task_complete: bool = False,
    require_user_input: bool = False,
    response_type: str = "text",
    metadata: Optional[Dict[str, Any]] = None
) -> List[Part]:
    """
    Main routing function - builds appropriate A2UI based on response context.
    
    Args:
        content: Response content
        is_task_complete: Whether task is complete
        require_user_input: Whether user input is required
        response_type: Type of response ('text', 'data', 'error', 'human_input')
        metadata: Response metadata with status and other context
        
    Returns:
        List of A2UI Parts ready for A2A protocol
    """
    metadata = metadata or {}
    status = metadata.get('status', 'working')
    
    # Convert content to string if needed
    if isinstance(content, dict):
        content_str = content.get('message', str(content))
    else:
        content_str = str(content) if content else "Processing..."
    
    # Route to appropriate builder based on context
    if response_type == 'error':
        a2ui_messages = build_error_a2ui(content_str)
    
    elif require_user_input:
        # HITL scenarios - determine if approval request or informational
        if isinstance(content, dict):
            question = content.get('question', content_str)
            phase = content.get('phase', metadata.get('phase', 'unknown'))
            context = content.get('context', '')
        else:
            question = content_str
            phase = metadata.get('phase', 'unknown')
            context = ''
        
        # Check if this is an approval request or just an informational message
        if _is_approval_request(question, metadata):
            # True approval request - show Approve/Reject buttons
            a2ui_messages = build_hitl_approval_a2ui(
                question=question,
                phase=phase,
                context=context if context else "No additional context provided",
                phase_id=phase
            )
        else:
            # Informational message - no buttons, just display the info
            a2ui_messages = build_info_message_a2ui(
                message=question,
                title="Clarification Needed"
            )
    
    elif is_task_complete:
        # Completion scenarios
        a2ui_messages = build_completion_a2ui(
            message=content_str,
            metrics=content if isinstance(content, dict) else None
        )
    
    else:
        # Working/processing status
        a2ui_messages = build_working_status_a2ui(
            content=content_str,
            status=status
        )
    
    # Convert to A2UI Parts
    return [create_a2ui_part(msg) for msg in a2ui_messages]
