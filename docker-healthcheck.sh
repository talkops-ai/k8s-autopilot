#!/bin/bash
set -e

# Use environment variables with defaults if not set
A2A_HOST="${A2A_HOST:-0.0.0.0}"
A2A_PORT="${A2A_PORT:-10102}"

# For healthcheck, use localhost instead of 0.0.0.0
# If A2A_HOST is 0.0.0.0, use localhost for the healthcheck
if [ "$A2A_HOST" = "0.0.0.0" ]; then
  HEALTH_HOST="localhost"
else
  HEALTH_HOST="$A2A_HOST"
fi

# Check if server is responding by hitting the agent card endpoint
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "http://${HEALTH_HOST}:${A2A_PORT}/.well-known/agent-card.json" || echo "000")

# A2A server exposes agent card at /.well-known/agent-card.json which returns 200 OK when healthy
if [ "$HTTP_CODE" = "200" ]; then
  echo "K8s Autopilot Agent is healthy (HTTP $HTTP_CODE)";
  exit 0;
fi;

# If we get 000, it means curl failed (connection refused, timeout, etc.)
if [ "$HTTP_CODE" = "000" ]; then
  echo "K8s Autopilot Agent is not responding";
  exit 1;
fi;

# Any other response is considered unhealthy
echo "K8s Autopilot Agent returned unexpected HTTP code: $HTTP_CODE";
exit 1;