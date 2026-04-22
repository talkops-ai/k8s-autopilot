"""Shared HITL tool factory for planner sub-agents.

Creates a ``request_human_input`` tool that triggers LangGraph ``interrupt()``
for human-in-the-loop feedback during the planning pipeline.

Reference: aws-orchestrator tf_planner/new_module/shared/hitl.py
"""
