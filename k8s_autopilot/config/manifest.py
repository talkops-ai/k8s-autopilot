"""Canonical manifest for every user-tunable configuration option.

This module is the single source of truth for the configuration surface:
the set of options, their types, defaults, env-var names, and DB keys.

No TOML resolution is performed here — the ``ConfigStore`` facade handles
resolution (DB → env → default) via ``ConfigStore.resolve(option)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

from k8s_autopilot.config.paths import ENV_PREFIX
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

# ── OptionKind ───────────────────────────────────────────


class OptionKind(Enum):
    """How an option's raw string value is coerced to a typed value."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    CHOICE = "choice"
    PATH = "path"
    SHELL_LIST = "shell_list"
    SECRET = "secret"
    JSON = "json"


_KIND_TYPE_LABEL: dict[OptionKind, str] = {
    OptionKind.BOOL: "bool",
    OptionKind.INT: "int",
    OptionKind.FLOAT: "float",
    OptionKind.STR: "str",
    OptionKind.CHOICE: "choice",
    OptionKind.PATH: "path",
    OptionKind.SHELL_LIST: "list[str]",
    OptionKind.SECRET: "secret",
    OptionKind.JSON: "json",
}

if _KIND_TYPE_LABEL.keys() != set(OptionKind):
    msg = "_KIND_TYPE_LABEL is missing an OptionKind entry"
    raise RuntimeError(msg)


# ── ConfigOption ─────────────────────────────────────────


@dataclass(frozen=True)
class ConfigOption:
    """One user-tunable configuration option with DB and env mappings."""

    key: str
    """Canonical dotted identifier (e.g. 'models.name')."""

    db_key: str
    """Key used in DB config_store and settings (e.g. 'MODEL')."""

    group: str
    """Human-readable grouping for the config UI."""

    summary: str
    """One-line description."""

    kind: OptionKind
    """How values are coerced."""

    default: Any = None
    """Typed default value."""

    env_var: str | None = None
    """Environment variable name (defaults to db_key if not set)."""

    settings_field: str | None = None
    """Settings dataclass attribute name."""

    redacted: bool = False
    """Mask value in display/logs."""

    choices: tuple[str, ...] | None = None
    """Valid values when kind is CHOICE."""

    @property
    def type_label(self) -> str:
        """Return a human-readable display label for the configuration entry type."""
        return _KIND_TYPE_LABEL[self.kind]

    @property
    def effective_env_var(self) -> str:
        """Return the effective environment variable name for this entry."""
        return self.env_var or self.db_key


# ── Coercion Engine ──────────────────────────────────────


def coerce_str_value(kind: OptionKind, raw: str) -> Any:
    """Coerce a raw string value to a typed Python object."""
    if kind == OptionKind.BOOL:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        return None
    if kind == OptionKind.INT:
        try:
            return int(raw.strip())
        except ValueError:
            return None
    if kind == OptionKind.FLOAT:
        try:
            return float(raw.strip())
        except ValueError:
            return None
    if kind in (OptionKind.STR, OptionKind.CHOICE, OptionKind.SECRET):
        return raw
    if kind == OptionKind.SHELL_LIST:
        return [cmd.strip() for cmd in raw.split(",") if cmd.strip()]
    if kind == OptionKind.PATH:
        return Path(raw.strip()).expanduser().resolve()
    if kind == OptionKind.JSON:
        import json

        try:
            return json.loads(raw)
        except Exception:
            return None
    return raw


def serialize_typed_value(kind: OptionKind, value: Any) -> str:
    """Serialize a typed Python value to a string for DB storage."""
    if value is None:
        return ""
    if kind == OptionKind.BOOL:
        return "true" if value else "false"
    if kind == OptionKind.SHELL_LIST:
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value)
    if kind == OptionKind.JSON:
        import json

        if isinstance(value, str):
            return value
        return json.dumps(value)
    return str(value)


