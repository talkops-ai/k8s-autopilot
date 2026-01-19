"""
ArgoCD Onboarding Agent Middleware

This module provides middleware classes for the ArgoCD Onboarding Agent following
the pattern established in helm_mgmt_agent.py:

1. ArgoCDStateMiddleware - Updates state based on tool outputs
2. ArgoCDApprovalHITLMiddleware - HITL for critical operations
3. ArgoCDErrorRecoveryMiddleware - Retry logic for transient errors

Architecture References:
- docs/app_onboarding/K8s-Autopilot-Deep-Agent-Architecture.md
- k8s_autopilot/core/agents/helm_mgmt/helm_mgmt_agent.py
"""

import json
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Callable, Awaitable, List, Set
from langgraph.types import Command, interrupt
from langchain_core.messages import ToolMessage, BaseMessage
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest

from k8s_autopilot.utils.logger import AgentLogger

# Agent logger
argocd_agent_logger = AgentLogger("k8sAutopilotArgoCDOnboardingAgent")


# ============================================================================
# State Update Handlers - Separated by Concern
# ============================================================================

def _update_project_state(tool_name: str, tool_args: Dict[str, Any], content: Any) -> Dict[str, Any]:
    """Update state for project-related tools."""
    import json
    updates = {}
    
    try:
        if isinstance(content, str):
            data = json.loads(content)
        else:
            data = content
    except json.JSONDecodeError as e:
        # Issue #8: Log parsing failure instead of silent exception
        argocd_agent_logger.log_structured(
            level="WARNING",
            message=f"Non-JSON tool output for {tool_name}: {e}",
            extra={"content_preview": str(content)[:100]}
        )
        # Fallback: store raw content
        data = {"raw": str(content)}
    
    try:
        if tool_name == "create_project":
            project_name = tool_args.get("project_name") or tool_args.get("name")
            updates["project_info"] = {
                "name": project_name,
                "description": tool_args.get("description"),
                "details": data
            }
            updates["project_created"] = True
            updates["project_list"] = [{"name": project_name, "created": True}]
            
        elif tool_name == "get_project":
            updates["project_info"] = data
            updates["project_validation_result"] = {"validated": True, "data": data}
            
        elif tool_name == "list_projects":
            # MCP server returns an envelope: {"projects": [...], ...}
            if isinstance(data, dict) and isinstance(data.get("projects"), list):
                updates["project_list"] = data.get("projects") or []
            elif isinstance(data, list):
                updates["project_list"] = data
            else:
                updates["project_list"] = [data]
            
        elif tool_name == "delete_project":
            project_name = tool_args.get("project_name") or tool_args.get("name")
            updates["project_info"] = {"name": project_name, "deleted": True}
            updates["project_created"] = False
            
    except Exception:
        pass
    
    return updates


def _update_repo_state(tool_name: str, tool_args: Dict[str, Any], content: Any) -> Dict[str, Any]:
    """Update state for repository-related tools."""
    import json
    updates = {}
    
    try:
        if isinstance(content, str):
            data = json.loads(content)
        else:
            data = content
    except json.JSONDecodeError as e:
        argocd_agent_logger.log_structured(
            level="WARNING",
            message=f"Non-JSON tool output for {tool_name}: {e}",
            extra={"content_preview": str(content)[:100]}
        )
        data = {"raw": str(content)}
    
    try:
        if tool_name == "validate_repository_connection":
            updates["repository_info"] = {
                "url": tool_args.get("repo_url"),
                "validated": True,
                "accessible": data.get("accessible", False) if isinstance(data, dict) else False,
            }
            updates["repo_validation_result"] = {
                "url": tool_args.get("repo_url"),
                "accessible": data.get("accessible", False) if isinstance(data, dict) else False,
                "details": data
            }
            
        elif tool_name in ["onboard_repository_https", "onboard_repository_ssh"]:
            updates["repository_info"] = {
                "url": tool_args.get("repo_url"),
                "auth_type": "https" if "https" in tool_name else "ssh",
                "project": tool_args.get("project"),
                "details": data
            }
            updates["repo_onboarded"] = True
            updates["repository_list"] = [{"url": tool_args.get("repo_url"), "onboarded": True}]
            
        elif tool_name == "list_repositories":
            # MCP server returns an envelope: {"repositories": [...], ...}
            if isinstance(data, dict) and isinstance(data.get("repositories"), list):
                updates["repository_list"] = data.get("repositories") or []
            elif isinstance(data, list):
                updates["repository_list"] = data
            else:
                updates["repository_list"] = [data]

        elif tool_name == "get_repository":
            # MCP server returns a single repo detail object
            updates["repository_info"] = data if isinstance(data, dict) else {"raw": data}
            
        elif tool_name == "delete_repository":
            updates["repository_info"] = {"url": tool_args.get("repo_url"), "deleted": True}
            updates["repo_onboarded"] = False
            
    except Exception as e:
        argocd_agent_logger.log_structured(
            level="ERROR",
            message=f"State update failed for {tool_name}: {e}"
        )
    
    return updates


