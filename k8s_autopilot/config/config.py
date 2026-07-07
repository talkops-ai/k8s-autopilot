"""
K8s Autopilot Agent — Configuration Engine.

This module is the INTERNAL implementation. Users should only edit ``default.py``
to change default values. Overrides are applied automatically via environment
variables and runtime ``config`` dicts.

Precedence (highest → lowest):
    1. Runtime overrides  (``Config({"LLM_PROVIDER": "anthropic"})``)
    2. Environment variables / ``.env`` file
    3. Defaults from ``DefaultConfig`` in ``default.py``
"""


import json
import os
from typing import Any, Dict, List, Optional, Type, Union, get_args, get_origin

from dotenv import load_dotenv

from k8s_autopilot.config.default import DefaultConfig
from k8s_autopilot.utils.exceptions import ConfigError

# Load environment variables once at module level
load_dotenv()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _convert_env_value(key: str, env_value: str, type_hint: Type) -> Any:
    """Coerce a raw env-var string to the type declared in ``DefaultConfig``."""
    origin = get_origin(type_hint)
    args = get_args(type_hint)

    # Handle Optional[X] / Union[X, None]
    if origin is Union:
        for arg in args:
            if arg is type(None):
                if env_value.lower() in ("none", "null", ""):
                    return None
            else:
                try:
                    return _convert_env_value(key, env_value, arg)
                except Exception:
                    continue
        raise ConfigError(f"Cannot convert env var '{key}'={env_value!r} to {args}")

    if type_hint is bool:
        return env_value.lower() in ("true", "1", "yes", "on")
    if type_hint is int:
        return int(env_value)
    if type_hint is float:
        return float(env_value)
    if type_hint in (str, Any):
        return env_value
    if origin is list or type_hint is list:
        return json.loads(env_value)
    if origin is dict or type_hint is dict:
        return json.loads(env_value)

    raise ConfigError(f"Unsupported type {type_hint} for config key '{key}'")


def _collect_defaults() -> tuple[dict[str, Any], dict[str, Type]]:
    """
    Read all user-declared defaults + type annotations from ``DefaultConfig``.

    Returns ``(defaults_dict, annotations_dict)``.
    """
    annotations: dict[str, Type] = {}
    # Walk the MRO so subclasses of DefaultConfig also work
    for cls in reversed(DefaultConfig.__mro__):
        annotations.update(getattr(cls, "__annotations__", {}))

    defaults = {
        key: getattr(DefaultConfig, key)
        for key in annotations
        if hasattr(DefaultConfig, key)
    }
    return defaults, annotations


def _build_llm_kwargs(
    store: dict[str, Any],
    prefix: str,
) -> dict[str, Any]:
    """
    Build a ``langchain.chat_models.init_chat_model()``-compatible kwargs dict.

    ``prefix`` is one of ``"LLM_"``, ``"LLM_HIGHER_"``, ``"LLM_DEEPAGENT_"`` etc.

    Thinking/reasoning support is provider-agnostic: when
    ``{prefix}THINKING_ENABLED`` is True, the correct provider-specific
    kwargs are injected automatically.
    """
    provider: str = store.get(f"{prefix}PROVIDER", "openai")
    model: str = store.get(f"{prefix}MODEL", "gpt-4o-mini")

    kwargs: dict[str, Any] = {}

    temp_val = store.get(f"{prefix}TEMPERATURE")
    if temp_val is not None and temp_val != "":
        try:
            kwargs["temperature"] = float(temp_val)
        except (ValueError, TypeError):
            pass

    max_tokens_val = store.get(f"{prefix}MAX_TOKENS")
    if max_tokens_val is not None and max_tokens_val != "":
        try:
            kwargs["max_tokens"] = int(max_tokens_val)
        except (ValueError, TypeError):
            pass

    # Provider-specific model string for init_chat_model
    if provider == "azure_openai":
        kwargs["model"] = f"azure_openai:{model}"
        deployment = store.get("AZURE_OPENAI_DEPLOYMENT_NAME")
        if deployment:
            kwargs["azure_deployment"] = deployment
    elif provider in ("google_genai", "gemini"):
        kwargs["model"] = f"google_genai:{model}"
    elif provider in ("bedrock", "aws_bedrock"):
        kwargs["model"] = model
        kwargs["model_provider"] = "bedrock_converse"
    else:
        kwargs["model"] = model
        if provider:
            kwargs["model_provider"] = provider

    # ── Thinking / reasoning support ──────────────────────────────────
    # Provider-agnostic: users set THINKING_ENABLED=True and optionally
    # THINKING_BUDGET=<int>.  The builder maps to provider-specific kwargs.
    thinking_enabled_val = store.get(f"{prefix}THINKING_ENABLED", False)
    if isinstance(thinking_enabled_val, str):
        thinking_enabled = thinking_enabled_val.lower() in ("true", "1", "yes", "on")
    else:
        thinking_enabled = bool(thinking_enabled_val)

    thinking_budget_val = store.get(f"{prefix}THINKING_BUDGET")
    if thinking_budget_val is None or thinking_budget_val == "":
        thinking_budget = None
    else:
        try:
            thinking_budget = int(thinking_budget_val)
        except (ValueError, TypeError):
            thinking_budget = None

    if thinking_enabled:
        _inject_thinking_kwargs(kwargs, provider, model, thinking_budget)

    # Backward-compat key (safe to remove once all callers use the property)
    kwargs["provider"] = provider
    return kwargs


