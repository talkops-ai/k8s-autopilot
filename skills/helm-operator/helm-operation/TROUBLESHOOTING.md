# Helm Operations Troubleshooting Guide

If the Helm Deployment pipeline fails, the helm-operation sub-agent handles read-failures dynamically while allowing structural execution issues to surface.

## Architectural Tool Recovery

### Read-Only Failure Retry
Read-only and idempotent query operations (e.g. `helm_search_charts` or `kubernetes_get_cluster_info`) are wrapped with transient error handling via `ToolRetryMiddleware`.
If a tool encounters network timeouts or upstream 500s:
1. The middleware intercepts the error automatically.
2. It natively retries the tool payload up to **2 times** before bubbling the exception up to the agent context.
3. You never need to loop these read operations manually.

### Duplicate Execution (Agent Lockout)
The system no longer employs hash-based execution locks. It relies exclusively on the `HumanInTheLoopMiddleware` approval gate natively binding high-stakes execution tools. Attempting to call `helm_install_chart` twice will pause the system to prompt for approval again via structured decisions (approve/edit/reject).
**Resolution**: Never retry installation logic mechanically unless instructed. Transition back to verification instead (`helm_get_release_status`).

## Manual Resolutions

### Error: "Another Operation in Progress"
**Cause:** A prior helm chart deployment hung, leaving the release in `pending-install` or `pending-upgrade`.
**Troubleshooting Strategy:**
```python
# 1. Look for pending-* status
helm_get_release_history(name, namespace)

# 2. If stuck in pending-install
helm_uninstall_release(name, namespace)

# 3. If stuck in pending-upgrade, firmly rollback to the last confirmed baseline
helm_rollback_release(name, namespace, revision=LAST_GOOD_REVISION)
```

### Error: "Rendered Manifests Contain an Existing Resource"
**Cause:** K8s resources exist in the cluster outside of helm's track state.
**Troubleshooting Strategy:**
```python
# Diff the resources
helm_get_installation_plan(chart_name, values)
# The AI must alert the Human via explicit prompt that manual deletion of the unbound namespace resources is mandatory.
```

### Missing Value Schema Blocking Validation
**Error**: `helm_validate_values` fails citing missing chart contexts.
**Troubleshooting Strategy**:
```python
# Attempt to parse limits off the pure README documentation directly bypassing the schema lock
helm_get_chart_info(chart_name)
```