def _update_app_state(tool_name: str, tool_args: Dict[str, Any], content: Any) -> Dict[str, Any]:
    """Update state for application-related tools."""
    import json
    updates = {}
    
    try:
        if isinstance(content, str):
            data = json.loads(content)
        else:
            data = content
    except json.JSONDecodeError as e:
        argocd_agent_logger.log_structured(
            level="WARNING",
            message=f"Non-JSON tool output for {tool_name}: {e}",
            extra={"content_preview": str(content)[:100]}
        )
        data = {"raw": str(content)}
    
    try:
        app_name = tool_args.get("name") or tool_args.get("app_name")
        
        if tool_name == "create_application":
            updates["application_info"] = {
                "name": app_name,
                "project": tool_args.get("project"),
                "namespace": tool_args.get("destination_namespace") or tool_args.get("dest_namespace"),
            }
            updates["application_created"] = True
            updates["application_details"] = data
            updates["application_list"] = [{"name": app_name, "created": True}]
            
        elif tool_name == "sync_application":
            updates["sync_operation_id"] = data.get("operation_id") if isinstance(data, dict) else None
            updates["sync_status"] = "synced" if (isinstance(data, dict) and data.get("success")) else "out_of_sync"
            updates["deployment_status"] = data
            
        elif tool_name == "get_application_status":
            updates["application_details"] = data
            updates["health_report"] = data.get("health") if isinstance(data, dict) else None
            updates["sync_status"] = data.get("sync", {}).get("status") if isinstance(data, dict) else None
            
        elif tool_name == "get_application_details":
            updates["application_details"] = data
            updates["application_info"] = data if isinstance(data, dict) else {"raw": data}
            
        elif tool_name == "delete_application":
            updates["application_info"] = {"name": app_name, "deleted": True}
            updates["application_created"] = False

        elif tool_name == "list_applications":
            # MCP server returns an envelope: {"applications": [...], ...}
            if isinstance(data, dict) and isinstance(data.get("applications"), list):
                updates["application_list"] = data.get("applications") or []
            elif isinstance(data, list):
                updates["application_list"] = data
            else:
                updates["application_list"] = [data]

        elif tool_name == "get_application_diff":
            # Store as a query-style result; shape varies by MCP server implementation
            updates["query_results"] = [{
                "type": "application_diff",
                "app_name": app_name,
                "data": data
            }]

        elif tool_name == "get_sync_status":
            updates["query_results"] = [{
                "type": "sync_status",
                "app_name": app_name,
                "data": data
            }]
            
    except Exception as e:
        argocd_agent_logger.log_structured(
            level="ERROR",
            message=f"State update failed for {tool_name}: {e}"
        )
    
    return updates


def _update_debug_state(tool_name: str, tool_args: Dict[str, Any], content: Any) -> Dict[str, Any]:
    """Update state for debug-related tools."""
    import json
    updates = {}
    
    try:
        if isinstance(content, str):
            data = json.loads(content)
        else:
            data = content
    except json.JSONDecodeError as e:
        argocd_agent_logger.log_structured(
            level="WARNING",
            message=f"Non-JSON tool output for {tool_name}: {e}",
            extra={"content_preview": str(content)[:100]}
        )
        data = {"raw": str(content)}
    
    try:
        app_name = tool_args.get("app_name")
        
        updates["debug_results"] = {
            "app_name": app_name,
            "tool": tool_name,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Update specific debug result fields
        if tool_name == "get_application_logs":
            updates["logs_collected"] = [str(data)]
        elif tool_name == "get_application_events":
            updates["events_collected"] = data if isinstance(data, list) else [data]
        elif tool_name == "get_pod_metrics":
            updates["metrics_collected"] = data
        elif tool_name == "get_argocd_status":
            updates["health_report"] = data
        
    except Exception as e:
        argocd_agent_logger.log_structured(
            level="ERROR",
            message=f"State update failed for {tool_name}: {e}"
        )
    
    return updates



# ============================================================================
# Tool Configuration Registry - Data-Driven Approach
# ============================================================================

class ToolConfig:
    """Configuration for tool behavior in middleware."""
    
    def __init__(
        self,
        duplicate_check_keys: tuple[str, ...],
        state_updater: Optional[Callable[[str, Dict[str, Any], Any], Dict[str, Any]]] = None,
        requires_approval: bool = False,
        is_critical: bool = False,
    ):
        self.duplicate_check_keys = duplicate_check_keys
        self.state_updater = state_updater
        self.requires_approval = requires_approval
        self.is_critical = is_critical


class ArgoCDToolRegistry:
    """Registry for ArgoCD tool configurations."""
    
    def __init__(self):
        self._configs: Dict[str, ToolConfig] = {}
        self._register_defaults()
    
    def register(self, tool_name: str, config: ToolConfig):
        """Register a tool configuration."""
        self._configs[tool_name] = config
    
    def get(self, tool_name: str) -> Optional[ToolConfig]:
        """Get configuration for a tool."""
        return self._configs.get(tool_name)
    
    def has_duplicate_check(self, tool_name: str) -> bool:
        """Check if tool has duplicate detection configured."""
        return tool_name in self._configs and self._configs[tool_name].duplicate_check_keys
    
    def _register_defaults(self):
        """Register default tool configurations."""
        # Project tools
        self.register("create_project", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_project_state,
            requires_approval=False,
        ))
        self.register("get_project", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_project_state,
        ))
        self.register("update_project", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_project_state,
        ))
        self.register("delete_project", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_project_state,
            requires_approval=True,
            is_critical=True,
        ))
        self.register("list_projects", ToolConfig(
            duplicate_check_keys=(),
            state_updater=_update_project_state,
        ))
        
        # Repository tools
        self.register("validate_repository_connection", ToolConfig(
            duplicate_check_keys=("repo_url",),
            state_updater=_update_repo_state,
        ))
        self.register("onboard_repository_https", ToolConfig(
            duplicate_check_keys=("repo_url", "project"),
            state_updater=_update_repo_state,
        ))
        self.register("onboard_repository_ssh", ToolConfig(
            duplicate_check_keys=("repo_url", "project"),
            state_updater=_update_repo_state,
        ))
        self.register("list_repositories", ToolConfig(
            duplicate_check_keys=(),
            state_updater=_update_repo_state,
        ))
        self.register("delete_repository", ToolConfig(
            duplicate_check_keys=("repo_url",),
            state_updater=_update_repo_state,
            requires_approval=True,
        ))
        
        # Application tools
        self.register("create_application", ToolConfig(
            duplicate_check_keys=("name", "project"),
            state_updater=_update_app_state,
        ))
        self.register("list_applications", ToolConfig(
            duplicate_check_keys=(),
            state_updater=_update_app_state,
        ))
        self.register("get_application_details", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_app_state,
        ))
        self.register("update_application", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_app_state,
        ))
        self.register("delete_application", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_app_state,
            requires_approval=True,
            is_critical=True,
        ))
        self.register("sync_application", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_app_state,
            requires_approval=True,  # Syncing can be risky
        ))
        self.register("get_application_diff", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_app_state,
        ))
        self.register("get_sync_status", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_app_state,
        ))
        self.register("get_application_status", ToolConfig(
            duplicate_check_keys=("name",),
            state_updater=_update_app_state,
        ))
        
        # Debug tools (no approval needed, read-only)
        for tool_name in ["get_application_logs", "get_application_events", 
                          "get_pod_metrics", "get_argocd_status", 
                          "get_resource_tree", "analyze_error"]:
            self.register(tool_name, ToolConfig(
                duplicate_check_keys=("app_name",),
                state_updater=_update_debug_state,
            ))