def _inject_thinking_kwargs(
    kwargs: dict[str, Any],
    provider: str,
    model: str,
    budget: int | None,
) -> None:
    """Inject provider-specific thinking/reasoning kwargs.

    Keeps the model-factory provider-agnostic — callers set
    ``THINKING_ENABLED=True`` without caring which LLM is behind.

    Supported providers:

    * **Google GenAI / Gemini**: ``include_thoughts=True``.  Gemini 2.5
      uses ``thinking_budget`` (token count), Gemini 3.x uses
      ``thinking_level`` (LOW/MEDIUM/HIGH).  We default to ``budget`` →
      ``thinking_budget`` for 2.5 and ``MEDIUM`` for 3.x.
    * **Anthropic / Bedrock-Claude**: ``thinking`` dict with ``type``
      and ``budget_tokens``.
    * **OpenAI**: o-series models (o1, o3, o4-mini) emit reasoning
      tokens automatically.  ``include_thoughts`` is passed for models
      that support it.
    * **Ollama**: ``think=True`` enables thinking mode.
    * **Other**: ``include_thoughts=True`` is set as a best-effort
      fallback. Providers that don't recognise it typically ignore it.
    """
    norm = provider.lower().replace("-", "_")

    if norm in ("google_genai", "gemini"):
        kwargs["include_thoughts"] = True
        if budget is not None:
            # Gemini 2.5: thinking_budget (token count)
            # Gemini 3.x: thinking_level takes precedence
            # Let LangChain pick the right one — if both are set the
            # adapter resolves automatically.
            kwargs["thinking_budget"] = budget

    elif norm in ("anthropic", "bedrock", "aws_bedrock", "bedrock_converse"):
        # Anthropic Claude 3.5+ / Bedrock Converse
        thinking_config: dict[str, Any] = {"type": "enabled"}
        if budget is not None:
            thinking_config["budget_tokens"] = budget
        else:
            thinking_config["budget_tokens"] = 4096  # sensible default
        kwargs["thinking"] = thinking_config

    elif norm in ("openai", "azure_openai"):
        # o-series models (o1, o3, o4-mini) have reasoning built in.
        # For standard models, include_thoughts has no effect but is harmless.
        kwargs["include_thoughts"] = True

    elif norm == "ollama":
        # Ollama (DeepSeek-R1, QwQ, etc.) uses think=True
        kwargs["think"] = True

    else:
        # Best-effort fallback — pass the flag and hope the provider
        # adapter recognises it or silently ignores it.
        kwargs["include_thoughts"] = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class Config:
    """
    Resolved configuration for the K8s Autopilot Agent.

    **Users should never edit this file.**  Change defaults in ``default.py``,
    override at runtime via env-vars or the ``config`` dict.

    Precedence (highest → lowest):
        1. PostgreSQL database overrides (dynamic cache)
        2. ``config`` dict passed to ``__init__``
        3. Environment variables (``.env`` or system)
        4. ``DefaultConfig`` values in ``default.py``

    Access style::

        cfg = Config()
        cfg.LLM_PROVIDER        # → "openai"  (canonical UPPER key)
        cfg["LLM_PROVIDER"]     # → same, dict-style
    """

    # ── construction ──────────────────────────────────────────────────────

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        import threading
        self._lock = threading.Lock()
        self._db_overrides: dict[str, Any] = {}
        self._db_mcp_server_ids: dict[str, str] = {}

        overrides = config or {}
        defaults, annotations = _collect_defaults()

        store: dict[str, Any] = {}

        # Layer 1 – defaults  →  Layer 2 – env-vars
        for key, default_value in defaults.items():
            env_value = os.getenv(key)
            if env_value is not None and env_value.strip():
                type_hint = annotations.get(key, type(default_value))
                try:
                    store[key] = _convert_env_value(key, env_value, type_hint)
                except Exception as exc:
                    raise ConfigError(
                        f"Failed to convert env var '{key}'={env_value!r} "
                        f"to {type_hint}: {exc}"
                    ) from exc
            else:
                store[key] = default_value

        # Layer 3 – runtime overrides (highest priority)
        store.update(overrides)

        # Freeze the internal dict
        self._store: dict[str, Any] = store

        # Sync key env vars to os.environ so third-party libraries (e.g. LangChain, Google SDK) pick them up
        for k, v in self._store.items():
            if k.startswith(("LANGCHAIN_", "LANGGRAPH_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "AZURE_", "AWS_")):
                if v is not None and v != "":
                    if isinstance(v, bool):
                        os.environ[k] = "true" if v else "false"
                    else:
                        os.environ[k] = str(v)

    async def reload(self) -> None:
        """Fetch all settings from the database and refresh the overrides cache."""
        from k8s_autopilot.core.hitl.checkpointer import get_database_uri
        db_uri = get_database_uri(self)
        if not db_uri:
            return

        try:
            from k8s_autopilot.config.db_config import deserialize_value
            import psycopg

            new_overrides = {}
            new_mcp_server_ids = {}
            # Open temporary connection to read settings
            async with await psycopg.AsyncConnection.connect(db_uri) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT key, value, type, mcp_server_id FROM k8s_autopilot_settings;")
                    rows = await cur.fetchall()
                    for key, val_str, type_str, mcp_server_id in rows:
                        try:
                            # Skip database URI itself to avoid circular overrides
                            if key == "POSTGRES_URI":
                                continue
                            new_overrides[key] = deserialize_value(val_str, type_str)
                            if mcp_server_id:
                                new_mcp_server_ids[key] = mcp_server_id
                        except Exception:
                            continue

            with self._lock:
                self._db_overrides = new_overrides
                self._db_mcp_server_ids = new_mcp_server_ids
                # Sync key env vars to os.environ so third-party libraries (e.g. LangChain, Google SDK) pick them up
                for k, v in self._db_overrides.items():
                    if k.startswith(("LANGCHAIN_", "LANGGRAPH_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "AZURE_", "AWS_")):
                        if v is not None and v != "":
                            if isinstance(v, bool):
                                os.environ[k] = "true" if v else "false"
                            else:
                                os.environ[k] = str(v)
        except Exception:
            # Silently degrade if DB is not ready/reachable yet
            pass

    def _get_merged_store(self) -> Dict[str, Any]:
        with self._lock:
            merged = dict(self._store)
            merged.update(self._db_overrides)
            return merged

    # ── attribute access (single path, no divergence) ─────────────────────

    def __getattr__(self, name: str) -> Any:
        # Allow both UPPER and lower lookups:  cfg.LLM_PROVIDER  /  cfg.llm_provider
        store = self.__dict__.get("_store")
        if store is None:
            raise AttributeError(name)
        
        db_overrides = self.__dict__.get("_db_overrides", {})
        if name in db_overrides:
            return db_overrides[name]
        upper = name.upper()
        if upper in db_overrides:
            return db_overrides[upper]

        if name in store:
            return store[name]
        if upper in store:
            return store[upper]
        raise AttributeError(f"Config has no key '{name}'")

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            if key in self._db_overrides:
                return self._db_overrides[key]
            if key.upper() in self._db_overrides:
                return self._db_overrides[key.upper()]
        return self._store[key]

    def __contains__(self, key: str) -> bool:
        with self._lock:
            if key in self._db_overrides or key.upper() in self._db_overrides:
                return True
        return key in self._store or key.upper() in self._store

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style ``.get()`` with fallback."""
        try:
            return self[key]
        except KeyError:
            return default

    # ── LLM config properties (DRY — single builder) ─────────────────────

    @property
    def llm_config(self) -> Dict[str, Any]:
        """Standard LLM config kwargs for ``init_chat_model()``."""
        return _build_llm_kwargs(self._get_merged_store(), "LLM_")

    @property
    def llm_higher_config(self) -> Dict[str, Any]:
        """Higher-tier LLM config kwargs for ``init_chat_model()``."""
        return _build_llm_kwargs(self._get_merged_store(), "LLM_HIGHER_")

    @property
    def llm_deepagent_config(self) -> Dict[str, Any]:
        """DeepAgent LLM config kwargs for ``init_chat_model()``."""
        return _build_llm_kwargs(self._get_merged_store(), "LLM_DEEPAGENT_")

    # Convenience aliases (some call-sites use method style)
    def get_llm_config(self) -> Dict[str, Any]:
        return self.llm_config

    def get_llm_higher_config(self) -> Dict[str, Any]:
        return self.llm_higher_config

    def get_llm_deepagent_config(self) -> Dict[str, Any]:
        return self.llm_deepagent_config

    # ── MCP config ────────────────────────────────────────────────────────

    @property
    def mcp_config(self) -> Dict[str, Any]:
        """Return MCP server configuration for ``MCPClient``."""
        raw = self._get_merged_store().get("MCP_SERVERS", [])

        if isinstance(raw, str):
            try:
                servers = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                servers = []
        else:
            servers = list(raw)  # defensive copy

        # Inject DB overrides into specific stdio MCP server environments
        merged_store = self._get_merged_store()
        updated_servers = []
        for s in servers:
            s_copy = dict(s)
            if "env" in s_copy:
                s_copy["env"] = dict(s_copy["env"])
            else:
                s_copy["env"] = {}

            name = s_copy.get("name")
            if name == "prometheus-mcp-server":
                prom_url = merged_store.get("PROMETHEUS_BASE_URL")
                if prom_url:
                    s_copy["env"]["PROMETHEUS_BASE_URL"] = prom_url
            elif name == "loki-mcp-server":
                loki_url = merged_store.get("LOKI_URL")
                if loki_url:
                    s_copy["env"]["LOKI_URL"] = loki_url
            elif name == "tempo-mcp-server":
                tempo_url = merged_store.get("TEMPO_BASE_URL")
                if tempo_url:
                    s_copy["env"]["TEMPO_BASE_URL"] = tempo_url
            elif name == "alertmanager-mcp-server":
                am_url = merged_store.get("ALERTMANAGER_BASE_URL")
                if am_url:
                    s_copy["env"]["ALERTMANAGER_BASE_URL"] = am_url
            elif name == "argocd_mcp_server":
                argocd_url = merged_store.get("ARGOCD_SERVER_URL")
                argocd_token = merged_store.get("ARGOCD_AUTH_TOKEN")
                argocd_insecure = merged_store.get("ARGOCD_INSECURE")
                if argocd_url:
                    s_copy["env"]["ARGOCD_SERVER_URL"] = argocd_url
                if argocd_token:
                    s_copy["env"]["ARGOCD_AUTH_TOKEN"] = argocd_token
                if argocd_insecure is not None:
                    s_copy["env"]["ARGOCD_INSECURE"] = "true" if argocd_insecure else "false"
            elif name == "helm_mcp_server":
                helm_ws = merged_store.get("HELM_WORKSPACE")
                if helm_ws:
                    s_copy["env"]["HELM_WORKSPACE"] = helm_ws

            # Inject dynamic custom settings associated with this server card
            card_id = None
            if name:
                if name == "opentelemetry-mcp-server":
                    card_id = "otel"
                elif name.endswith("_mcp_server"):
                    card_id = name[:-11]
                elif name.endswith("-mcp-server"):
                    card_id = name[:-11]
                elif name == "github_mcp":
                    card_id = "github"
            
            if card_id:
                for k, v in merged_store.items():
                    if self._db_mcp_server_ids.get(k) == card_id:
                        if v is not None and v != "":
                            if isinstance(v, bool):
                                s_copy["env"][k] = "true" if v else "false"
                            else:
                                s_copy["env"][k] = str(v)

            updated_servers.append(s_copy)

        return {
            "servers": updated_servers,
            "timeout": {
                "total": merged_store.get("MCP_TIMEOUT_TOTAL", 600.0),
                "connect": merged_store.get("MCP_TIMEOUT_CONNECT", 300.0),
            },
            "default_host": merged_store.get("MCP_DEFAULT_HOST", "localhost"),
            "default_transport": merged_store.get("MCP_DEFAULT_TRANSPORT", "sse"),
        }

    def get_mcp_config(self) -> Dict[str, Any]:
        return self.mcp_config

    # ── mutators (update store directly — no stale attrs) ─────────────────

    def set(self, key: str, value: Any) -> None:
        """Set a config key at runtime (highest priority)."""
        self._store[key] = value
        if key.startswith(("LANGCHAIN_", "LANGGRAPH_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "AZURE_", "AWS_")):
            if value is not None and value != "":
                if isinstance(value, bool):
                    os.environ[key] = "true" if value else "false"
                else:
                    os.environ[key] = str(value)
            else:
                os.environ.pop(key, None)

    def set_llm_config(self, values: Dict[str, Any]) -> None:
        """Convenience: set standard LLM fields from a dict."""
        _KEY_MAP = {
            "provider": "LLM_PROVIDER",
            "model": "LLM_MODEL",
            "temperature": "LLM_TEMPERATURE",
            "max_tokens": "LLM_MAX_TOKENS",
        }
        for k, v in values.items():
            store_key = _KEY_MAP.get(k)
            if store_key:
                self._store[store_key] = v

    def set_llm_higher_config(self, values: Dict[str, Any]) -> None:
        """Convenience: set higher-tier LLM fields from a dict."""
        _KEY_MAP = {
            "provider": "LLM_HIGHER_PROVIDER",
            "model": "LLM_HIGHER_MODEL",
            "temperature": "LLM_HIGHER_TEMPERATURE",
            "max_tokens": "LLM_HIGHER_MAX_TOKENS",
        }
        for k, v in values.items():
            store_key = _KEY_MAP.get(k)
            if store_key:
                self._store[store_key] = v

    def set_mcp_servers(self, servers: List[Dict[str, Any]]) -> None:
        """Replace MCP server list."""
        self._store["MCP_SERVERS"] = servers

    def add_mcp_server(
        self,
        name: str,
        host: str,
        port: int,
        transport: str = "sse",
        disabled: bool = False,
    ) -> None:
        """Append an MCP server definition."""
        current = self.mcp_config["servers"]
        current.append(
            {
                "name": name,
                "host": host,
                "port": port,
                "transport": transport,
                "disabled": disabled,
            }
        )
        self.set_mcp_servers(current)

    # ── serialisation ─────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Return a shallow copy of all resolved config values."""
        return self._get_merged_store()

    @classmethod
    def load_config(cls, config_path: str) -> "Config":
        """
        Load configuration from a JSON file, merged with defaults.

        Returns a ``Config`` instance (not a raw dict) so precedence
        rules are always enforced.
        """
        if not os.path.exists(config_path):
            raise ConfigError(
                f"Configuration file not found: '{config_path}'"
            )

        with open(config_path, "r") as fh:
            custom = json.load(fh)

        return cls(config=custom)

    def __repr__(self) -> str:
        keys = sorted(self._store)
        return f"Config({', '.join(f'{k}=...' for k in keys[:5])}, ...)"