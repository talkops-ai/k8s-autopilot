"""
Request validation utilities for HITL.

Validates that user requests are related to Helm chart generation/deployment.
"""

from typing import Dict, Any, Optional, Tuple
import re
from k8s_autopilot.utils.logger import AgentLogger

# Create logger for request validator
validator_logger = AgentLogger("k8sAutopilotRequestValidator")

# Keywords that indicate Helm chart related requests
HELM_KEYWORDS = [
    "helm", "chart", "kubernetes", "k8s", "kube",
    "deployment", "deploy", "container", "pod", "service",
    "ingress", "configmap", "secret", "pvc", "hpa",
    "statefulset", "daemonset", "namespace", "rbac",
    "argocd", "gitops", "yaml", "manifest",
    "docker", "image", "registry", "cluster"
]

# Keywords that indicate non-Helm requests (should be rejected)
NON_HELM_KEYWORDS = [
    "terraform", "aws", "azure", "gcp", "cloudformation",
    "ansible", "puppet", "chef", "python script", "bash script",
    "database migration", "api endpoint", "rest api", "graphql",
    "frontend", "ui", "website", "html", "css", "javascript",
    "code review", "debug", "test", "unit test", "integration test"
]


def is_helm_related_request(user_query: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Validate if a user request is related to Helm chart generation/deployment.
    
    Args:
        user_query: User's request/query string
        
    Returns:
        Tuple of:
        - is_valid: True if request is Helm-related
        - reason: Explanation of validation result
        - interrupt_data: Optional interrupt data if ambiguous (needs human confirmation)
    """
    query_lower = user_query.lower()
    
    # Check for explicit non-Helm keywords
    non_helm_matches = [kw for kw in NON_HELM_KEYWORDS if kw in query_lower]
    if non_helm_matches:
        validator_logger.log_structured(
            level="INFO",
            message="Request rejected - contains non-Helm keywords",
            extra={"non_helm_keywords": non_helm_matches, "query": user_query[:100]}
        )
        return False, f"Request appears to be about {', '.join(non_helm_matches[:3])}, which is not related to Helm chart generation or deployment.", None
    
    # Check for Helm-related keywords
    helm_matches = [kw for kw in HELM_KEYWORDS if kw in query_lower]
    
    if helm_matches:
        # Strong match - clearly Helm-related
        validator_logger.log_structured(
            level="INFO",
            message="Request validated - contains Helm keywords",
            extra={"helm_keywords": helm_matches, "query": user_query[:100]}
        )
        return True, f"Request is related to Helm/Kubernetes: {', '.join(helm_matches[:3])}", None
    
    # Check for common Helm request patterns
    helm_patterns = [
        r"create.*chart",
        r"generate.*chart",
        r"write.*chart",
        r"build.*chart",
        r"deploy.*kubernetes",
        r"deploy.*k8s",
        r"setup.*kubernetes",
        r"kubernetes.*deployment",
        r"helm.*chart",
        r"chart.*helm"
    ]
    
    for pattern in helm_patterns:
        if re.search(pattern, query_lower):
            validator_logger.log_structured(
                level="INFO",
                message="Request validated - matches Helm pattern",
                extra={"pattern": pattern, "query": user_query[:100]}
            )
            return True, f"Request matches Helm chart pattern: {pattern}", None
    
    # Ambiguous - might need human confirmation
    validator_logger.log_structured(
        level="WARNING",
        message="Request is ambiguous - no clear Helm keywords found",
        extra={"query": user_query[:100]}
    )
    
    interrupt_data = {
        "type": "request_validation",
        "phase": "requirements",
        "summary": f"""
# Request Validation Required

The following request does not clearly indicate it's related to Helm chart generation or deployment:

**User Request:**
{user_query}

**Validation Status:** Ambiguous

Please confirm if this request is related to:
- Helm chart creation/generation
- Kubernetes deployment configuration
- Container orchestration setup

If not, the supervisor will reject the request.
        """,
        "required_action": "confirm",
        "options": ["approve", "reject"],
        "original_query": user_query
    }
    
    return False, "Request is ambiguous - does not clearly indicate Helm chart generation/deployment", interrupt_data


def validate_and_reject_non_helm(
    user_query: str,
    require_confirmation: bool = True
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
    """
    Validate request and provide rejection message if not Helm-related.
    
    Args:
        user_query: User's request/query string
        require_confirmation: If True, ambiguous requests trigger interrupt for confirmation
        
    Returns:
        Tuple of:
        - is_valid: True if request should proceed
        - rejection_message: Message to return if rejected (None if valid)
        - interrupt_data: Optional interrupt data if ambiguous and require_confirmation=True
    """
    is_valid, reason, interrupt_data = is_helm_related_request(user_query)
    
    if is_valid:
        return True, None, None
    
    # Create rejection message
    rejection_message = f"""
I can only help with Kubernetes Helm chart generation and deployment tasks.

Your request: "{user_query[:100]}"

{reason}

Please provide a request related to:
- Creating or generating Helm charts
- Kubernetes deployment configurations
- Container orchestration setup
- Helm chart validation or deployment

Examples of valid requests:
- "Create a Helm chart for nginx"
- "Generate a Kubernetes deployment for my API"
- "Help me write a Helm chart for a web application"
- "Deploy my application to Kubernetes using Helm"
    """.strip()
    
    if interrupt_data and require_confirmation:
        # Return interrupt data for human confirmation
        return False, None, interrupt_data
    
    # Direct rejection
    return False, rejection_message, None