# Global registry instance
_ARGOCD_TOOL_REGISTRY = ArgoCDToolRegistry()


# ============================================================================
# Middleware Classes
# ============================================================================

class ArgoCDStateMiddleware(AgentMiddleware):
    """
    Middleware to intercept tool outputs and update ArgoCDOnboardingState.
    
    This bridges the gap between MCP tools (which return strings/JSON)
    and the Agent's structured state (project_info, application_info, etc.)
    """
    
    def __init__(self):
        self.registry = _ARGOCD_TOOL_REGISTRY
    
    def _get_state_updates(
        self, 
        tool_name: str, 
        tool_args: Dict[str, Any], 
        response: ToolMessage
    ) -> Dict[str, Any]:
        """Get state updates for tool response using registry configuration."""
        config = self.registry.get(tool_name)
        if not config or not config.state_updater:
            return {}
        
        content = response.content
        return config.state_updater(tool_name, tool_args, content) or {}
    
    def _build_dup_key(self, tool_name: str, tool_args: Dict[str, Any], check_keys: tuple) -> str:
        """Build a unique key for duplicate detection based on configured keys."""
        key_parts = [tool_name]
        for key in check_keys:
            value = tool_args.get(key, "")
            key_parts.append(f"{key}:{value}")
        return "|".join(key_parts)



    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Intercept tool calls to update state based on results."""
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})
        
        # Issue #7: De-dup logic - check for duplicate tool calls using state
        config = self.registry.get(tool_name)
        dup_key = None
        if config and config.duplicate_check_keys:
            dup_key = self._build_dup_key(tool_name, tool_args, config.duplicate_check_keys)
            # Check state for seen tool calls (passed in request.state or via context)
            state = getattr(request, 'state', None) or {}
            seen_calls = state.get("_seen_tool_calls", [])
            if dup_key in seen_calls:
                argocd_agent_logger.log_structured(
                    level="INFO",
                    message=f"Skipping duplicate tool call: {tool_name}",
                    extra={"dup_key": dup_key}
                )
                # Return cached/minimal response for duplicate
                return ToolMessage(
                    content=f"Tool {tool_name} was already called with these parameters. Using cached result.",
                    tool_call_id=request.tool_call.get("id", "")
                )
        
        # Execute the tool
        response = await handler(request)
        
        # Process response only if it's a ToolMessage
        if not isinstance(response, ToolMessage):
            return response
        
        # Determine success status
        is_success = "error" not in str(response.content).lower()
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # Get state updates using registry configuration
        updates = self._get_state_updates(tool_name, tool_args, response)

        # Record this tool call in seen_tool_calls for de-dup (only on success)
        if dup_key and is_success:
            updates["_seen_tool_calls"] = [dup_key]
        
        # Add execution log entry
        log_entry = {
            "timestamp": timestamp,
            "tool": tool_name,
            "args": tool_args,
            "status": "success" if is_success else "failed"
        }
        updates["execution_logs"] = [log_entry]
        
        # Add audit log entry (Issue #5)
        audit_entry = {
            "timestamp": timestamp,
            "action": f"tool_call:{tool_name}",
            "tool_name": tool_name,
            "tool_args": {k: v for k, v in tool_args.items() if k not in ["password", "ssh_private_key", "token"]},  # Exclude secrets
            "success": is_success,
            "content_preview": str(response.content)[:200] if response.content else None,
        }
        updates["audit_log"] = [audit_entry]
        
        # Apply state updates if any
        if updates:
            return Command(update={**updates, "messages": [response]})
        
        return response


class ArgoCDMissingInputsHITLMiddleware(AgentMiddleware):
    """
    Deterministic HITL for missing required tool inputs.

    This avoids brittle text heuristics by triggering ONLY when a tool call is about
    to execute without required arguments. It pauses with `interrupt(...)`, collects
    user-provided values, injects them into the tool call args, then continues.
    """

    # NOTE: Tool argument names are defined by the ArgoCD MCP server.
    # This project also carries some legacy aliases in prompts/tool calls, so we accept
    # common synonyms to avoid unnecessary HITL interruptions.
    REQUIRED_ARGS: Dict[str, List[str]] = {
        # Application lifecycle (per docs/app_onboarding/argo_mcp_tools.md)
        "create_application": ["cluster_name", "app_name", "project", "repo_url", "path", "destination_namespace"],
        "sync_application": ["cluster_name", "app_name"],
        "delete_application": ["cluster_name", "app_name"],

        # Project lifecycle (per docs/app_onboarding/argo_mcp_tools.md)
        "create_project": ["project_name"],
        "delete_project": ["project_name"],

        "onboard_repository_https": ["repo_url"],
        "onboard_repository_ssh": ["repo_url"],
    }

    FRIENDLY_FIELD_NAMES: Dict[str, str] = {
        "name": "name",
        "app_name": "application name",
        "project": "ArgoCD project",
        "repo_url": "repository URL",
        "path": "repo path (chart path)",
        "destination_namespace": "destination namespace",
        "dest_namespace": "destination namespace",
        "cluster_name": "target cluster name",
        "destination_server": "destination server URL",
        "project_name": "ArgoCD project name",
    }

    # Per-tool aliasing: treat these arg names as equivalent for “required” checks.
    REQUIRED_ALIASES: Dict[str, Dict[str, List[str]]] = {
        "create_application": {
            "cluster_name": ["cluster_name", "dest_cluster"],
            "app_name": ["app_name", "name"],
            "destination_namespace": ["destination_namespace", "dest_namespace"],
        },
        "sync_application": {
            "cluster_name": ["cluster_name", "dest_cluster"],
            "app_name": ["app_name", "name"],
        },
        "delete_application": {
            "cluster_name": ["cluster_name", "dest_cluster"],
            "app_name": ["app_name", "name"],
        },
        "create_project": {
            "project_name": ["project_name", "name"],
        },
        "delete_project": {
            "project_name": ["project_name", "name"],
        }
    }

    def _missing_required(self, tool_name: str, tool_args: Dict[str, Any]) -> List[str]:
        required = self.REQUIRED_ARGS.get(tool_name, [])
        aliases = self.REQUIRED_ALIASES.get(tool_name, {})
        missing: List[str] = []
        for k in required:
            keys_to_check = aliases.get(k, [k])
            found = False
            v = None
            for kk in keys_to_check:
                if kk not in tool_args:
                    continue
                v = tool_args.get(kk)
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                found = True
                break

            if not found:
                missing.append(k)
                continue

            if v is None:
                missing.append(k)
                continue
            if isinstance(v, str) and not v.strip():
                missing.append(k)
                continue
        return missing

    def _extract_project_destinations(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Best-effort extraction of project destinations from ArgoCD state.

        Expected shapes we may see:
        - state["project_info"]["destinations"] -> list
        - state["project_info"]["spec"]["destinations"] -> list
        - state["project_info"]["details"]["destinations"] -> list
        """
        if not isinstance(state, dict):
            return []

        project_info = state.get("project_info")
        if not isinstance(project_info, dict):
            return []

        # direct
        dests = project_info.get("destinations")
        if isinstance(dests, list):
            return [d for d in dests if isinstance(d, dict)]

        # common nested shapes
        spec = project_info.get("spec")
        if isinstance(spec, dict) and isinstance(spec.get("destinations"), list):
            return [d for d in spec.get("destinations") if isinstance(d, dict)]

        details = project_info.get("details")
        if isinstance(details, dict):
            # Some tool responses return destinations at top-level
            if isinstance(details.get("destinations"), list):
                return [d for d in details.get("destinations") if isinstance(d, dict)]
            # Some return ArgoCD-style shape with spec.destinations
            spec2 = details.get("spec")
            if isinstance(spec2, dict) and isinstance(spec2.get("destinations"), list):
                return [d for d in spec2.get("destinations") if isinstance(d, dict)]

        return []

    def _apply_destination_defaults(
        self, tool_args: Dict[str, Any], destinations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Apply safe destination defaults when unambiguous.

        We only default `dest_namespace` because it is a known tool arg in this codebase.
        The ArgoCD destination server/cluster field varies by MCP schema; we avoid injecting
        unknown fields here to prevent schema validation failures.
        """
        if not destinations:
            return {}

        def _is_wildcard(v: Any) -> bool:
            s = str(v or "").strip()
            return s in {"*", "/*"} or s.lower() in {"any", "all"}

        def _ns_key() -> str:
            # Prefer the key style already used by the tool call
            if "destination_namespace" in tool_args or "destination_namespace" in (tool_args or {}):
                return "destination_namespace"
            if "dest_namespace" in tool_args:
                return "dest_namespace"
            # Default to MCP schema key
            return "destination_namespace"

        ns_key = _ns_key()

        # If there is exactly one destination, default namespace if missing
        if len(destinations) == 1:
            ns = (destinations[0].get("namespace") or "").strip()
            if _is_wildcard(ns):
                # Wildcard isn't a concrete choice; pick safe default namespace
                ns = "default"

            if ns and (not str(tool_args.get(ns_key, "") or "").strip()):
                return {ns_key: ns}
            return {}

        # Multiple destinations: if project allows wildcard namespace, pick safe default
        # (This reduces user churn while still staying within allowed destinations.)
        if any(_is_wildcard(d.get("namespace")) for d in destinations):
            if not str(tool_args.get(ns_key, "") or "").strip():
                return {ns_key: "default"}
        return {}

    def _infer_project_name_from_state(self, state: Dict[str, Any]) -> Optional[str]:
        """
        Try to infer an ArgoCD project name from existing state to avoid unnecessary HITL.
        """
        if not isinstance(state, dict):
            return None

        # If we already have project_info populated, prefer its name
        project_info = state.get("project_info")
        if isinstance(project_info, dict):
            n = (project_info.get("name") or "").strip()
            if n:
                return n
            raw_name = (project_info.get("project_name") or "").strip()
            if raw_name:
                return raw_name

        # Fall back to parsing user_request text
        text = str(state.get("user_request") or "").strip()
        if not text:
            return None

        # Patterns like: "argocd project will be demo", "project name is demo", "project 'demo'"
        for pat in [
            r"(?:argocd\s+)?project(?:\s+name)?\s*(?:will\s+be|is|=|:)\s*([a-z0-9-]+)",
            r"project\s+named\s+[\"'“”]?([a-z0-9-]+)[\"'”]?",
            r"project\s+[\"'“”]?([a-z0-9-]+)[\"'”]?",
        ]:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()

        return None

    def _extract_project_source_repos(self, state: Dict[str, Any]) -> List[str]:
        """
        Best-effort extraction of allowed source repos for the selected project.

        Common shapes:
        - state["project_info"]["source_repos"] -> list[str]
        - state["project_info"]["spec"]["sourceRepos"] -> list[str]
        - state["project_info"]["details"]["source_repos"] -> list[str]
        - state["project_info"]["details"]["spec"]["sourceRepos"] -> list[str]
        - state["project_info"]["raw_response"]["spec"]["sourceRepos"] -> list[str]
        """
        if not isinstance(state, dict):
            return []

        project_info = state.get("project_info")
        if not isinstance(project_info, dict):
            return []

        def _as_list(v: Any) -> List[str]:
            if isinstance(v, list):
                return [str(x) for x in v if isinstance(x, (str, int, float)) or x is not None]
            return []

        src = _as_list(project_info.get("source_repos"))
        if src:
            return src

        spec = project_info.get("spec")
        if isinstance(spec, dict):
            src2 = _as_list(spec.get("sourceRepos"))
            if src2:
                return src2

        details = project_info.get("details")
        if isinstance(details, dict):
            src3 = _as_list(details.get("source_repos"))
            if src3:
                return src3
            spec2 = details.get("spec")
            if isinstance(spec2, dict):
                src4 = _as_list(spec2.get("sourceRepos"))
                if src4:
                    return src4

        raw = project_info.get("raw_response")
        if isinstance(raw, dict):
            spec3 = raw.get("spec")
            if isinstance(spec3, dict):
                src5 = _as_list(spec3.get("sourceRepos"))
                if src5:
                    return src5

        return []

    def _repo_identity(self, repo_url: str) -> str:
        """
        Normalize repo URL into a comparable identity: "<host>/<owner>/<repo>".
        Handles:
        - git@github.com:org/repo(.git)
        - https://github.com/org/repo(.git)
        - ssh://git@github.com/org/repo(.git)
        """
        u = str(repo_url or "").strip()
        if not u:
            return ""

        # git@host:org/repo(.git)
        m = re.match(r"^git@([^:]+):(.+)$", u)
        if m:
            host = m.group(1).lower()
            path = m.group(2).lstrip("/").rstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return f"{host}/{path}".lower()

        # ssh://git@host/org/repo(.git) or https://host/org/repo(.git)
        m2 = re.match(r"^(?:ssh|https?)://(?:[^@/]+@)?([^/]+)/(.+)$", u)
        if m2:
            host = m2.group(1).lower()
            path = m2.group(2).lstrip("/").rstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return f"{host}/{path}".lower()

        return u.lower()

    def _is_wildcard_repo_allowed(self, allowed: List[str]) -> bool:
        return any(str(x).strip() in {"*", "/*"} for x in (allowed or []))

    def _parse_user_inputs(self, human_response: Any) -> Dict[str, Any]:
        """
        Accept common resume shapes:
        - dict with `edited_tool_args`
        - dict with fields directly
        - JSON string
        - lines like `key=value` or `key: value`
        """
        if isinstance(human_response, dict):
            return human_response.get("edited_tool_args") or human_response

        text = str(human_response or "").strip()
        if not text:
            return {}

        lower = text.lower()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed.get("edited_tool_args") or parsed
        except Exception:
            pass

        # Lightweight natural-language extraction (deterministic patterns)
        extracted: Dict[str, Any] = {}

        def _match(pattern: str) -> Optional[str]:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            return m.group(1).strip() if m else None

        # Common fields users provide conversationally
        project = _match(r"(?:argocd\s+)?project(?:\s+name)?\s*(?:is|=|:)\s*([a-z0-9-]+)")
        if project:
            extracted["project"] = project

        app_name = _match(r"(?:application|app)(?:\s+name)?\s*(?:is|=|:)\s*([a-z0-9-]+)")
        if app_name:
            extracted["name"] = app_name

        namespace = _match(r"(?:target\s+)?namespace\s*(?:is|=|:)\s*([a-z0-9-]+)")
        if namespace:
            extracted["dest_namespace"] = namespace

        cluster = _match(r"(?:target\s+)?cluster(?:\s+name)?\s*(?:is|=|:)\s*([a-z0-9-]+)")
        if cluster:
            extracted["dest_cluster"] = cluster

        revision = _match(r"(?:git\s+)?(?:revision|branch|tag|commit)\s*(?:is|=|:)\s*([^\s,]+)")
        if revision:
            extracted["target_revision"] = revision

        sync_policy = _match(r"sync\s+policy\s*(?:is|=|:)\s*(manual|auto|automatic)")
        if sync_policy:
            extracted["sync_policy"] = "automatic" if sync_policy.lower().startswith("auto") else "manual"

        # Allow explicit "use defaults" / "rely on your knowledge"
        if ("use defaults" in lower) or ("defaults" in lower and "rely" in lower) or ("rely on" in lower and "knowledge" in lower):
            extracted["_use_defaults"] = True

        kv: Dict[str, Any] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
            elif ":" in line:
                k, v = line.split(":", 1)
            else:
                continue
            kv[k.strip()] = v.strip()

        # Merge key/value lines on top of extracted values
        return {**extracted, **kv}

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {}) or {}

        # Agentic defaulting: infer required args from state when possible
        if isinstance(request.state, dict):
            # If project agent forgets to pass `name` for create_project, infer it from state/user_request
            if tool_name == "create_project" and (not str(tool_args.get("project_name") or "").strip()) and (not str(tool_args.get("name") or "").strip()):
                inferred = self._infer_project_name_from_state(request.state)
                if inferred:
                    tool_args = {**tool_args, "project_name": inferred}
                    request.tool_call["args"] = tool_args

            # Default cluster_name when missing for app operations
            if tool_name in {"create_application", "sync_application", "delete_application", "get_application_details", "get_application_status", "get_application_diff", "get_sync_status"}:
                if not str(tool_args.get("cluster_name") or "").strip():
                    # Safe default used across examples; prevents unnecessary HITL.
                    tool_args = {**tool_args, "cluster_name": "default"}
                    request.tool_call["args"] = tool_args

            # Repo URL normalization for create_application:
            # Ensure the repo_url we pass is permitted by the selected project.
            if tool_name == "create_application":
                allowed = self._extract_project_source_repos(request.state)
                repo_url = str(tool_args.get("repo_url") or "").strip()
                if repo_url and allowed and (not self._is_wildcard_repo_allowed(allowed)):
                    # If exact match isn't allowed but identity matches one allowed entry, swap.
                    if repo_url not in allowed:
                        rid = self._repo_identity(repo_url)
                        for a in allowed:
                            if self._repo_identity(a) == rid:
                                tool_args = {**tool_args, "repo_url": a}
                                request.tool_call["args"] = tool_args
                                break

        # Agentic defaulting: if creating an app and the project has a single destination,
        # auto-fill dest_namespace so we don't interrupt for cluster/namespace repeatedly.
        if tool_name == "create_application" and isinstance(request.state, dict):
            destinations = self._extract_project_destinations(request.state)
            injected = self._apply_destination_defaults(tool_args, destinations)
            if injected:
                tool_args = {**tool_args, **injected}
                request.tool_call["args"] = tool_args

        missing = self._missing_required(tool_name, tool_args)
        if not missing:
            return await handler(request)

        fields = "\n".join([f"- **{self.FRIENDLY_FIELD_NAMES.get(k, k)}** (`{k}`)" for k in missing])
        example_json = "{\n" + ",\n".join([f'  \"{k}\": \"...\"' for k in missing]) + "\n}"
        question = (
            "I need a few required values to continue:\n\n"
            f"{fields}\n\n"
            "Reply with a JSON object (recommended), e.g.:\n"
            f"{example_json}\n"
        )

        interrupt_payload = {
            "pending_feedback_requests": {
                "status": "input_required",
                "session_id": (request.state.get("session_id") if isinstance(request.state, dict) else "unknown") or "unknown",
                "question": question,
                "context": f"Missing required inputs for `{tool_name}`.",
                "active_phase": "argocd_onboarding",
            }
        }

        human_response = interrupt(interrupt_payload)
        user_args = self._parse_user_inputs(human_response)

        # If user explicitly requested defaults, fill safe defaults for remaining missing fields
        use_defaults = bool(user_args.pop("_use_defaults", False))
        if use_defaults:
            if "project" in missing and "project" not in user_args:
                user_args["project"] = "default"
            if "app_name" in missing and "app_name" not in user_args:
                # derive from provided path if present
                p = (tool_args.get("path") or "").strip()
                if p and "/" in p:
                    user_args["app_name"] = p.rstrip("/").split("/")[-1]
            if "destination_namespace" in missing and "destination_namespace" not in user_args:
                # Prefer project destination default if available
                destinations = self._extract_project_destinations(request.state) if isinstance(request.state, dict) else []
                injected = self._apply_destination_defaults({**tool_args, **user_args}, destinations)
                if "destination_namespace" in injected:
                    user_args["destination_namespace"] = injected["destination_namespace"]
                elif "dest_namespace" in injected:
                    user_args["destination_namespace"] = injected["dest_namespace"]
                else:
                    user_args["destination_namespace"] = user_args.get("project") or tool_args.get("project") or "default"
            # Provide common defaults if the tool supports them
            if "target_revision" not in tool_args and "target_revision" not in user_args:
                user_args["target_revision"] = "HEAD"
            if "sync_policy" not in tool_args and "sync_policy" not in user_args:
                user_args["sync_policy"] = "manual"

        # If multiple destinations exist and destination_namespace is missing, ask once to choose
        if tool_name == "create_application" and "destination_namespace" in missing and isinstance(request.state, dict):
            destinations = self._extract_project_destinations(request.state)
            if len(destinations) > 1:
                options = []
                for idx, d in enumerate(destinations, start=1):
                    server = d.get("server") or "unknown-server"
                    ns = d.get("namespace") or "unknown-namespace"
                    options.append(f"{idx}. {server}/{ns}")
                question2 = (
                    "This ArgoCD project allows multiple deployment destinations. "
                    "Which destination should I use?\n\n"
                    + "\n".join(options)
                    + "\n\nReply with the number (recommended) or a JSON object like {\"destination_namespace\": \"...\"}."
                )
                interrupt_payload2 = {
                    "pending_feedback_requests": {
                        "status": "input_required",
                        "session_id": (request.state.get("session_id") or "unknown"),
                        "question": question2,
                        "context": "Choose a destination allowed by the selected ArgoCD project.",
                        "active_phase": "argocd_onboarding",
                    }
                }
                resp2 = interrupt(interrupt_payload2)
                parsed2 = self._parse_user_inputs(resp2)
                if "destination_namespace" not in parsed2 and "dest_namespace" not in parsed2:
                    # allow a numeric selection
                    try:
                        choice = int(str(resp2).strip())
                        if 1 <= choice <= len(destinations):
                            chosen = destinations[choice - 1]
                            ns = (chosen.get("namespace") or "").strip()
                            if ns:
                                parsed2["destination_namespace"] = ns
                    except Exception:
                        pass
                user_args = {**user_args, **parsed2}

        # Merge + continue (tool runner will validate final args)
        request.tool_call["args"] = {**tool_args, **user_args}
        return await handler(request)



class ArgoCDApprovalHITLMiddleware(AgentMiddleware):
    """
    Middleware for production gating and structured approval payloads.
    
    Note: Simple approve/reject for delete/sync operations is now handled by
    the built-in HumanInTheLoopMiddleware. This custom middleware only handles:
    - Production namespace gating (create_application in prod)
    - Structured payloads for rich approval UIs
    """
    
    # Production namespace patterns that trigger approval
    PRODUCTION_NAMESPACES = {"production", "prod", "live", "prd"}

    # Tools that should use standardized approval templates
    TEMPLATE_APPROVAL_TOOLS: Set[str] = {
        "sync_application",
        "delete_application",
        "delete_project",
        "create_application",  # only gated for production namespace below
    }

    def _format_template(self, template: str, values: Dict[str, Any]) -> str:
        """Format template with safe defaults for missing keys."""
        class _SafeDict(dict):
            def __missing__(self, key):
                return "unknown"

        return template.format_map(_SafeDict(values))

    def _extract_confirm_text(self, human_response: Any) -> str:
        """Best-effort extraction of confirmation text from HITL response."""
        if isinstance(human_response, dict):
            # Common shapes: {"confirm_text": "..."} or {"confirmation": "..."} or {"text": "..."}
            return str(
                human_response.get("confirm_text")
                or human_response.get("confirmation")
                or human_response.get("confirm")
                or human_response.get("text")
                or human_response.get("input")
                or ""
            )
        return str(human_response or "")

    def _normalize_confirm(self, text: str) -> str:
        return str(text or "").strip().strip("`\"'").strip().lower()

    def _extract_app_context_from_state(self, state: Any, app_name: str) -> Dict[str, str]:
        """
        Pull project/namespace for an application from state if available.
        Works with get_application_details shapes from docs/app_onboarding/argo_mcp_tools.md.
        """
        if not isinstance(state, dict):
            return {}

        # Prefer explicit application_details when present
        details = state.get("application_details")
        if isinstance(details, dict):
            # If state holds details for another app, we still use it as best-effort only
            project = str(details.get("project") or "")
            dest = details.get("destination") or {}
            namespace = ""
            if isinstance(dest, dict):
                namespace = str(dest.get("namespace") or "")
            return {
                "project": project or "",
                "namespace": namespace or "",
            }

        info = state.get("application_info")
        if isinstance(info, dict):
            project = str(info.get("project") or "")
            dest = info.get("destination") or {}
            namespace = str(info.get("namespace") or "")
            if not namespace and isinstance(dest, dict):
                namespace = str(dest.get("namespace") or "")
            return {
                "project": project or "",
                "namespace": namespace or "",
            }

        return {}

    async def _request_templated_approval(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
        tool_name: str,
        tool_args: Dict[str, Any],
        template_key: str,
        template_values: Dict[str, Any],
        phase: str,
        require_exact_confirm: Optional[str] = None,
    ) -> ToolMessage | Command:
        """Request approval using a standardized template and optional exact-name confirmation."""
        tool_call_id = request.tool_call.get("id", "unknown")

        # Import templates lazily to avoid import-time cycles
        from k8s_autopilot.core.agents.onboarding.prompts.argocd_prompts import APPROVAL_TEMPLATES

        template = APPROVAL_TEMPLATES.get(template_key, "")
        question = self._format_template(template, template_values)

        interrupt_payload = {
            "pending_tool_calls": {
                tool_call_id: {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "is_critical": True,
                    "phase": phase,
                    "reason": question,
                    "status": "pending",
                    "allowed_decisions": ["approve", "reject"],
                }
            }
        }

        human_response = interrupt(interrupt_payload)
        is_approved, edited_tool_args = self._parse_human_decision(human_response)

        # Optional hard confirmation (type exact app/project name).
        # IMPORTANT: Do not fail the operation silently and cause the agent to re-issue the same tool call.
        # Instead, if approved but confirmation is missing/mismatched, ask a focused follow-up question here.
        if is_approved and require_exact_confirm:
            confirm_text = self._normalize_confirm(self._extract_confirm_text(human_response))
            required = self._normalize_confirm(require_exact_confirm)
            freeform = self._normalize_confirm(str(human_response or ""))

            if confirm_text != required and required not in freeform:
                followup_payload = {
                    "pending_feedback_requests": {
                        "status": "input_required",
                        "session_id": (request.state.get("session_id") if isinstance(request.state, dict) else "unknown") or "unknown",
                        "question": (
                            "To confirm you understand the impact, type the name exactly:\n\n"
                            f"`{require_exact_confirm}`"
                        ),
                        "context": f"Exact-name confirmation required for `{tool_name}`.",
                        "active_phase": phase,
                        "tool_name": tool_name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                }
                followup_response = interrupt(followup_payload)
                followup_text = self._normalize_confirm(self._extract_confirm_text(followup_response))
                followup_freeform = self._normalize_confirm(str(followup_response or ""))
                if followup_text != required and required not in followup_freeform:
                    # Now we can reject explicitly.
                    is_approved = False

        if is_approved:
            if edited_tool_args:
                request.tool_call["args"] = {**tool_args, **edited_tool_args}
            return await handler(request)

        response_str = str(human_response).lower() if human_response else ""
        return ToolMessage(
            content=f"Operation rejected. Response: {response_str}",
            tool_call_id=request.tool_call.get("id", ""),
        )
    
    def _parse_human_decision(self, human_response: Any) -> tuple[bool, Dict[str, Any]]:
        """
        Parse human response from interrupt resume.
        
        Expected shapes (examples):
        - "approve" / "reject"
        - {"decision": "approve"}
        - {"approved": true}
        - {"status": "approved"|"rejected"}
        - {"edited_tool_args": {...}}  # optional override
        """
        if isinstance(human_response, dict):
            decision = (
                human_response.get("decision")
                or human_response.get("status")
                or ("approve" if human_response.get("approved") else None)
            )
            edited_args = human_response.get("edited_tool_args") or {}
            is_approved = str(decision).lower() in {"approve", "approved", "yes", "proceed", "true"}
            return is_approved, edited_args

        response_str = str(human_response) if human_response else ""
        lower_str = response_str.lower()

        # Handle A2UI resume payloads (e.g., "USER_ACTION: hitl_response, CONTEXT: {...}")
        if "USER_ACTION:" in response_str and "CONTEXT:" in response_str:
            try:
                _, context_part = response_str.split("CONTEXT:", 1)
                context_json = context_part.strip()
                parsed_context = json.loads(context_json)
                decision = parsed_context.get("decision") or parsed_context.get("status")
                edited_args = parsed_context.get("edited_tool_args") or {}
                is_approved = str(decision).lower() in {"approve", "approved", "yes", "proceed", "true"}
                return is_approved, edited_args
            except Exception:
                # Fall through to string matching
                pass

        is_approved = any(token in lower_str for token in ("approve", "approved", "yes", "proceed"))
        return is_approved, {}

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Intercept tool calls for templated approvals (LangGraph interrupts)."""
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})

        # Standardized template approvals for destructive / high-impact operations
        if tool_name == "delete_application":
            app_name = tool_args.get("app_name") or tool_args.get("name") or "unknown"
            cascade = bool(tool_args.get("cascade", True))

            # Delete tool args don't include project/namespace; enrich from state if possible
            ctx = self._extract_app_context_from_state(getattr(request, "state", None), app_name)
            project = (tool_args.get("project") or ctx.get("project") or "unknown")
            namespace = (tool_args.get("destination_namespace") or tool_args.get("dest_namespace") or ctx.get("namespace") or "unknown")

            impact_message = (
                "This will DELETE all Kubernetes resources managed by this application."
                if cascade
                else "The ArgoCD Application will be removed; Kubernetes resources may be orphaned."
            )

            return await self._request_templated_approval(
                request,
                handler,
                tool_name,
                tool_args,
                template_key="delete_application",
                template_values={
                    "app_name": app_name,
                    "project": project,
                    "namespace": namespace,
                    "cascade": cascade,
                    "impact_message": impact_message,
                    "resource_list": "Will be determined by ArgoCD/Kubernetes during deletion.",
                },
                phase="delete_approval",
                require_exact_confirm=app_name,
            )

        if tool_name == "delete_project":
            project_name = tool_args.get("project_name") or tool_args.get("name") or tool_args.get("project") or "unknown"
            cascade = bool(tool_args.get("cascade", False))
            impact_message = (
                "Cascade is enabled: applications in this project may be deleted as part of cleanup."
                if cascade
                else "You may need to delete applications in this project before deleting the project."
            )
            return await self._request_templated_approval(
                request,
                handler,
                tool_name,
                tool_args,
                template_key="delete_project",
                template_values={
                    "project_name": project_name,
                    "app_count": "unknown",
                    "cascade": cascade,
                    "impact_message": impact_message,
                },
                phase="delete_approval",
                require_exact_confirm=project_name,
            )

        if tool_name == "sync_application":
            app_name = tool_args.get("app_name") or tool_args.get("name") or "unknown"
            namespace = tool_args.get("destination_namespace") or tool_args.get("dest_namespace") or tool_args.get("namespace", "unknown")
            project = tool_args.get("project", "unknown")
            dry_run = bool(tool_args.get("dry_run", False))
            prune = bool(tool_args.get("prune", False))
            force = bool(tool_args.get("force", False))

            is_prod = namespace.lower() in self.PRODUCTION_NAMESPACES
            template_key = "production_sync" if is_prod else "sync_application"

            base_values = {
                "app_name": app_name,
                "project": project,
                "namespace": namespace,
                "dry_run": dry_run,
                "prune": prune,
                "force": force,
                "reason": "Sync may create/update/delete live Kubernetes resources.",
                # production_sync placeholders
                "revision": tool_args.get("revision", "unknown"),
                "changes_summary": "Diff will be shown if available.",
                "deployment_count": "unknown",
                "service_count": "unknown",
                "configmap_count": "unknown",
            }

            return await self._request_templated_approval(
                request,
                handler,
                tool_name,
                tool_args,
                template_key=template_key,
                template_values=base_values,
                phase="sync_approval",
            )
        
        # Always require a final approval before creating an application.
        # (Plan approval is not a substitute for a tool-level gate.)
        if tool_name == "create_application":
            app_name = tool_args.get("app_name") or tool_args.get("name") or "unknown"
            project = tool_args.get("project", "unknown")
            repo_url = tool_args.get("repo_url", "unknown")
            path = tool_args.get("path", "unknown")
            target_revision = tool_args.get("target_revision", "HEAD")
            destination_server = tool_args.get("destination_server", "https://kubernetes.default.svc")
            destination_namespace = tool_args.get("destination_namespace") or tool_args.get("dest_namespace") or "unknown"
            auto_sync = bool(tool_args.get("auto_sync", False))
            prune = bool(tool_args.get("prune", True))
            self_heal = bool(tool_args.get("self_heal", True))

            is_prod = destination_namespace.lower() in self.PRODUCTION_NAMESPACES
            template_key = "create_production_app" if is_prod else "create_application"

            # Keep existing production template semantics; otherwise use standard create template.
            if is_prod:
                return await self._request_templated_approval(
                    request,
                    handler,
                    tool_name,
                    tool_args,
                    template_key="create_production_app",
                    template_values={
                        "app_name": app_name,
                        "project": project,
                        "namespace": destination_namespace,
                        "repo_url": repo_url,
                        "path": path,
                        "sync_policy": "automatic" if auto_sync else "manual",
                        "auto_sync": auto_sync,
                    },
                    phase="production_approval",
                )

            return await self._request_templated_approval(
                request,
                handler,
                tool_name,
                tool_args,
                template_key="create_application",
                template_values={
                    "app_name": app_name,
                    "project": project,
                    "destination_server": destination_server,
                    "destination_namespace": destination_namespace,
                    "repo_url": repo_url,
                    "path": path,
                    "target_revision": target_revision,
                    "auto_sync": auto_sync,
                    "prune": prune,
                    "self_heal": self_heal,
                },
                phase="app_approval",
            )
        
        return await handler(request)


