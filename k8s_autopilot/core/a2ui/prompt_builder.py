# Copyright 2025 K8s Autopilot
# Prompt builder for A2UI-enabled agent responses

from .schema import A2UI_SCHEMA
from .examples import K8S_UI_EXAMPLES

A2UI_RESPONSE_FORMAT_INSTRUCTIONS = """

--- A2UI RESPONSE FORMAT ---
When generating responses with UI, you MUST follow these rules:
1. Your response MUST be in TWO parts, separated by the delimiter: `---a2ui_JSON---`
2. The FIRST part is your conversational text response (Markdown supported)
3. The SECOND part is a single, raw JSON array of A2UI messages
4. The JSON part MUST validate against the A2UI JSON SCHEMA provided below

Example output format:
```
Here is the status of your Helm chart generation workflow...

---a2ui_JSON---
[
  {"beginRendering": {"surfaceId": "status", "root": "root-column", "styles": {"primaryColor": "#326CE5"}}},
  {"surfaceUpdate": {"surfaceId": "status", "components": [...]}},
  {"dataModelUpdate": {"surfaceId": "status", "path": "/", "contents": [...]}}
]
```

--- UI TEMPLATE SELECTION RULES ---
Choose the appropriate UI template based on the context:

1. **WORKFLOW_STATUS_EXAMPLE**: Use when showing progress of planning/generation/validation workflow
2. **HITL_APPROVAL_FORM_EXAMPLE**: Use when requesting human approval with Approve/Reject buttons
   - ALWAYS use this for `request_human_feedback` responses
   - ALWAYS use this for phase approval gates
3. **HELM_CHART_LIST_EXAMPLE**: Use when showing generated chart files
4. **VALIDATION_RESULTS_EXAMPLE**: Use when showing lint/security/template validation results
5. **HELM_RELEASE_TABLE_EXAMPLE**: Use when listing installed Helm releases
6. **COMPLETION_EXAMPLE**: Use when workflow is complete

--- BUTTON ACTIONS ---
For interactive buttons, use these action names that the client understands:
- `hitl_response`: For approval/rejection with context `{decision: "approved"|"rejected", phase: "<phase>"}`
- `download_chart`: For downloading chart artifacts
- `deploy_chart`: For deploying charts
- `upgrade_release`: For upgrading existing releases
- `uninstall_release`: For removing releases

"""


def get_a2ui_prompt(base_url: str = "") -> str:
    """
    Constructs the full A2UI prompt with instructions, examples, and schema.
    
    This prompt teaches the LLM how to generate valid A2UI JSON responses
    that will be rendered as rich UI components in the client.
    
    Args:
        base_url: Optional base URL for resolving static assets
        
    Returns:
        A formatted string to append to the agent's system prompt
    """
    # Format examples with base_url if needed
    formatted_examples = K8S_UI_EXAMPLES
    if base_url:
        formatted_examples = K8S_UI_EXAMPLES.format(base_url=base_url)
    
    return f"""
{A2UI_RESPONSE_FORMAT_INSTRUCTIONS}

{formatted_examples}

---BEGIN A2UI JSON SCHEMA---
{A2UI_SCHEMA}
---END A2UI JSON SCHEMA---
"""


def get_text_prompt() -> str:
    """
    Constructs the prompt for text-only responses (no UI).
    
    Use this when A2UI extension is not active.
    
    Returns:
        A formatted string for text-only agent responses
    """
    return """
--- TEXT RESPONSE FORMAT ---
You are a Kubernetes Helm chart assistant. Provide helpful, clear text responses.

For workflow updates, use clear Markdown formatting:
- Use headers (##, ###) to organize information
- Use bullet points for lists
- Use code blocks for YAML, commands, and file paths
- Use ✅ ❌ ⚠️ emojis for status indicators

For approval requests, clearly state:
1. What needs approval
2. The context/details
3. Ask the user to respond with "approve" or "reject"
"""


def parse_a2ui_response(content: str) -> tuple[str, list]:
    """
    Parse an LLM response that may contain A2UI JSON.
    
    Splits the response on the `---a2ui_JSON---` delimiter and returns
    the text part and parsed JSON messages separately.
    
    Args:
        content: The full LLM response string
        
    Returns:
        Tuple of (text_content, a2ui_messages_list)
        If no A2UI JSON found, returns (content, [])
    """
    import json
    
    if "---a2ui_JSON---" not in content:
        return content.strip(), []
    
    try:
        text_part, json_part = content.split("---a2ui_JSON---", 1)
        
        # Clean up the JSON part
        json_cleaned = json_part.strip()
        # Remove markdown code block markers if present
        if json_cleaned.startswith("```"):
            # Find the end of the opening marker
            first_newline = json_cleaned.find("\n")
            if first_newline != -1:
                json_cleaned = json_cleaned[first_newline + 1:]
        if json_cleaned.endswith("```"):
            json_cleaned = json_cleaned[:-3]
        json_cleaned = json_cleaned.strip()
        
        # Parse the JSON
        a2ui_messages = json.loads(json_cleaned)
        
        # Ensure it's a list
        if not isinstance(a2ui_messages, list):
            a2ui_messages = [a2ui_messages]
        
        return text_part.strip(), a2ui_messages
        
    except (json.JSONDecodeError, ValueError) as e:
        # If parsing fails, return original content with no UI
        import logging
        logging.getLogger(__name__).warning(f"Failed to parse A2UI JSON: {e}")
        return content.strip(), []


def validate_a2ui_messages(messages: list) -> tuple[bool, str]:
    """
    Validate A2UI messages against the schema.
    
    Args:
        messages: List of A2UI message dictionaries
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        import json
        import jsonschema
        
        # Parse the schema
        schema = json.loads(A2UI_SCHEMA)
        
        # Wrap schema for array validation
        array_schema = {"type": "array", "items": schema}
        
        # Validate
        jsonschema.validate(instance=messages, schema=array_schema)
        return True, ""
        
    except jsonschema.exceptions.ValidationError as e:
        return False, f"Schema validation failed: {e.message}"
    except json.JSONDecodeError as e:
        return False, f"Schema parse error: {e}"
    except ImportError:
        # jsonschema not installed, skip validation
        return True, ""
