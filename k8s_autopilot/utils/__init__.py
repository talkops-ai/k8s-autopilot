"""Utils module for K8s Autopilot."""

from __future__ import annotations

from k8s_autopilot.utils.cost_estimation import TokenCostEstimator, estimate_cost
from k8s_autopilot.utils.git import (
    find_git_dir,
    get_git_branch,
    get_git_remote_url,
    get_git_root,
)
from k8s_autopilot.utils.logger import AgentLogger, configure_logging, get_logger
from k8s_autopilot.utils.session_stats import (
    ModelStats,
    SessionStats,
    format_cost,
    format_token_count,
)
from k8s_autopilot.utils.startup_error import STARTUP_ERROR_MARKER, emit_startup_failure

__all__ = [
    "AgentLogger",
    "configure_logging",
    "get_logger",
    "ModelStats",
    "STARTUP_ERROR_MARKER",
    "SessionStats",
    "TokenCostEstimator",
    "emit_startup_failure",
    "estimate_cost",
    "find_git_dir",
    "format_cost",
    "format_token_count",
    "get_git_branch",
    "get_git_remote_url",
    "get_git_root",
]
