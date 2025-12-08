"""
Core module for K8s Auto Pilot Agent.

This module contains the core components for K8s Auto Pilot protocol integration,
agent management, Agent Card service discovery, and Supervisor Agent orchestration.
"""

from k8s_autopilot.core.a2a_autopilot_executor import A2AAutoPilotExecutor, ExecutorValidationMixin

__all__ = [
    # A2A Executor
    "A2AAutoPilotExecutor",
    "ExecutorValidationMixin",
]