def resolve_from_env(option: ConfigOption) -> tuple[Any, str] | None:
    """Resolve an option from environment variables."""
    env_name = option.effective_env_var
    prefixed = f"{ENV_PREFIX}{env_name}"

    for name in (prefixed, env_name):
        raw = os.environ.get(name)
        if raw is not None and raw != "":
            coerced = coerce_str_value(option.kind, raw)
            if coerced is not None:
                return coerced, f"env ({name})"

    return None


# ── Credential Options ───────────────────────────────────

_CREDENTIAL_REGISTRY: tuple[tuple[str, str, str, str], ...] = (
    ("credentials.openai", "OPENAI_API_KEY", "OpenAI API Key", "openai_api_key"),
    ("credentials.anthropic", "ANTHROPIC_API_KEY", "Anthropic API Key", "anthropic_api_key"),
    ("credentials.google", "GOOGLE_API_KEY", "Google Gemini API Key", "google_api_key"),
    ("credentials.groq", "GROQ_API_KEY", "Groq API Key", "groq_api_key"),
    ("credentials.deepseek", "DEEPSEEK_API_KEY", "DeepSeek API Key", "deepseek_api_key"),
    ("credentials.openrouter", "OPENROUTER_API_KEY", "OpenRouter API Key", "openrouter_api_key"),
    ("credentials.mistralai", "MISTRAL_API_KEY", "Mistral AI Key", "mistral_api_key"),
    ("credentials.fireworks", "FIREWORKS_API_KEY", "Fireworks Key", "fireworks_api_key"),
    ("credentials.together", "TOGETHER_API_KEY", "Together AI Key", "together_api_key"),
    ("credentials.xai", "XAI_API_KEY", "xAI Key", "xai_api_key"),
    ("credentials.cohere", "COHERE_API_KEY", "Cohere Key", "cohere_api_key"),
    ("credentials.perplexity", "PERPLEXITY_API_KEY", "Perplexity Key", "perplexity_api_key"),
    ("credentials.nvidia", "NVIDIA_API_KEY", "NVIDIA Key", "nvidia_api_key"),
    ("credentials.huggingface", "HUGGINGFACE_API_KEY", "Hugging Face Key", "huggingface_api_key"),
    ("credentials.bedrock", "AWS_ACCESS_KEY_ID", "AWS Access Key ID", "aws_access_key_id"),
    ("credentials.bedrock_secret", "AWS_SECRET_ACCESS_KEY", "AWS Secret Access Key", "aws_secret_access_key"),
    ("credentials.bedrock_region", "AWS_REGION", "AWS Region", "aws_region"),
    ("credentials.tavily", "TAVILY_API_KEY", "Tavily Search API Key", "tavily_api_key"),
    ("credentials.azure_openai", "AZURE_OPENAI_API_KEY", "Azure OpenAI Key", "azure_openai_api_key"),
    ("credentials.langchain", "LANGCHAIN_API_KEY", "LangSmith Tracing Key", "langchain_api_key"),
    ("credentials.loki", "LOKI_AUTH_TOKEN", "Loki Auth Token", "loki_auth_token"),
    ("credentials.tempo", "TEMPO_AUTH_HEADER", "Tempo Auth Header", "tempo_auth_header"),
    ("credentials.slack_bot", "SLACK_BOT_TOKEN", "Slack Bot Token", "slack_bot_token"),
    ("credentials.slack_secret", "SLACK_SIGNING_SECRET", "Slack Signing Secret", "slack_signing_secret"),
    ("credentials.argocd", "ARGOCD_AUTH_TOKEN", "ArgoCD Auth Token", "argocd_auth_token"),
)


def _credential_options() -> tuple[ConfigOption, ...]:
    return tuple(
        ConfigOption(
            key=key,
            db_key=db_key,
            group="Credentials",
            summary=summary,
            kind=OptionKind.SECRET,
            settings_field=field_name,
            redacted=True,
        )
        for key, db_key, summary, field_name in _CREDENTIAL_REGISTRY
    )


# ── Static Options ───────────────────────────────────────

