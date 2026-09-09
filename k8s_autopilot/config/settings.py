"""Global settings, environment detection, and bootstrap for K8s Autopilot.

The ``Settings`` dataclass holds all runtime configuration.
In parity with the OpsCode architecture, it maintains a single active model
configuration (model_name, model_provider, reasoning_effort, context_limit)
rather than hardcoded duplicate tier models.

It can be hydrated from:
1. **ConfigStore** (primary): ``Settings.from_store(store)`` resolves settings
   through the DB → env → manifest defaults chain.
2. **Environment only** (bootstrap fallback): ``Settings.from_env()`` for
   early startup before the DB is available.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import threading
from typing import Any

from k8s_autopilot.config import paths
from k8s_autopilot.config.paths import (
    DOTENV_DENIED_ENV_KEYS,
    ENV_PREFIX,
)
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


# ── Bootstrap State ──────────────────────────────────────


@dataclass
class _BootstrapState:
    """Internal state tracker for settings initialization lifecycle."""

    done: bool = False
    start_path: Path | None = None


_bootstrap_state = _BootstrapState()
_bootstrap_lock = threading.Lock()


def sync_mcp_env_aliases() -> None:
    """Harmonize environment variable aliases for MCP server compatibility.

    Ensures bidirectional synchronization between standard config keys and MCP server keys:
    - PROMETHEUS_BASE_URL <-> PROMETHEUS_URL (used by argo-rollout-mcp-server)
    - KUBECONFIG <-> K8S_KUBECONFIG (used by argo-rollout and traefik mcp servers)
    """
    # Prometheus: PROMETHEUS_BASE_URL <-> PROMETHEUS_URL
    p_base = os.environ.get("PROMETHEUS_BASE_URL")
    p_url = os.environ.get("PROMETHEUS_URL")
    if p_base and not p_url:
        os.environ["PROMETHEUS_URL"] = p_base
    elif p_url and not p_base:
        os.environ["PROMETHEUS_BASE_URL"] = p_url

    # Kubeconfig: KUBECONFIG <-> K8S_KUBECONFIG
    kube = os.environ.get("KUBECONFIG")
    k8s_kube = os.environ.get("K8S_KUBECONFIG")
    if kube and not k8s_kube:
        os.environ["K8S_KUBECONFIG"] = kube
    elif k8s_kube and not kube:
        os.environ["KUBECONFIG"] = k8s_kube


def _ensure_bootstrap() -> None:
    """One-time bootstrap: dotenv loading. Idempotent and thread-safe."""
    if _bootstrap_state.done:
        return
    with _bootstrap_lock:
        if _bootstrap_state.done:
            return
        try:
            _bootstrap_state.start_path = Path.cwd()
            _load_dotenv(start_path=_bootstrap_state.start_path)
            sync_mcp_env_aliases()
        except Exception:
            logger.exception("Bootstrap failed; proceeding with env as-is.")
        finally:
            _bootstrap_state.done = True


# ── Env-Var Resolution ───────────────────────────────────


def resolve_env_var(name: str, fallback_names: tuple[str, ...] = ()) -> str | None:
    """Resolve env var with K8S_AUTOPILOT_ prefix priority and Settings fallback."""
    _ensure_bootstrap()
    prefixed = f"{ENV_PREFIX}{name}"
    val = os.environ.get(prefixed)
    if val is not None and val != "":
        return val

    val = os.environ.get(name)
    if val is not None and val != "":
        return val

    for alt in fallback_names:
        val = os.environ.get(alt)
        if val is not None and val != "":
            return val

    # Fallback to _settings singleton if populated
    if _settings is not None:
        field_name = name.lower()
        if hasattr(_settings, field_name):
            field_val = getattr(_settings, field_name)
            if field_val is not None and str(field_val).strip() != "":
                return str(field_val)

    return None


# ── Dotenv Loading ───────────────────────────────────────


def _load_dotenv(*, start_path: Path | None = None, refresh_loaded: bool = False) -> None:
    """Load .env files: project-level (walk-up), then global ~/.k8s_autopilot/.env."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return

    search = (start_path or Path.cwd()).expanduser().resolve()
    project_env: Path | None = None
    for parent in [search, *search.parents]:
        candidate = parent / ".env"
        if candidate.is_file():
            project_env = candidate
            break

    loaded_vals: dict[str, str | None] = {}

    # Global ~/.k8s_autopilot/.env
    global_env = paths.GLOBAL_ENV_PATH
    if global_env.is_file():
        with contextlib.suppress(Exception):
            loaded_vals.update(dotenv_values(global_env))

    # Project/CWD .env (higher priority)
    if project_env:
        with contextlib.suppress(Exception):
            loaded_vals.update(dotenv_values(project_env))

    # Filter out denied keys
    for key in DOTENV_DENIED_ENV_KEYS:
        if key in loaded_vals:
            logger.warning("Denied .env key in dotenv file: %s", key)
            loaded_vals.pop(key, None)

    # Apply to os.environ
    for k, v in loaded_vals.items():
        if v is not None and (refresh_loaded or k not in os.environ):
            os.environ[k] = v


