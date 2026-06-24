"""
Subagent eval harness — tests tool selection with real LLM, no real MCP.

Architecture:
    Task message → Real LLM (subagent prompt) → Mock MCP tools (record calls) → Assert

Each mock tool matches the real MCP tool name/description but returns a scripted
response and records the call. The real LLM decides which tool to call based on
the full subagent prompt (routing tables, iron rules, safety rules, etc.).

This catches:
- Wrong tool selected (e.g. prom_uninstall_exporter instead of prom_install_exporter)
- Wrong args (e.g. wrong namespace, missing matchers)
- Read-only queries triggering state-modifying tools
- Missing HITL gates for dangerous tools

Usage::

    from tests.evals.subagent_eval_harness import SubagentEvalHarness

    harness = SubagentEvalHarness.for_prometheus()
    trace = await harness.run(
        "[READ-ONLY] Query CPU usage for service checkout",
    )
    assert "prom_query_instant" in trace.tool_names

Note: HITL middleware is NOT attached in evals because the agent would
GraphInterrupt before the tool call is recorded.  Instead, we verify that
the LLM *selects* the correct tool.  HITL gate existence is already tested
in the HITL layer (tests/hitl/).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool


# ═══════════════════════════════════════════════════════════════════════════
# ToolRecorder — mock tool that records calls
# ═══════════════════════════════════════════════════════════════════════════

class ToolRecorder:
    """Registry of mock tools that records every call for later assertion."""

    def __init__(self):
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.tools: List[StructuredTool] = []

    def add(
        self,
        name: str,
        description: str,
        response: str = "OK",
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> "ToolRecorder":
        """Register a mock tool that records calls.

        Args:
            name: Tool name (must match MCP tool name exactly).
            description: Tool description (visible to LLM for routing).
            response: Scripted response string to return.
            params: Optional parameter schema hints (name → type string).
                    Used to generate typed kwargs for cleaner LLM binding.
        """
        recorder = self  # closure

        # Build parameter annotations for the tool signature
        tool_params = params or {}

        # Create a simple async function with **kwargs to accept any args
        async def _mock_tool(**kwargs) -> str:
            recorder.calls.append((name, kwargs))
            return response

        # Set function name and doc for LangChain tool introspection
        _mock_tool.__name__ = name
        _mock_tool.__doc__ = description

        tool = StructuredTool.from_function(
            func=None,
            coroutine=_mock_tool,
            name=name,
            description=description,
        )
        self.tools.append(tool)
        return self

    def add_resource_reader(self, server_name: str) -> "ToolRecorder":
        """Add a mock read_mcp_resource tool."""
        return self.add(
            "read_mcp_resource",
            f"Read content of a specific MCP resource by URI (server: {server_name}). "
            "Use this to read state natively.",
            response='{"status": "ok", "data": []}',
        )

    @property
    def tool_names_called(self) -> List[str]:
        return [name for name, _ in self.calls]

    def get_calls_for(self, tool_name: str) -> List[Dict[str, Any]]:
        return [args for name, args in self.calls if name == tool_name]

    def reset(self):
        self.calls.clear()


# ═══════════════════════════════════════════════════════════════════════════
# SubagentTrace — eval result
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SubagentTrace:
    """Result of a subagent eval run."""

    tool_calls: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    """All tool calls made: [(tool_name, args), ...]"""

    tool_names: List[str] = field(default_factory=list)
    """Ordered list of tool names called."""

    final_message: str = ""
    """Final AI message content."""

    messages: List[Any] = field(default_factory=list)
    """All messages in the trajectory."""

    hitl_triggered: bool = False
    """Whether request_human_input was called."""

    error: Optional[str] = None
    """Error message if the agent failed."""

    @property
    def first_tool(self) -> Optional[str]:
        return self.tool_names[0] if self.tool_names else None

    @property
    def called_state_modifying(self) -> bool:
        """Whether any state-modifying tool was called (vs read-only)."""
        return self.hitl_triggered or "request_human_input" in self.tool_names


# ═══════════════════════════════════════════════════════════════════════════
# SubagentEvalHarness — builds real subagent with mock tools
# ═══════════════════════════════════════════════════════════════════════════

class SubagentEvalHarness:
    """Harness for running subagent evals with real LLM + mock MCP tools.

    The harness builds a LangChain agent graph using:
    - The REAL system prompt (from prompt_sections.py)
    - The REAL LLM (from Config)
    - Mock MCP tools (StructuredTool wrappers that record calls)
    - request_human_input tool (for HITL testing)
    """

    def __init__(
        self,
        name: str,
        system_prompt: str,
        recorder: ToolRecorder,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.recorder = recorder

    async def run(
        self,
        task_message: str,
        *,
        context: Optional[str] = None,
        timeout: int = 90,
        thread_id: Optional[str] = None,
    ) -> SubagentTrace:
        """Run a single task through the subagent with real LLM.

        Args:
            task_message: The task string (as the coordinator would send it).
            context: Optional system message with pre-supplied context.
            timeout: Max seconds before aborting.
            thread_id: Optional thread ID for checkpointer.

        Returns:
            SubagentTrace with tool calls, messages, and final output.
        """
        from langchain.agents import create_agent
        from langgraph.checkpoint.memory import MemorySaver
        from k8s_autopilot.config.config import Config
        from k8s_autopilot.utils.llm import create_model
        from k8s_autopilot.core.hitl.tools import create_hitl_tools

        self.recorder.reset()

        config = Config()
        model = create_model(config.get_llm_deepagent_config())

        # Combine mock MCP tools + HITL tools
        tools = list(self.recorder.tools) + create_hitl_tools()

        agent = create_agent(
            model=model,
            tools=tools,
            middleware=[],
            system_prompt=self.system_prompt,
            name=self.name,
            checkpointer=MemorySaver(),
        )

        # Build initial state
        messages = []
        if context:
            messages.append(SystemMessage(content=context))
        messages.append(HumanMessage(content=task_message))

        run_config = {
            "configurable": {
                "thread_id": thread_id or f"eval-{self.name}-{id(task_message)}",
            }
        }

        trace = SubagentTrace()

        try:
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": messages}, config=run_config),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            trace.error = "Timeout"
            # Try to get partial state
            try:
                state = agent.get_state(run_config)
                result = state.values if hasattr(state, "values") else {}
            except Exception:
                result = {}
        except Exception as e:
            e_name = type(e).__name__
            if "GraphInterrupt" in e_name or "interrupt" in e_name.lower():
                trace.hitl_triggered = True
                try:
                    state = agent.get_state(run_config)
                    result = state.values if hasattr(state, "values") else {}
                except Exception:
                    result = {}
            else:
                trace.error = f"{e_name}: {str(e)[:200]}"
                result = {}

        # Extract trajectory
        all_messages = (result or {}).get("messages", [])
        for msg in all_messages:
            trace.messages.append(msg)
            if isinstance(msg, AIMessage):
                content = msg.content
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") == "text" and part.get("text"):
                                text_parts.append(part["text"])
                            elif "text" in part and part.get("type") != "thinking":
                                text_parts.append(str(part["text"]))
                        elif isinstance(part, str):
                            text_parts.append(part)
                    content = " ".join(text_parts).strip()
                trace.final_message = str(content or "")

                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        name = tc.get("name", "")
                        args = tc.get("args", {})
                        trace.tool_calls.append((name, args))
                        trace.tool_names.append(name)
                        if name == "request_human_input":
                            trace.hitl_triggered = True

        # Also capture calls from the recorder (actual executions)
        # The recorder captures what was actually executed (vs what was planned)
        if self.recorder.calls and not trace.tool_calls:
            trace.tool_calls = self.recorder.calls
            trace.tool_names = self.recorder.tool_names_called

        return trace

    # ═══════════════════════════════════════════════════════════════════════
    # Factory methods — one per observability domain
    # ═══════════════════════════════════════════════════════════════════════

    @classmethod
    def for_prometheus(cls) -> "SubagentEvalHarness":
        """Build harness for prometheus-operator with mock Prometheus MCP tools."""
        from k8s_autopilot.core.agents.observability.prompt_sections import (
            compose_subagent_prompt,
        )

        recorder = ToolRecorder()

        # Read-only tools
        recorder.add("prom_query_instant", "Run an instant PromQL query against Prometheus.", '{"status":"success","data":{"resultType":"vector","result":[{"metric":{"__name__":"up"},"value":[1717600000,"1"]}]}}')
        recorder.add("prom_query_range", "Run a range PromQL query against Prometheus.", '{"status":"success","data":{"resultType":"matrix","result":[]}}')
        recorder.add("prom_validate_promql", "Validate a PromQL expression without executing it.", '{"valid": true}')
        recorder.add("prom_explore_labels", "Explore metric label names and values.", '{"labels": ["__name__", "job", "instance"]}')
        recorder.add("prom_test_endpoint", "Test whether a metrics endpoint is reachable and returning valid Prometheus exposition format.", '{"reachable": true, "metrics_count": 42}')
        recorder.add("prom_recommend_instrumentation", "Recommend instrumentation for a service.", '{"recommendation": "Use client_golang"}')
        recorder.add("prom_recommend_exporter", "Recommend a Prometheus exporter for a given technology.", '{"exporter": "node-exporter", "helm_chart": "prometheus-community/prometheus-node-exporter"}')
        recorder.add("prom_describe_alert_rule", "Describe what an alerting rule does in plain language.", '{"description": "Fires when instance is down for 5m"}')
        recorder.add("prom_analyze_firing_history", "Analyze the firing history of an alert rule.", '{"fires": 3, "last_fired": "2024-01-01"}')
        recorder.add("prom_draft_alert_rule", "Draft a new Prometheus alerting rule from a description.", '{"rule": "alert: HighCPU\\nexpr: rate(cpu[5m]) > 0.9"}')
        recorder.add("prom_tune_alert_rule", "Tune thresholds for an existing alert rule.", '{"tuned_rule": "updated"}')
        recorder.add("prom_verify_exporter", "Verify that an exporter is installed and scraping.", '{"installed": true, "up": 1}')

        # State-modifying tools (HITL-gated in production)
        recorder.add("prom_install_exporter", "Install a Prometheus exporter into the cluster.", '{"status": "installed", "name": "node-exporter", "namespace": "monitoring"}')
        recorder.add("prom_uninstall_exporter", "Uninstall a Prometheus exporter from the cluster.", '{"status": "uninstalled"}')
        recorder.add("prom_apply_servicemonitor", "Apply a ServiceMonitor CRD to wire a service to Prometheus.", '{"status": "applied", "name": "checkout-monitor"}')
        recorder.add("prom_upsert_rule_group", "Create or update a Prometheus rule group (alerting or recording rules).", '{"status": "upserted", "group": "test-rules"}')
        recorder.add("prom_manage_file_sd", "Manage file-based service discovery targets.", '{"status": "updated"}')
        recorder.add("prom_configure_remote_write", "Generate or apply remote_write configuration.", '{"status": "configured"}')

        recorder.add_resource_reader("prometheus-mcp-server")

        return cls(
            name="prometheus-operator",
            system_prompt=compose_subagent_prompt("prometheus"),
            recorder=recorder,
        )

    @classmethod
    def for_alertmanager(cls) -> "SubagentEvalHarness":
        """Build harness for alertmanager-operator with mock Alertmanager MCP tools."""
        from k8s_autopilot.core.agents.observability.prompt_sections import (
            compose_subagent_prompt,
        )

        recorder = ToolRecorder()

        # Read-only tools
        recorder.add("am_list_alerts", "List active alerts from Alertmanager with optional filtering.", '[{"labels":{"alertname":"HighCPU","severity":"critical"},"status":"firing"}]')
        recorder.add("am_list_alert_groups", "List alert groups from Alertmanager.", '{"groups": []}')
        recorder.add("am_summarize_oncall", "Summarize on-call alert status.", '{"critical": 2, "warning": 5, "total": 7}')
        recorder.add("am_explain_routing", "Explain how a specific alert is routed through the routing tree.", '{"receiver": "slack-critical", "route": "severity=critical"}')
        recorder.add("am_audit_default_route", "Audit alerts hitting the default route (potential misrouting).", '{"misrouted_count": 0}')
        recorder.add("am_list_recent_changes", "List recent silence create/expire activity.", '{"changes": []}')
        recorder.add("am_preview_silence", "Preview the blast radius of a silence before creating it.", '{"affected_alerts": 3, "matchers": [{"name":"alertname","value":"HighCPU"}]}')
        recorder.add("am_validate_silence_policy", "Validate a silence against organizational policies.", '{"valid": true}')
        recorder.add("am_list_silences", "List active/expired/pending silences.", '{"silences": []}')

        # State-modifying tools (HITL-gated in production)
        recorder.add("am_create_silence", "Create a new silence in Alertmanager.", '{"silenceID": "abc-123", "status": "active"}')
        recorder.add("am_update_silence", "Update (extend) an existing silence.", '{"status": "updated"}')
        recorder.add("am_expire_silence", "Expire an active silence.", '{"status": "expired"}')
        recorder.add("am_push_test_alert", "Push a test alert to Alertmanager to verify routing.", '{"status": "pushed"}')
        recorder.add("am_silence_alert", "Quick-silence helper: create a silence from an alert.", '{"silenceID": "def-456"}')

        recorder.add_resource_reader("alertmanager-mcp-server")

        return cls(
            name="alertmanager-operator",
            system_prompt=compose_subagent_prompt("alertmanager"),
            recorder=recorder,
        )

    @classmethod
    def for_loki(cls) -> "SubagentEvalHarness":
        """Build harness for loki-operator with mock Loki MCP tools (all read-only)."""
        from k8s_autopilot.core.agents.observability.prompt_sections import (
            compose_subagent_prompt,
        )

        recorder = ToolRecorder()

        recorder.add("get_cluster_labels", "Discover available log labels across all streams.", '{"labels": ["namespace", "pod", "container", "service_name", "trace_id"]}')
        recorder.add("get_label_values", "List values for a specific label.", '{"values": ["checkout", "payment", "frontend"]}')
        recorder.add("get_active_series", "Validate a log stream selector and check cardinality.", '{"active_series": 42}')
        recorder.add("get_detected_fields", "Discover structured fields in log lines.", '{"fields": ["level", "msg", "caller", "trace_id"]}')
        recorder.add("get_log_patterns", "Discover common log line patterns.", '{"patterns": [{"pattern": "<_> level=error <_>", "count": 150}]}')
        recorder.add("get_query_stats", "Estimate the cost of a LogQL query before running it.", '{"bytes_processed": 1048576, "entries_scanned": 5000}')
        recorder.add("execute_logql_instant", "Execute a LogQL instant query (scalar result).", '{"data": {"result": [{"value": [1717600000, "42"]}]}}')
        recorder.add("execute_logql_query", "Execute a LogQL range query (log lines or metric results).", '{"data": {"result": [{"stream":{"service_name":"checkout"},"values":[["1717600000","level=error msg=timeout"]]}]}}')

        recorder.add_resource_reader("loki-mcp-server")

        return cls(
            name="loki-operator",
            system_prompt=compose_subagent_prompt("loki"),
            recorder=recorder,
        )

    @classmethod
    def for_opentelemetry(cls) -> "SubagentEvalHarness":
        """Build harness for opentelemetry-operator with mock OTel MCP tools."""
        from k8s_autopilot.core.agents.observability.prompt_sections import (
            compose_subagent_prompt,
        )

        recorder = ToolRecorder()

        # Read-only tools
        recorder.add("otel_list_collectors", "List OpenTelemetry Collector instances.", '[{"name":"default","namespace":"observability","mode":"deployment"}]')
        recorder.add("otel_get_collector", "Get detailed config for a specific collector.", '{"name":"default","config":{"receivers":{"otlp":{}}}}')
        recorder.add("otel_list_instrumented_services", "List services with auto-instrumentation annotations.", '[{"name":"checkout","namespace":"default","language":"java"}]')
        recorder.add("otel_lookup_instrumentation", "Look up auto-instrumentation support for a language.", '{"language":"java","supported":true,"agent":"opentelemetry-javaagent"}')
        recorder.add("otel_validate_k8sattributes_order", "Validate processor ordering in collector pipeline.", '{"valid":true}')
        recorder.add("otel_check_filelog_safety", "Check filelog receiver safety configuration.", '{"safe":true}')
        recorder.add("otel_inspect_target_allocator_state", "Inspect Target Allocator state.", '{"status":"healthy"}')
        recorder.add("otel_recommend_collector_topology", "Recommend collector topology for a use case.", '{"recommendation":"DaemonSet for node metrics, Deployment for centralized traces"}')
        recorder.add("otel_detect_cardinality", "Detect high-cardinality attributes in telemetry.", '{"high_cardinality_attrs":[],"risk":"low"}')
        recorder.add("otel_analyze_ebpf_footprint", "Analyze eBPF security footprint.", '{"footprint":"minimal"}')
        recorder.add("otel_inspect_sampling_configuration", "Inspect current sampling config.", '{"strategy":"probabilistic","rate":0.1}')
        recorder.add("otel_inspect_spanmetrics_config", "Inspect SpanMetrics connector config.", '{"enabled":false}')

        # State-modifying tools (HITL-gated in production)
        recorder.add("otel_provision_collector", "Provision a new OpenTelemetry Collector instance.", '{"status":"provisioned","name":"new-collector","namespace":"observability"}')
        recorder.add("otel_patch_collector", "Patch an existing collector's CRD spec.", '{"status":"patched"}')
        recorder.add("otel_patch_instrumentation", "Patch an Instrumentation CRD.", '{"status":"patched"}')
        recorder.add("otel_annotate_deployment", "Annotate a Deployment for auto-instrumentation injection.", '{"status":"annotated","deployment":"checkout","namespace":"default"}')
        recorder.add("otel_toggle_sampling_strategy", "Toggle sampling strategy (head/tail).", '{"status":"updated","strategy":"tail"}')
        recorder.add("otel_enable_spanmetrics_for_service", "Enable SpanMetrics connector for a service.", '{"status":"enabled"}')

        recorder.add_resource_reader("opentelemetry-mcp-server")

        return cls(
            name="opentelemetry-operator",
            system_prompt=compose_subagent_prompt("opentelemetry"),
            recorder=recorder,
        )

    @classmethod
    def for_tempo(cls) -> "SubagentEvalHarness":
        """Build harness for tempo-operator with mock Tempo MCP tools."""
        from k8s_autopilot.core.agents.observability.prompt_sections import (
            compose_subagent_prompt,
        )

        recorder = ToolRecorder()

        # Read-only tools
        recorder.add("tempo_list_backends", "List Tempo backends.", '[{"id":"default","healthy":true}]')
        recorder.add("tempo_get_backend", "Get details for a specific Tempo backend.", '{"id":"default","version":"2.4.0"}')
        recorder.add("tempo_get_query_policies", "Get current query guardrails.", '{"max_duration":"720h","max_bytes_per_tag":"5000000"}')
        recorder.add("tempo_get_attribute_names", "Discover trace attribute names.", '{"attributes":["service.name","http.method","http.status_code"]}')
        recorder.add("tempo_get_attribute_values", "Get values for a trace attribute.", '{"values":["checkout","payment","frontend"]}')
        recorder.add("tempo_get_k8s_attribute_map", "Get K8s attribute naming conventions.", '{"map":{"pod":"k8s.pod.name"}}')
        recorder.add("tempo_traceql_search", "Search traces using TraceQL or filters.", '{"traces":[{"traceID":"abc123","rootServiceName":"checkout","durationMs":1500}]}')
        recorder.add("tempo_get_trace", "Get a single trace by trace ID.", '{"traceID":"abc123","spans":[{"spanID":"s1","operationName":"HTTP GET"}]}')
        recorder.add("tempo_summarize_trace", "Summarize a trace: critical path, errors, root cause.", '{"critical_path":["checkout→payment"],"errors":[],"root_cause":"none"}')
        recorder.add("tempo_find_related_traces", "Find traces related to a given trace.", '{"related":[]}')
        recorder.add("tempo_compare_traces", "Compare two traces (diff).", '{"diff":"no differences"}')
        recorder.add("tempo_traceql_metrics_range", "TraceQL metrics range query (time series).", '{"series":[]}')
        recorder.add("tempo_traceql_metrics_instant", "TraceQL metrics instant query.", '{"value": 42}')
        recorder.add("tempo_get_exemplar_traces", "Get exemplar traces from metrics.", '{"exemplars":[]}')
        recorder.add("tempo_get_trace_from_log", "Get a trace from a log line containing a trace_id.", '{"traceID":"from-log-123"}')
        recorder.add("tempo_get_diagnostics", "Get Tempo backend diagnostics.", '{"healthy":true,"ingester":"ok","compactor":"ok"}')
        recorder.add("tempo_get_service_dependencies", "Get service dependency topology.", '{"edges":[{"source":"checkout","target":"payment"}]}')
        recorder.add("tempo_list_operator_crs", "List Tempo Operator CRs (TempoStack/TempoMonolithic).", '[]')
        recorder.add("tempo_get_operator_cr", "Inspect a specific Tempo Operator CR.", '{"kind":"TempoStack","name":"prod","status":"ready"}')
        recorder.add("tempo_generate_alerting_expression", "Generate a PromQL alerting expression from trace data.", '{"promql":"histogram_quantile(0.99, rate(traces_spanmetrics_duration_bucket[5m])) > 2","yaml_snippet":"..."}')

        # State-modifying tools (HITL-gated in production)
        recorder.add("tempo_create_operator_cr", "Create a Tempo Operator CR (TempoStack or TempoMonolithic).", '{"status":"created","name":"prod","kind":"TempoStack"}')
        recorder.add("tempo_patch_operator_cr", "Patch an existing Tempo Operator CR.", '{"status":"patched"}')

        recorder.add_resource_reader("tempo-mcp-server")

        return cls(
            name="tempo-operator",
            system_prompt=compose_subagent_prompt("tempo"),
            recorder=recorder,
        )

    @classmethod
    def for_argocd(cls) -> "SubagentEvalHarness":
        """Build harness for argocd-onboarder with mock ArgoCD MCP tools."""
        from k8s_autopilot.core.agents.app_operator.subagents import (
            ARGOCD_ONBOARDER_PROMPT,
        )

        recorder = ToolRecorder()

        # Read-only tools
        recorder.add("list_applications", "List applications in ArgoCD.", '[{"metadata":{"name":"checkout"}}]')
        recorder.add("get_application_details", "Get application details.", '{"spec":{"project":"default"}}')
        recorder.add("get_application_events", "Get application events.", '[]')
        recorder.add("get_application_logs", "View application logs.", '{"logs":[]}')
        recorder.add("get_sync_status", "Get sync status.", '{"status":"Synced"}')
        recorder.add("get_application_diff", "Get application diff.", '{"diff":""}')
        recorder.add("list_repositories", "List repositories.", '[]')
        recorder.add("get_repository", "Get repository details.", '{"repo":"https://github.com/foo"}')
        recorder.add("list_projects", "List projects.", '[]')
        recorder.add("get_project", "Get project details.", '{"metadata":{"name":"default"}}')

        # State-modifying tools
        recorder.add("create_application", "Create an application.", '{"status":"created"}')
        recorder.add("update_application", "Update an application.", '{"status":"updated"}')
        recorder.add("sync_application", "Sync an application.", '{"status":"synced"}')
        recorder.add("delete_application", "Delete an application.", '{"status":"deleted"}')
        recorder.add("delete_project", "Delete a project.", '{"status":"deleted"}')
        recorder.add("delete_repository", "Delete a repository.", '{"status":"deleted"}')
        recorder.add("onboard_repository_https", "Onboard a repository via HTTPS.", '{"status":"onboarded"}')
        recorder.add("onboard_repository_ssh", "Onboard a repository via SSH.", '{"status":"onboarded"}')
        recorder.add("create_project", "Create a project.", '{"status":"created"}')

        recorder.add_resource_reader("argocd_mcp_server")

        return cls(
            name="argocd-onboarder",
            system_prompt=ARGOCD_ONBOARDER_PROMPT,
            recorder=recorder,
        )

    @classmethod
    def for_argo_rollouts(cls) -> "SubagentEvalHarness":
        """Build harness for argo-rollouts-onboarder with mock Argo Rollouts MCP tools."""
        from k8s_autopilot.core.agents.app_operator.subagents import (
            ARGO_ROLLOUTS_ONBOARDER_PROMPT,
        )

        recorder = ToolRecorder()

        # Write tools
        recorder.add("argo_delete_rollout", "Delete rollout.", '{"status":"deleted"}')
        recorder.add("argo_delete_experiment", "Delete experiment.", '{"status":"deleted"}')
        recorder.add("convert_deployment_to_rollout", "Convert deployment to rollout.", '{"status":"converted"}')
        recorder.add("convert_rollout_to_deployment", "Convert rollout to deployment.", '{"status":"converted"}')
        recorder.add("argo_manage_rollout_lifecycle", "Manage rollout lifecycle.", '{"status":"managed"}')
        recorder.add("argo_manage_legacy_deployment", "Manage legacy deployment.", '{"status":"managed"}')
        recorder.add("argo_create_rollout", "Create rollout.", '{"status":"created"}')
        recorder.add("argo_configure_analysis_template", "Configure analysis template.", '{"status":"configured"}')
        recorder.add("create_stable_canary_services", "Create stable/canary services.", '{"status":"created"}')
        recorder.add("argo_update_rollout", "Update rollout.", '{"status":"updated"}')

        recorder.add_resource_reader("argo_rollout_mcp_server")

        return cls(
            name="argo-rollouts-onboarder",
            system_prompt=ARGO_ROLLOUTS_ONBOARDER_PROMPT,
            recorder=recorder,
        )

    @classmethod
    def for_traefik(cls) -> "SubagentEvalHarness":
        """Build harness for traefik-edge-router with mock Traefik MCP tools."""
        from k8s_autopilot.core.agents.app_operator.subagents import (
            TRAEFIK_EDGE_ROUTER_PROMPT,
        )

        recorder = ToolRecorder()

        recorder.add("traefik_manage_weighted_routing", "Manage weighted routing.", '{"status":"managed"}')
        recorder.add("traefik_manage_simple_route", "Manage simple route.", '{"status":"managed"}')
        recorder.add("traefik_generate_routing_manifest", "Generate routing manifest.", '{"status":"generated"}')

        recorder.add_resource_reader("traefik_mcp_server")

        return cls(
            name="traefik-edge-router",
            system_prompt=TRAEFIK_EDGE_ROUTER_PROMPT,
            recorder=recorder,
        )