class ArgoCDErrorRecoveryMiddleware(AgentMiddleware):
    """
    Middleware for handling tool errors and implementing retry logic.
    """
    
    def __init__(self, max_retries: int = 3, retry_tools: Optional[Set[str]] = None):
        self.max_retries = max_retries
        self.retry_tools = retry_tools or {
            "get_project",
            "list_projects",
            "validate_repository_connection",
            "list_repositories",
            "get_application_details",
            "get_application_status",
            "get_application_logs",
            "get_application_events",
            "get_pod_metrics",
            "get_argocd_status",
        }
    
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        
        # Only retry specific read-only/idempotent tools
        if tool_name not in self.retry_tools:
            return await handler(request)
        
        attempts = 0
        last_error = None
        
        while attempts <= self.max_retries:
            try:
                attempts += 1
                return await handler(request)
            except Exception as e:
                last_error = e
                argocd_agent_logger.log_structured(
                    level="WARNING",
                    message=f"Tool {tool_name} failed (attempt {attempts}/{self.max_retries + 1}): {e}"
                )
                if attempts > self.max_retries:
                    break
        
        # If all retries fail, return a structured error message
        return ToolMessage(
            content=f"Error: Tool '{tool_name}' failed after {self.max_retries} retries. Reason: {str(last_error)}",
            tool_call_id=request.tool_call.get("id", ""),
            status="error"
        )


# Export all middleware classes
__all__ = [
    "ArgoCDStateMiddleware",
    "ArgoCDMissingInputsHITLMiddleware",
    "ArgoCDApprovalHITLMiddleware",
    "ArgoCDErrorRecoveryMiddleware",
    "ArgoCDToolRegistry",
    "_ARGOCD_TOOL_REGISTRY",
]