def parse_shell_allow_list(value: str | None) -> list[str] | None:
    """Parse comma-separated list of allowed shell commands."""
    if value is None:
        return None
    if not value.strip():
        return []
    return [cmd.strip() for cmd in value.split(",") if cmd.strip()]


# ── Settings Dataclass ───────────────────────────────────


@dataclass
class Settings:
    """Application settings resolved from store, env, or defaults.

    Maintains one clean active model state matching OpsCode.
    """

    # ── Active Model State ────────────────────────────────
    model: str | None = None
    model_name: str | None = None
    model_provider: str | None = None
    reasoning_effort: str = "medium"
    model_context_limit: int | None = None
    model_unsupported_modalities: frozenset[str] = field(default_factory=frozenset)

    # ── Assistant / Agent Identity ────────────────────────
    assistant_id: str = "k8s-autopilot"

    # ── Approval & Security ───────────────────────────────
    approval_mode: str = "manual"
    shell_allow_list: list[str] | None = None

    # ── Kubernetes Environment ────────────────────────────
    kubeconfig: str = ""
    kube_context: str = ""
    kube_namespace: str = "default"
    sandbox_provider: str = "local"

    # ── Provider Credentials ──────────────────────────────
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    groq_api_key: str | None = None
    deepseek_api_key: str | None = None
    baseten_api_key: str | None = None
    watsonx_apikey: str | None = None
    litellm_api_key: str | None = None
    model_api_key: str | None = None
    tavily_api_key: str | None = None
    azure_openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    mistral_api_key: str | None = None
    fireworks_api_key: str | None = None
    together_api_key: str | None = None
    xai_api_key: str | None = None
    cohere_api_key: str | None = None
    perplexity_api_key: str | None = None
    nvidia_api_key: str | None = None
    huggingface_api_key: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str | None = None
    langchain_api_key: str | None = None
    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    argocd_auth_token: str | None = None
    github_personal_access_token: str | None = None
    loki_auth_token: str | None = None
    tempo_auth_header: str | None = None

    # ── Provider Base URLs & Vertex AI ────────────────────
    google_genai_use_vertexai: bool = False
    google_cloud_project: str | None = None
    google_cloud_location: str | None = None
    openai_base_url: str | None = None
    anthropic_base_url: str | None = None
    google_base_url: str | None = None
    groq_base_url: str | None = None
    deepseek_base_url: str | None = None
    openrouter_base_url: str | None = None
    fireworks_base_url: str | None = None
    together_base_url: str | None = None
    xai_base_url: str | None = None
    mistral_base_url: str | None = None
    cohere_base_url: str | None = None
    perplexity_base_url: str | None = None
    nvidia_base_url: str | None = None
    baseten_base_url: str | None = None

    # ── MCP Configuration ─────────────────────────────────
    mcp_timeout: int = 30
    mcp_timeout_total: float = 600.0
    mcp_timeout_connect: float = 300.0
    mcp_default_transport: str = "sse"

    # ── Observability & Integrations Endpoints ────────────
    prometheus_base_url: str = "http://prometheus-operated.monitoring.svc:9090"
    prometheus_verify_ssl: bool = False
    alertmanager_base_url: str = "http://alertmanager-operated.monitoring.svc:9093"
    alertmanager_verify_ssl: bool = False
    loki_url: str = "http://host.docker.internal:3100"
    tempo_base_url: str = "http://localhost:3200"
    tempo_verify_ssl: bool = True
    otel_exporter_otlp_endpoint: str | None = None
    argocd_server_url: str | None = None
    argocd_insecure: bool = True
    traefik_api_url: str | None = None
    loki_timeout: int = 30
    loki_verify_ssl: bool = True
    loki_org_id: str = "talkops"

    # ── Compaction ────────────────────────────────────────
    compaction_token_budget: int = 100_000
    compaction_keep_messages: int = 6
    compaction_summary_model: str | None = None

    # ── Rubric ────────────────────────────────────────────
    rubric_grader_model: str | None = None
    rubric_max_iterations: int = 3

    # ── Integrations ──────────────────────────────────────
    slack_enabled: bool = False
    slack_operation_mode: str = "read"
    slack_stream_buffer_size: int = 256

    # ── Code Interpreter ──────────────────────────────────
    interpreter_ptc: str | list[str] | None = "safe"
    interpreter_ptc_acknowledge_unsafe: bool = False
    interpreter_timeout_seconds: int = 30
    interpreter_memory_limit_mb: int = 64
    interpreter_max_ptc_calls: int = 50
    interpreter_max_result_chars: int = 50000

    # ── A2A Server ────────────────────────────────────────
    a2a_server_host: str = "0.0.0.0"
    a2a_server_port: int = 10102
    autopilot_mode: str = "a2a"

    # ── System & Runtime ──────────────────────────────────
    project_root: Path | None = None
    environment: str = "development"
    org_id: str = "default"
    org_name: str = "default_org"
    debug: bool = False
    log_level: str = "INFO"
    log_mode: str = "text"
    log_to_console: bool = True
    log_to_file: bool = True
    log_file: str = "k8s_autopilot.log"
    recursion_limit: int = 50
    checkpoint_backend: str = "sqlite"
    postgres_uri: str | None = None
    extra_skills_dirs: str | None = None

    # ── Tracing ───────────────────────────────────────────
    langchain_api_key: str | None = None
    langchain_tracing: bool = False
    langchain_project: str = "k8s-autopilot"
    langchain_endpoint: str = "https://api.smith.langchain.com"

    # ── Search ────────────────────────────────────────────
    tavily_max_results: int = 5
    web_search_timeout: int = 30

    def __post_init__(self) -> None:
        if not self.model_name and self.model:
            self.model_name = self.model
        if not self.model and self.model_name:
            self.model = self.model_name

    # ── Store-Hydrated Factory ────────────────────────────

    @classmethod
    async def from_store(cls, store: Any) -> Settings:
        """Create settings by resolving every manifest option through the store."""
        from k8s_autopilot.config.manifest import get_config_options

        kwargs: dict[str, Any] = {}
        for option in get_config_options():
            if option.settings_field:
                value, _ = await store.resolve(option)
                if value is not None:
                    kwargs[option.settings_field] = value

        if "project_root" not in kwargs or kwargs.get("project_root") is None:
            kwargs["project_root"] = paths.find_project_root()

        return cls(**kwargs)

    @classmethod
    def from_env(cls, start_path: Path | None = None) -> Settings:
        """Create settings from environment variables only (bootstrap fallback)."""
        _ensure_bootstrap()

        root = paths.find_project_root(start_path)

        def _get(key: str, default: str = "", fallbacks: tuple[str, ...] = ()) -> str:
            return resolve_env_var(key, fallbacks) or default

        debug_val = _get("DEBUG", "false").lower() in ("1", "true", "yes", "on")

        raw_shell_allow = resolve_env_var("SHELL_ALLOW_LIST")
        shell_allow = parse_shell_allow_list(raw_shell_allow)

        context_limit_str = resolve_env_var("MODEL_CONTEXT_LIMIT")
        context_limit = int(context_limit_str) if context_limit_str and context_limit_str.isdigit() else None

        mcp_timeout_str = resolve_env_var("MCP_TIMEOUT", ("MCP_TIMEOUT_SECONDS",))
        mcp_timeout = int(mcp_timeout_str) if mcp_timeout_str and mcp_timeout_str.isdigit() else 30

        port_str = _get("A2A_SERVER_PORT", "10102", ("PORT",))
        port = int(port_str) if port_str.isdigit() else 10102

        model_val = _get("MODEL", "", ("DEFAULT_MODEL", "MODEL_NAME")) or None

        return cls(
            model=model_val,
            model_name=model_val,
            model_provider=resolve_env_var("MODEL_PROVIDER", ("LLM_PROVIDER",)),
            reasoning_effort=_get("REASONING_EFFORT", "medium"),
            model_context_limit=context_limit,
            approval_mode=_get("APPROVAL_MODE", "manual"),
            shell_allow_list=shell_allow,
            kubeconfig=_get("KUBECONFIG", "", ("K8S_KUBECONFIG",)),
            kube_context=_get("KUBE_CONTEXT", ""),
            kube_namespace=_get("KUBE_NAMESPACE", "default"),
            openai_api_key=resolve_env_var("OPENAI_API_KEY"),
            anthropic_api_key=resolve_env_var("ANTHROPIC_API_KEY"),
            google_api_key=resolve_env_var("GOOGLE_API_KEY", ("GEMINI_API_KEY",)),
            groq_api_key=resolve_env_var("GROQ_API_KEY"),
            deepseek_api_key=resolve_env_var("DEEPSEEK_API_KEY"),
            openrouter_api_key=resolve_env_var("OPENROUTER_API_KEY"),
            mistral_api_key=resolve_env_var("MISTRAL_API_KEY"),
            fireworks_api_key=resolve_env_var("FIREWORKS_API_KEY"),
            together_api_key=resolve_env_var("TOGETHER_API_KEY"),
            xai_api_key=resolve_env_var("XAI_API_KEY"),
            cohere_api_key=resolve_env_var("COHERE_API_KEY"),
            perplexity_api_key=resolve_env_var("PERPLEXITY_API_KEY"),
            nvidia_api_key=resolve_env_var("NVIDIA_API_KEY"),
            huggingface_api_key=resolve_env_var("HUGGINGFACE_API_KEY"),
            aws_access_key_id=resolve_env_var("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=resolve_env_var("AWS_SECRET_ACCESS_KEY"),
            aws_region=resolve_env_var("AWS_REGION"),
            tavily_api_key=resolve_env_var("TAVILY_API_KEY"),
            azure_openai_api_key=resolve_env_var("AZURE_OPENAI_API_KEY"),
            langchain_api_key=resolve_env_var("LANGCHAIN_API_KEY", ("LANGSMITH_API_KEY",)),
            langchain_tracing=_get("LANGCHAIN_TRACING_V2", "false", ("LANGSMITH_TRACING",)).lower()
            in ("1", "true", "yes", "on"),
            langchain_project=_get(
                "LANGCHAIN_PROJECT", "k8s-autopilot", ("LANGSMITH_PROJECT", "K8S_AUTOPILOT_LANGSMITH_PROJECT")
            ),
            langchain_endpoint=_get("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com", ("LANGSMITH_ENDPOINT",)),
            slack_bot_token=resolve_env_var("SLACK_BOT_TOKEN"),
            slack_signing_secret=resolve_env_var("SLACK_SIGNING_SECRET"),
            argocd_auth_token=resolve_env_var("ARGOCD_AUTH_TOKEN"),
            loki_auth_token=resolve_env_var("LOKI_AUTH_TOKEN"),
            tempo_auth_header=resolve_env_var("TEMPO_AUTH_HEADER"),
            prometheus_base_url=_get(
                "PROMETHEUS_BASE_URL", "http://prometheus-operated.monitoring.svc:9090", ("PROMETHEUS_URL",)
            ),
            prometheus_verify_ssl=_get("PROMETHEUS_VERIFY_SSL", "false").lower() in ("1", "true", "yes", "on"),
            alertmanager_base_url=_get("ALERTMANAGER_BASE_URL", "http://alertmanager-operated.monitoring.svc:9093"),
            alertmanager_verify_ssl=_get("ALERTMANAGER_VERIFY_SSL", "false").lower() in ("1", "true", "yes", "on"),
            loki_url=_get("LOKI_URL", "http://host.docker.internal:3100"),
            loki_timeout=int(_get("LOKI_TIMEOUT", "30")) if _get("LOKI_TIMEOUT", "30").isdigit() else 30,
            loki_verify_ssl=_get("LOKI_VERIFY_SSL", "true").lower() in ("1", "true", "yes", "on"),
            loki_org_id=_get("LOKI_ORG_ID", "talkops"),
            tempo_base_url=_get("TEMPO_BASE_URL", "http://localhost:3200"),
            tempo_verify_ssl=_get("TEMPO_VERIFY_SSL", "true").lower() in ("1", "true", "yes", "on"),
            argocd_server_url=resolve_env_var("ARGOCD_SERVER_URL"),
            argocd_insecure=_get("ARGOCD_INSECURE", "true").lower() in ("1", "true", "yes", "on"),
            traefik_api_url=resolve_env_var("TRAEFIK_API_URL"),
            otel_exporter_otlp_endpoint=resolve_env_var("OTEL_EXPORTER_OTLP_ENDPOINT"),
            project_root=root,
            mcp_timeout=mcp_timeout,
            extra_skills_dirs=resolve_env_var("EXTRA_SKILLS_DIRS"),
            debug=debug_val,
            log_level=_get("LOG_LEVEL", "INFO"),
            log_mode=_get("LOG_MODE", "text"),
            log_to_console=_get("LOG_TO_CONSOLE", "true").lower() in ("1", "true", "yes", "on"),
            log_to_file=_get("LOG_TO_FILE", "true").lower() in ("1", "true", "yes", "on"),
            log_file=_get("LOG_FILE", "k8s_autopilot.log"),
            a2a_server_host=_get("A2A_SERVER_HOST", "0.0.0.0", ("HOST",)),
            a2a_server_port=port,
            checkpoint_backend=_get("CHECKPOINT_BACKEND", "sqlite", ("CHECKPOINTER_BACKEND",)),
            postgres_uri=resolve_env_var("POSTGRES_URI", ("K8S_AUTOPILOT_POSTGRES_URI", "DATABASE_URL")),
        )

    async def sync_to_store(self, store: Any) -> int:
        """Persist current settings values back to the store."""
        from k8s_autopilot.config.manifest import get_config_options

        written = 0
        for option in get_config_options():
            if option.settings_field:
                current_value = getattr(self, option.settings_field, None)
                if current_value is not None:
                    await store.set_typed(option, current_value)
                    written += 1
        return written

    def ensure_agent_dir(self, assistant_id: str | None = None) -> Path:
        """Ensure the agent directory and required subdirectories exist.

        Args:
            assistant_id: Optional assistant identifier. Defaults to the configured assistant_id.

        Returns:
            Resolved Path to the agent directory.
        """
        target_id = assistant_id or self.assistant_id or "k8s-autopilot"
        return paths.ensure_agent_dir(target_id)


# ── Lazy Singleton ───────────────────────────────────────

_settings: Settings | None = None
_settings_lock = threading.RLock()


def get_settings() -> Settings:
    """Return the singleton Settings instance."""
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = Settings.from_env()
    return _settings


async def reload_from_store(store: Any) -> Settings:
    """Replace the singleton with store-hydrated settings."""
    global _settings
    with _settings_lock:
        _settings = await Settings.from_store(store)
        from k8s_autopilot.config.manifest import get_config_options

        for option in get_config_options():
            if option.settings_field and option.effective_env_var:
                val = getattr(_settings, option.settings_field, None)
                if val is not None and str(val).strip() != "":
                    os.environ[option.effective_env_var] = str(val)
        if _settings.checkpoint_backend:
            os.environ["CHECKPOINTER_BACKEND"] = _settings.checkpoint_backend
        if _settings.model:
            os.environ["MODEL"] = _settings.model
        if _settings.model_name:
            os.environ["MODEL_NAME"] = _settings.model_name
        if _settings.model_provider:
            os.environ["MODEL_PROVIDER"] = _settings.model_provider

        # Bidirectional aliases for MCP compatibility
        sync_mcp_env_aliases()

        # Apply tracing settings and reset LangSmith caches
        from k8s_autopilot.config.langsmith import apply_tracing_settings

        apply_tracing_settings(_settings)

        # Hot-reload central logging with updated values
        from k8s_autopilot.utils.logger import configure_logging

        configure_logging(
            level=_settings.log_level,
            mode=getattr(_settings, "log_mode", "text"),
            to_console=getattr(_settings, "log_to_console", True),
            to_file=getattr(_settings, "log_to_file", True),
            file_path=getattr(_settings, "log_file", "k8s_autopilot.log"),
        )

        from k8s_autopilot.model.factory import clear_model_cache

        clear_model_cache()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from environment only (no store)."""
    global _settings
    with _settings_lock:
        _bootstrap_state.done = False
        _settings = Settings.from_env()
        from k8s_autopilot.utils.logger import configure_logging

        configure_logging(
            level=_settings.log_level,
            mode=_settings.log_mode,
            to_console=_settings.log_to_console,
            to_file=_settings.log_to_file,
            file_path=_settings.log_file,
        )

        from k8s_autopilot.model.factory import clear_model_cache

        clear_model_cache()
    return _settings
