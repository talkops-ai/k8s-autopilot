#!/bin/bash
set -e

# Use environment variables with defaults if not set
exec k8s-autopilot \
  --host "${A2A_HOST:-0.0.0.0}" \
  --port "${A2A_PORT:-10102}" \
  --agent-card "${A2A_AGENT_CARD:-k8s_autopilot/card/k8s_autopilot.json}" \
  "$@"