_STATIC_OPTIONS: tuple[ConfigOption, ...] = (
    # ── Active Model State (OpsCode Unified Model) ────────
    ConfigOption(
        key="models.name",
        db_key="MODEL",
        group="Models",
        summary="Active primary LLM model spec (provider:model or model name)",
        kind=OptionKind.STR,
        default="gemini-3.7-flash",
        settings_field="model",
    ),
    ConfigOption(
        key="models.provider",
        db_key="MODEL_PROVIDER",
        group="Models",
        summary="Active primary LLM provider (e.g. google_genai, anthropic, openai)",
        kind=OptionKind.STR,
        default=None,
        settings_field="model_provider",
    ),
    ConfigOption(
        key="models.reasoning_effort",
        db_key="REASONING_EFFORT",
        group="Models",
        summary="Thinking / reasoning effort level",
        kind=OptionKind.CHOICE,
        default="medium",
        choices=("low", "medium", "high", "max"),
        settings_field="reasoning_effort",
    ),
    ConfigOption(
        key="models.context_limit",
        db_key="MODEL_CONTEXT_LIMIT",
        group="Models",
        summary="Model context window token limit",
        kind=OptionKind.INT,
        default=None,
        settings_field="model_context_limit",
    ),
    # ── Provider Base URLs & Vertex AI ────────────────────
    ConfigOption(
        key="models.google_genai_use_vertexai",
        db_key="GOOGLE_GENAI_USE_VERTEXAI",
        group="Models",
        summary="Use Google Vertex AI backend instead of Google AI Studio",
        kind=OptionKind.BOOL,
        default=False,
        settings_field="google_genai_use_vertexai",
    ),
    ConfigOption(
        key="models.google_cloud_project",
        db_key="GOOGLE_CLOUD_PROJECT",
        group="Models",
        summary="Google Cloud Project ID for Vertex AI",
        kind=OptionKind.STR,
        default=None,
        settings_field="google_cloud_project",
    ),
    ConfigOption(
        key="models.google_cloud_location",
        db_key="GOOGLE_CLOUD_LOCATION",
        group="Models",
        summary="Google Cloud Location for Vertex AI (e.g. us-central1)",
        kind=OptionKind.STR,
        default=None,
        settings_field="google_cloud_location",
    ),
    ConfigOption(
        key="models.gemini_base_url",
        db_key="GEMINI_API_BASE",
        group="Models",
        summary="Google Gemini API Base URL",
        kind=OptionKind.STR,
        settings_field="google_base_url",
    ),
    ConfigOption(
        key="models.openai_base_url",
        db_key="OPENAI_BASE_URL",
        group="Models",
        summary="OpenAI API Base URL",
        kind=OptionKind.STR,
        settings_field="openai_base_url",
    ),
    ConfigOption(
        key="models.anthropic_base_url",
        db_key="ANTHROPIC_BASE_URL",
        group="Models",
        summary="Anthropic API Base URL",
        kind=OptionKind.STR,
        settings_field="anthropic_base_url",
    ),
    ConfigOption(
        key="models.openrouter_base_url",
        db_key="OPENROUTER_API_BASE",
        group="Models",
        summary="OpenRouter API Base URL",
        kind=OptionKind.STR,
        settings_field="openrouter_base_url",
    ),
    # ── Security & Approval ───────────────────────────────
    ConfigOption(
        key="security.approval_mode",
        db_key="APPROVAL_MODE",
        group="Security",
        summary="Tool execution approval mode",
        kind=OptionKind.CHOICE,
        default="manual",
        choices=("manual", "auto", "yolo"),
        settings_field="approval_mode",
    ),
    ConfigOption(
        key="security.shell_allow_list",
        db_key="SHELL_ALLOW_LIST",
        group="Security",
        summary="Comma-separated shell commands allowed without approval",
        kind=OptionKind.SHELL_LIST,
        default=None,
        settings_field="shell_allow_list",
    ),
    # ── Kubernetes Environment ────────────────────────────
    ConfigOption(
        key="k8s.kubeconfig",
        db_key="KUBECONFIG",
        group="Kubernetes",
        summary="Path to kubeconfig file",
        kind=OptionKind.STR,
        default="",
        settings_field="kubeconfig",
    ),
    ConfigOption(
        key="k8s.context",
        db_key="KUBE_CONTEXT",
        group="Kubernetes",
        summary="Active Kubernetes context",
        kind=OptionKind.STR,
        default="",
        settings_field="kube_context",
    ),
    ConfigOption(
        key="k8s.namespace",
        db_key="KUBE_NAMESPACE",
        group="Kubernetes",
        summary="Default Kubernetes namespace",
        kind=OptionKind.STR,
        default="default",
        settings_field="kube_namespace",
    ),
    ConfigOption(
        key="k8s.sandbox_provider",
        db_key="SANDBOX_PROVIDER",
        group="Kubernetes",
        summary="Sandbox execution provider",
        kind=OptionKind.CHOICE,
        default="local",
        choices=("local", "docker", "kubernetes"),
        settings_field="sandbox_provider",
    ),
    # ── MCP Configuration ─────────────────────────────────
    ConfigOption(
        key="mcp.timeout",
        db_key="MCP_TIMEOUT",
        group="MCP",
        summary="MCP tool execution timeout in seconds",
        kind=OptionKind.INT,
        default=30,
        settings_field="mcp_timeout",
    ),
    ConfigOption(
        key="mcp.timeout_total",
        db_key="MCP_TIMEOUT_TOTAL",
        group="MCP",
        summary="Total MCP operation timeout in seconds",
        kind=OptionKind.FLOAT,
        default=600.0,
        settings_field="mcp_timeout_total",
    ),
    ConfigOption(
        key="mcp.timeout_connect",
        db_key="MCP_TIMEOUT_CONNECT",
        group="MCP",
        summary="MCP server connection timeout in seconds",
        kind=OptionKind.FLOAT,
        default=300.0,
        settings_field="mcp_timeout_connect",
    ),
    ConfigOption(
        key="mcp.default_transport",
        db_key="MCP_DEFAULT_TRANSPORT",
        group="MCP",
        summary="Default transport for new MCP servers",
        kind=OptionKind.CHOICE,
        default="sse",
        choices=("sse", "stdio", "http"),
        settings_field="mcp_default_transport",
    ),
    # ── Observability & Integrations Endpoints ────────────
    ConfigOption(
        key="integrations.prometheus_url",
        db_key="PROMETHEUS_BASE_URL",
        group="Integrations",
        summary="Prometheus server base URL",
        kind=OptionKind.STR,
        default="http://prometheus-operated.monitoring.svc:9090",
        settings_field="prometheus_base_url",
    ),
    ConfigOption(
        key="integrations.alertmanager_url",
        db_key="ALERTMANAGER_BASE_URL",
        group="Integrations",
        summary="Alertmanager server base URL",
        kind=OptionKind.STR,
        default="http://alertmanager-operated.monitoring.svc:9093",
        settings_field="alertmanager_base_url",
    ),
    ConfigOption(
        key="integrations.loki_url",
        db_key="LOKI_URL",
        group="Integrations",
        summary="Grafana Loki endpoint URL",
        kind=OptionKind.STR,
        default="http://host.docker.internal:3100",
        settings_field="loki_url",
    ),
    ConfigOption(
        key="integrations.tempo_url",
        db_key="TEMPO_BASE_URL",
        group="Integrations",
        summary="Grafana Tempo endpoint URL",
        kind=OptionKind.STR,
        default="http://localhost:3200",
        settings_field="tempo_base_url",
    ),
    ConfigOption(
        key="integrations.argocd_url",
        db_key="ARGOCD_SERVER_URL",
        group="Integrations",
        summary="ArgoCD API server URL",
        kind=OptionKind.STR,
        default=None,
        settings_field="argocd_server_url",
    ),
    ConfigOption(
        key="integrations.argocd_insecure",
        db_key="ARGOCD_INSECURE",
        group="Integrations",
        summary="Skip TLS verification for ArgoCD",
        kind=OptionKind.BOOL,
        default=True,
        settings_field="argocd_insecure",
    ),
    ConfigOption(
        key="integrations.traefik_url",
        db_key="TRAEFIK_API_URL",
        group="Integrations",
        summary="Traefik API / dashboard URL",
        kind=OptionKind.STR,
        default=None,
        settings_field="traefik_api_url",
    ),
    ConfigOption(
        key="integrations.prometheus_verify_ssl",
        db_key="PROMETHEUS_VERIFY_SSL",
        group="Integrations",
        summary="Verify SSL for Prometheus",
        kind=OptionKind.BOOL,
        default=False,
        settings_field="prometheus_verify_ssl",
    ),
    ConfigOption(
        key="integrations.alertmanager_verify_ssl",
        db_key="ALERTMANAGER_VERIFY_SSL",
        group="Integrations",
        summary="Verify SSL for Alertmanager",
        kind=OptionKind.BOOL,
        default=False,
        settings_field="alertmanager_verify_ssl",
    ),
    ConfigOption(
        key="integrations.tempo_verify_ssl",
        db_key="TEMPO_VERIFY_SSL",
        group="Integrations",
        summary="Verify SSL for Tempo",
        kind=OptionKind.BOOL,
        default=True,
        settings_field="tempo_verify_ssl",
    ),
    ConfigOption(
        key="integrations.otel_exporter_otlp_endpoint",
        db_key="OTEL_EXPORTER_OTLP_ENDPOINT",
        group="Integrations",
        summary="OpenTelemetry OTLP Collector endpoint",
        kind=OptionKind.STR,
        default=None,
        settings_field="otel_exporter_otlp_endpoint",
    ),
    ConfigOption(
        key="integrations.loki_timeout",
        db_key="LOKI_TIMEOUT",
        group="Integrations",
        summary="Loki query timeout in seconds",
        kind=OptionKind.INT,
        default=30,
        settings_field="loki_timeout",
    ),
    ConfigOption(
        key="integrations.loki_verify_ssl",
        db_key="LOKI_VERIFY_SSL",
        group="Integrations",
        summary="Verify SSL for Loki connection",
        kind=OptionKind.BOOL,
        default=True,
        settings_field="loki_verify_ssl",
    ),
    ConfigOption(
        key="integrations.loki_org_id",
        db_key="LOKI_ORG_ID",
        group="Integrations",
        summary="Loki tenant org ID header",
        kind=OptionKind.STR,
        default="talkops",
        settings_field="loki_org_id",
    ),
    # ── Compaction ────────────────────────────────────────
    ConfigOption(
        key="compaction.token_budget",
        db_key="COMPACTION_TOKEN_BUDGET",
        group="Compaction",
        summary="Token threshold triggering context compaction",
        kind=OptionKind.INT,
        default=100_000,
        settings_field="compaction_token_budget",
    ),
    ConfigOption(
        key="compaction.keep_messages",
        db_key="COMPACTION_KEEP_MESSAGES",
        group="Compaction",
        summary="Number of recent messages preserved across compaction",
        kind=OptionKind.INT,
        default=6,
        settings_field="compaction_keep_messages",
    ),
    ConfigOption(
        key="compaction.summary_model",
        db_key="COMPACTION_SUMMARY_MODEL",
        group="Compaction",
        summary="Model used for generating compaction summaries",
        kind=OptionKind.STR,
        default=None,
        settings_field="compaction_summary_model",
    ),
    # ── Rubric ────────────────────────────────────────────
    ConfigOption(
        key="rubric.grader_model",
        db_key="RUBRIC_GRADER_MODEL",
        group="Rubric",
        summary="Model used for grading evaluation rubrics",
        kind=OptionKind.STR,
        default=None,
        settings_field="rubric_grader_model",
    ),
    ConfigOption(
        key="rubric.max_iterations",
        db_key="RUBRIC_MAX_ITERATIONS",
        group="Rubric",
        summary="Maximum rubric refinement loop iterations",
        kind=OptionKind.INT,
        default=3,
        settings_field="rubric_max_iterations",
    ),
    # ── Slack ─────────────────────────────────────────────
    ConfigOption(
        key="slack.enabled",
        db_key="SLACK_ENABLED",
        group="Integrations",
        summary="Enable Slack bot integration",
        kind=OptionKind.BOOL,
        default=False,
        settings_field="slack_enabled",
    ),
    ConfigOption(
        key="slack.operation_mode",
        db_key="SLACK_OPERATION_MODE",
        group="Integrations",
        summary="Slack integration operation mode",
        kind=OptionKind.CHOICE,
        default="read",
        choices=("read", "write", "auto"),
        settings_field="slack_operation_mode",
    ),
    ConfigOption(
        key="slack.stream_buffer_size",
        db_key="SLACK_STREAM_BUFFER_SIZE",
        group="Integrations",
        summary="Slack streaming response buffer size",
        kind=OptionKind.INT,
        default=256,
        settings_field="slack_stream_buffer_size",
    ),
    # ── A2A Server ────────────────────────────────────────
    ConfigOption(
        key="a2a.server_host",
        db_key="A2A_SERVER_HOST",
        group="A2A",
        summary="A2A server listen host",
        kind=OptionKind.STR,
        default="0.0.0.0",
        settings_field="a2a_server_host",
    ),
    ConfigOption(
        key="a2a.server_port",
        db_key="A2A_SERVER_PORT",
        group="A2A",
        summary="A2A server listen port",
        kind=OptionKind.INT,
        default=8000,
        settings_field="a2a_server_port",
    ),
    ConfigOption(
        key="a2a.autopilot_mode",
        db_key="AUTOPILOT_MODE",
        group="A2A",
        summary="Autopilot operation mode (a2a, standalone)",
        kind=OptionKind.STR,
        default="a2a",
        settings_field="autopilot_mode",
    ),
    # ── System & Runtime ──────────────────────────────────
    ConfigOption(
        key="system.debug",
        db_key="DEBUG",
        group="System",
        summary="Enable debug logging and diagnostic endpoints",
        kind=OptionKind.BOOL,
        default=False,
        settings_field="debug",
    ),
    ConfigOption(
        key="system.log_level",
        db_key="LOG_LEVEL",
        group="System",
        summary="Logging severity level",
        kind=OptionKind.CHOICE,
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        settings_field="log_level",
    ),
    ConfigOption(
        key="system.log_mode",
        db_key="LOG_MODE",
        group="System",
        summary="Console output format (color text vs structured JSON)",
        kind=OptionKind.CHOICE,
        default="text",
        choices=("text", "json"),
        settings_field="log_mode",
    ),
    ConfigOption(
        key="system.log_to_console",
        db_key="LOG_TO_CONSOLE",
        group="System",
        summary="Enable logging to stderr",
        kind=OptionKind.BOOL,
        default=True,
        settings_field="log_to_console",
    ),
    ConfigOption(
        key="system.log_to_file",
        db_key="LOG_TO_FILE",
        group="System",
        summary="Enable logging to file",
        kind=OptionKind.BOOL,
        default=True,
        settings_field="log_to_file",
    ),
    ConfigOption(
        key="system.log_file",
        db_key="LOG_FILE",
        group="System",
        summary="Destination file for persistent logs",
        kind=OptionKind.STR,
        default="k8s_autopilot.log",
        settings_field="log_file",
    ),
    ConfigOption(
        key="system.environment",
        db_key="ENVIRONMENT",
        group="System",
        summary="Runtime environment name",
        kind=OptionKind.STR,
        default="development",
        settings_field="environment",
    ),
    ConfigOption(
        key="system.org_id",
        db_key="ORG_ID",
        group="System",
        summary="Organization ID for multi-tenant tenancy",
        kind=OptionKind.STR,
        default="default",
        settings_field="org_id",
    ),
    ConfigOption(
        key="system.org_name",
        db_key="ORG_NAME",
        group="System",
        summary="Organization display name",
        kind=OptionKind.STR,
        default="default_org",
        settings_field="org_name",
    ),
    ConfigOption(
        key="system.recursion_limit",
        db_key="RECURSION_LIMIT",
        group="System",
        summary="LangGraph graph step recursion limit",
        kind=OptionKind.INT,
        default=50,
        settings_field="recursion_limit",
    ),
    ConfigOption(
        key="system.postgres_uri",
        db_key="POSTGRES_URI",
        group="System",
        summary="PostgreSQL connection URI for backend state and checkpointing",
        kind=OptionKind.SECRET,
        default="",
        redacted=True,
        settings_field="postgres_uri",
    ),
    ConfigOption(
        key="system.checkpoint_backend",
        db_key="CHECKPOINT_BACKEND",
        group="System",
        summary="Checkpoint storage backend",
        kind=OptionKind.CHOICE,
        default="sqlite",
        choices=("sqlite", "postgres", "auto"),
        settings_field="checkpoint_backend",
    ),
    ConfigOption(
        key="system.extra_skills_dirs",
        db_key="EXTRA_SKILLS_DIRS",
        group="System",
        summary="Additional colon-separated directories containing skills",
        kind=OptionKind.STR,
        default=None,
        settings_field="extra_skills_dirs",
    ),
    # ── Tracing ───────────────────────────────────────────
    ConfigOption(
        key="tracing.langchain_tracing",
        db_key="LANGCHAIN_TRACING_V2",
        group="Tracing",
        summary="Enable LangSmith v2 tracing",
        kind=OptionKind.BOOL,
        default=False,
        settings_field="langchain_tracing",
    ),
    ConfigOption(
        key="tracing.langchain_project",
        db_key="LANGCHAIN_PROJECT",
        group="Tracing",
        summary="LangSmith project name for trace routing",
        kind=OptionKind.STR,
        default="k8s-autopilot",
        settings_field="langchain_project",
    ),
    ConfigOption(
        key="tracing.langchain_endpoint",
        db_key="LANGCHAIN_ENDPOINT",
        group="Tracing",
        summary="LangSmith endpoint URL",
        kind=OptionKind.STR,
        default="https://api.smith.langchain.com",
        settings_field="langchain_endpoint",
    ),
    # ── Search ────────────────────────────────────────────
    ConfigOption(
        key="search.tavily_max_results",
        db_key="TAVILY_MAX_RESULTS",
        group="Search",
        summary="Maximum web search results returned per query",
        kind=OptionKind.INT,
        default=5,
        settings_field="tavily_max_results",
    ),
    ConfigOption(
        key="search.web_search_timeout",
        db_key="WEB_SEARCH_TIMEOUT",
        group="Search",
        summary="Web search HTTP request timeout in seconds",
        kind=OptionKind.INT,
        default=30,
        settings_field="web_search_timeout",
    ),
)


# ── Public API ───────────────────────────────────────────


@lru_cache(maxsize=1)
def get_config_options() -> tuple[ConfigOption, ...]:
    """Return all configuration options (credentials first, then static)."""
    return _credential_options() + _STATIC_OPTIONS


@lru_cache(maxsize=1)
def _options_by_key() -> dict[str, ConfigOption]:
    return {opt.key: opt for opt in get_config_options()}


@lru_cache(maxsize=1)
def _options_by_db_key() -> dict[str, ConfigOption]:
    return {opt.db_key: opt for opt in get_config_options()}


def get_option(key: str) -> ConfigOption | None:
    """Lookup an option by its dotted key (e.g. 'models.name')."""
    return _options_by_key().get(key)


def get_option_by_db_key(db_key: str) -> ConfigOption | None:
    """Lookup an option by its DB/env key (e.g. 'MODEL')."""
    return _options_by_db_key().get(db_key)


def option_keys() -> tuple[str, ...]:
    """Return all canonical option keys in definition order."""
    return tuple(opt.key for opt in get_config_options())


def iter_groups(options: Iterable[ConfigOption] | None = None) -> list[str]:
    """Return distinct group names in first-seen order."""
    if options is None:
        options = get_config_options()
    groups: list[str] = []
    for opt in options:
        if opt.group not in groups:
            groups.append(opt.group)
    return groups
