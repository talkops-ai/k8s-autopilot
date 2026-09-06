"""Settings and Models REST API — manifest-aware, typed, DB-backed.

Endpoints:
- **Models**:
  - ``GET /api/models`` — list available models with metadata & current active selection
  - ``GET /api/models/effort`` — get supported effort levels for a model
  - ``POST /api/models/select`` — dynamically switch active model / reasoning effort
- **Settings**:
  - ``GET /api/settings`` — list all settings with manifest metadata
  - ``GET /api/settings/{key}`` — get a single setting
  - ``PUT /api/settings`` — bulk update settings
  - ``DELETE /api/settings/{key}`` — reset a setting
  - ``POST /api/settings/test-integration`` — test integration connectivity
  - ``GET /api/settings/health`` — health check
- **Entities**:
  - ``GET /api/mcp-servers``, ``PUT /api/mcp-servers``, ``DELETE /api/mcp-servers/{name}``
  - ``GET /api/plugins``, ``PUT /api/plugins``, ``DELETE /api/plugins/{plugin_id}``
  - ``GET /api/skills``, ``PUT /api/skills``, ``DELETE /api/skills/{name}``
  - ``GET /api/subagents``, ``PUT /api/subagents``, ``DELETE /api/subagents/{name}``
"""

from __future__ import annotations

import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from k8s_autopilot.config.manifest import (
    ConfigOption,
    OptionKind,
    coerce_str_value,
    get_config_options,
    get_option,
    get_option_by_db_key,
)
from k8s_autopilot.config.settings import get_settings
from k8s_autopilot.config.store import ConfigCategory, ConfigStore
from k8s_autopilot.config.store_factory import create_config_store
from k8s_autopilot.model.config import (
    AVAILABLE_MODELS,
    get_available_models_list,
    get_model_profile,
    get_provider_display_name,
    normalize_model_spec,
    resolve_model_spec,
)
from k8s_autopilot.model.reasoning import (
    SUPPORTED_EFFORT_LEVELS,
    default_effort_for_model,
    is_effort_supported_for_model,
    supported_efforts_for_model,
)

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_config_store: ConfigStore | None = None


async def get_config_store() -> ConfigStore:
    """Get or create the ConfigStore singleton."""
    global _config_store
    if _config_store is None:
        _config_store = await create_config_store()
    return _config_store


async def set_config_store(store: ConfigStore) -> None:
    """Set the ConfigStore singleton."""
    global _config_store
    _config_store = store


_SETTING_ALIASES: dict[str, str] = {
    "GEMINI_API_KEY": "GOOGLE_API_KEY",
    "GEMINI_API_BASE": "GEMINI_API_BASE",
    "GOOGLE_API_BASE": "GEMINI_API_BASE",
    "GOOGLE_GENAI_USE_VERTEXAI": "models.google_genai_use_vertexai",
    "GOOGLE_CLOUD_PROJECT": "models.google_cloud_project",
    "GOOGLE_CLOUD_LOCATION": "models.google_cloud_location",
    "LANGSMITH_API_KEY": "LANGCHAIN_API_KEY",
    "LANGSMITH_PROJECT": "LANGCHAIN_PROJECT",
    "LANGSMITH_ENDPOINT": "LANGCHAIN_ENDPOINT",
    "LANGSMITH_TRACING": "LANGCHAIN_TRACING_V2",
    "CHECKPOINTER_BACKEND": "CHECKPOINT_BACKEND",
    "CHECKPOINT_BACKEND": "CHECKPOINT_BACKEND",
    "K8S_AUTOPILOT_POSTGRES_URI": "POSTGRES_URI",
    "DATABASE_URL": "POSTGRES_URI",
    "POSTGRES_URI": "POSTGRES_URI",
}


def _find_option(key: str) -> ConfigOption | None:
    canonical = _SETTING_ALIASES.get(key, key)
    opt = get_option(canonical)
    if opt is not None:
        return opt
    opt = get_option_by_db_key(canonical)
    if opt is not None:
        return opt
    opt = get_option(key)
    if opt is not None:
        return opt
    return get_option_by_db_key(key)


# ─── Integration Test Helpers ────────────────────────────────────────────

async def test_slack_token(token: str) -> tuple[bool, str]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return True, ""
                return False, data.get("error", "Invalid Slack token")
            return False, f"Slack API returned HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


async def test_github_token(token: str) -> tuple[bool, str]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 401:
                return False, "Bad credentials / unauthorized"
            return False, f"GitHub API returned HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


# ─── Route Factory ───────────────────────────────────────────────────────

def create_settings_routes(config: Any = None) -> list[Route]:
    """Create Starlette routes for the Settings and Model APIs."""

    # ── Model Endpoints ───────────────────────────────────

    async def list_models(request: Request) -> JSONResponse:
        """GET /api/models — list available models with capabilities and current selection."""
        store = await get_config_store()
        current_model = await store.get("MODEL") or get_settings().model
        current_effort = await store.get("REASONING_EFFORT") or get_settings().reasoning_effort

        models_by_provider: dict[str, list[dict[str, Any]]] = {}
        for provider, models in AVAILABLE_MODELS.items():
            models_by_provider[provider] = []
            for model_id, display_name in models:
                spec = f"{provider}:{model_id}"
                prof = get_model_profile(spec)
                profile = prof["profile"] if prof else {}
                models_by_provider[provider].append({
                    "spec": spec,
                    "model_id": model_id,
                    "display_name": display_name,
                    "provider": provider,
                    "provider_display_name": get_provider_display_name(provider),
                    "reasoning_output": profile.get("reasoning_output", False),
                    "tool_calling": profile.get("tool_calling", True),
                    "max_input_tokens": profile.get("max_input_tokens"),
                    "max_output_tokens": profile.get("max_output_tokens"),
                    "is_active": spec == current_model or model_id == current_model,
                })

        return JSONResponse({
            "current_model": current_model,
            "current_effort": current_effort,
            "providers": list(AVAILABLE_MODELS.keys()),
            "models_by_provider": models_by_provider,
            "all_models": get_available_models_list(),
        })

    async def get_model_effort_options(request: Request) -> JSONResponse:
        """GET /api/models/effort?model=... — get effort levels for a model."""
        model_spec = request.query_params.get("model")
        efforts = supported_efforts_for_model(model_spec)
        default_eff = default_effort_for_model(model_spec)
        return JSONResponse({
            "model": model_spec,
            "supported_efforts": list(efforts),
            "default_effort": default_eff,
        })

    async def select_model(request: Request) -> JSONResponse:
        """POST /api/models/select — dynamically set active model and/or thinking effort."""
        data = await request.json()
        model_spec = data.get("model") or data.get("model_spec")
        effort = data.get("effort") or data.get("reasoning_effort")

        store = await get_config_store()
        settings = get_settings()

        response: dict[str, Any] = {"success": True}

        if model_spec:
            normalized = normalize_model_spec(model_spec)
            provider, model_name = resolve_model_spec(normalized)

            await store.set(
                key="MODEL",
                value=normalized,
                category=ConfigCategory.MODELS,
                display_name="Active Model",
            )
            await store.set(
                key="MODEL_PROVIDER",
                value=provider,
                category=ConfigCategory.MODELS,
                display_name="Active Provider",
            )

            os.environ["MODEL"] = normalized
            os.environ["MODEL_PROVIDER"] = provider

            if provider:
                from k8s_autopilot.model.config import apply_stored_credentials
                apply_stored_credentials(provider)

            response["model"] = normalized
            response["provider"] = provider

            # Update recent models in model_preferences
            prefs = await store.get_model_preferences()
            recent = prefs.get("recent_models", [])
            recent = [m for m in recent if m != normalized]
            recent.insert(0, normalized)
            prefs["recent_models"] = recent[:10]
            prefs["default_model"] = normalized
            await store.save_model_preferences(prefs)

            # If effort was not explicitly provided in this request, resolve saved or default effort
            if not effort:
                by_model = prefs.get("effort_by_model", {})
                effort = by_model.get(normalized) or by_model.get(model_spec) or default_effort_for_model(normalized)

        if effort:
            eff_lower = effort.lower()
            spec_for_effort = model_spec or settings.model or "openai:gpt-4o"
            if is_effort_supported_for_model(spec_for_effort, eff_lower):
                await store.set(
                    key="REASONING_EFFORT",
                    value=eff_lower,
                    category=ConfigCategory.MODELS,
                    display_name="Reasoning Effort",
                )
                settings.reasoning_effort = eff_lower
                os.environ["REASONING_EFFORT"] = eff_lower
                response["effort"] = eff_lower

                if model_spec:
                    prefs = await store.get_model_preferences()
                    by_model = prefs.get("effort_by_model", {})
                    by_model[model_spec] = eff_lower
                    prefs["effort_by_model"] = by_model
                    await store.save_model_preferences(prefs)
            else:
                response["warning"] = f"Effort level '{effort}' may not be natively supported by {model_spec}"

        from k8s_autopilot.config.settings import reload_from_store
        await reload_from_store(store)
        await _invalidate_mcp_and_agent_caches()

        return JSONResponse(response)

    async def _invalidate_mcp_and_agent_caches() -> None:
        """Evict active MCP sessions and cached agent instances on configuration changes."""
        try:
            from k8s_autopilot.mcp.session_manager import MCPSessionManager
            from k8s_autopilot.mcp.preload import clear_cached_mcp_server_infos
            from k8s_autopilot.server.executor import A2AAutoPilotExecutor

            await MCPSessionManager.get_instance().close_all()
            clear_cached_mcp_server_infos()
            A2AAutoPilotExecutor.invalidate_all_agents()
        except Exception as exc:
            logger.debug("Failed evicting MCP sessions or agent caches: %s", exc)

    # ── Settings Endpoints ────────────────────────────────

    async def get_settings_list(request: Request) -> JSONResponse:
        """GET /api/settings — list all settings with manifest metadata and custom entries."""
        store = await get_config_store()
        reveal = request.query_params.get("reveal", "").lower() in ("true", "1", "yes")
        result = []
        manifest_keys: set[str] = set()

        for option in get_config_options():
            manifest_keys.add(option.db_key)
            manifest_keys.add(option.key)
            value, source = await store.resolve(option)
            display_value = value if (reveal or not option.redacted) else ("******" if value else "")
            result.append({
                "key": option.key,
                "db_key": option.db_key,
                "value": display_value,
                "source": source,
                "group": option.group,
                "kind": option.kind.value,
                "summary": option.summary,
                "default": option.default,
                "is_sensitive": option.redacted,
                "choices": option.choices,
            })

        # Include custom config entries from DB
        all_entries = await store.list_all()
        for entry in all_entries:
            if (
                entry.key not in manifest_keys
                and not entry.key.startswith("models.")
                and not entry.key.startswith("credentials.")
            ):
                display_val = entry.value if (reveal or not entry.is_secret) else ("******" if entry.value else "")
                result.append({
                    "key": entry.key,
                    "db_key": entry.key,
                    "value": display_val,
                    "source": "db",
                    "group": "Custom",
                    "kind": "str",
                    "summary": entry.display_name or "Custom Environment Variable",
                    "default": None,
                    "is_sensitive": entry.is_secret,
                    "choices": None,
                })

        return JSONResponse(result)

    async def get_setting(request: Request) -> JSONResponse:
        """GET /api/settings/{key} — get a single setting (supports ?reveal=true for secrets)."""
        key = request.path_params["key"]
        reveal = request.query_params.get("reveal", "").lower() in ("true", "1", "yes")

        store = await get_config_store()
        option = _find_option(key)

        if option is not None:
            value, source = await store.resolve(option)
            display_value = value if (reveal or not option.redacted) else ("******" if value else "")
            return JSONResponse({
                "key": option.key,
                "db_key": option.db_key,
                "value": display_value,
                "source": source,
                "group": option.group,
                "kind": option.kind.value,
                "summary": option.summary,
                "default": option.default,
                "is_sensitive": option.redacted,
                "choices": option.choices,
            })

        # Check for custom configuration entry
        entry = await store.get_entry(key) or await store.get_entry(key.upper())
        if entry is not None:
            display_value = entry.value if (reveal or not entry.is_secret) else ("******" if entry.value else "")
            return JSONResponse({
                "key": entry.key,
                "db_key": entry.key,
                "value": display_value,
                "source": "db",
                "group": "Custom",
                "kind": "str",
                "summary": entry.display_name or "Custom Environment Variable",
                "default": None,
                "is_sensitive": entry.is_secret,
                "choices": None,
            })

        return JSONResponse({"detail": f"Unknown setting: {key}"}, status_code=404)

    async def put_settings(request: Request) -> JSONResponse:
        """PUT /api/settings — bulk update settings."""
        data = await request.json()
        if not isinstance(data, list):
            return JSONResponse(
                {"detail": "Expected list of {key, value} objects"},
                status_code=400,
            )

        store = await get_config_store()
        updated = 0
        errors: list[str] = []
        env_updates: dict[str, str] = {}
        storage_changed = False

        for item in data:
            key = item.get("key") or item.get("db_key")
            value = item.get("value")
            if not key or value is None:
                continue

            if value == "******":
                continue

            option = _find_option(key)
            if option is not None:
                if option.choices and str(value) not in option.choices:
                    errors.append(
                        f"Invalid value for {key}: {value!r}. "
                        f"Valid choices: {', '.join(option.choices)}"
                    )
                    continue

                coerced = coerce_str_value(option.kind, str(value))
                if coerced is None and str(value) != "" and option.kind != OptionKind.STR:
                    errors.append(f"Cannot coerce {value!r} to {option.type_label} for {key}")
                    continue

                await store.set_typed(option, coerced if coerced is not None else value)
                logger.info(
                    "Config setting '%s' updated in ConfigStore",
                    option.key,
                    extra={"key": option.key, "category": option.group, "source": "api"},
                )
                env_name = option.effective_env_var
                if env_name and value is not None:
                    os.environ[env_name] = str(value)
                    env_updates[env_name] = str(value)
                    for alias_key, canonical_target in _SETTING_ALIASES.items():
                        if canonical_target in (env_name, option.db_key, option.key):
                            os.environ[alias_key] = str(value)
                        elif alias_key in (env_name, option.db_key, option.key):
                            os.environ[canonical_target] = str(value)

                if option.db_key in ("CHECKPOINT_BACKEND", "CHECKPOINTER_BACKEND", "POSTGRES_URI"):
                    storage_changed = True
                    if option.db_key == "CHECKPOINT_BACKEND":
                        os.environ["CHECKPOINTER_BACKEND"] = str(value)
            else:
                # Arbitrary custom configuration variable
                clean_key = str(key).strip().upper()
                await store.set(
                    key=clean_key,
                    value=str(value),
                    category=ConfigCategory.SYSTEM,
                    display_name=clean_key,
                    is_secret=False,
                )
                os.environ[clean_key] = str(value)
                env_updates[clean_key] = str(value)
                logger.info(
                    "Custom config setting '%s' saved in ConfigStore and environment",
                    clean_key,
                )

            updated += 1

        if env_updates:
            from k8s_autopilot.config.paths import upsert_env_vars
            upsert_env_vars(env_updates)

        # Dynamic Storage Hot-Swapping
        if storage_changed:
            try:
                from k8s_autopilot.api.service import ThreadService, set_thread_service
                from k8s_autopilot.server.executor import A2AAutoPilotExecutor
                from k8s_autopilot.state.session import create_runtime_checkpointer
                from k8s_autopilot.config.store_factory import create_config_store

                target_backend = os.environ.get("CHECKPOINT_BACKEND", "sqlite")

                # 1. Dynamically hot-swap thread/checkpoint runtime checkpointer
                new_cp = await create_runtime_checkpointer(target_backend)
                for inst in A2AAutoPilotExecutor._instances:
                    inst.checkpointer = new_cp
                    inst._active_checkpointer = None
                exec_inst = A2AAutoPilotExecutor._instances[0] if A2AAutoPilotExecutor._instances else None
                thread_service = ThreadService(new_cp, executor=exec_inst)
                set_thread_service(thread_service)
                A2AAutoPilotExecutor.invalidate_all_agents()
                logger.info("Storage checkpointer dynamically hot-swapped to %s", target_backend)

                # 2. Dynamically hot-swap ConfigStore to target database backend
                new_store = await create_config_store(target_backend)
                await set_config_store(new_store)
                store = new_store
                logger.info("ConfigStore dynamically hot-swapped to %s", target_backend)

                # 3. Synchronize environment credentials & settings into new store if not present
                for option in get_config_options():
                    env_val = os.environ.get(option.effective_env_var)
                    if env_val:
                        existing_val = await new_store.get(option.db_key)
                        if not existing_val:
                            await new_store.set(
                                key=option.db_key,
                                value=env_val,
                                category=ConfigCategory.SYSTEM,
                                is_secret=(option.kind == OptionKind.SECRET or option.group == "Credentials"),
                                display_name=option.summary,
                            )

                # 4. Rehydrate plugins for the new store and reset active MCP sessions
                try:
                    from k8s_autopilot.plugins.discovery import discover_plugins_async
                    await discover_plugins_async(store=new_store)
                except Exception as p_err:
                    logger.warning("Plugin rehydration warning on storage switch: %s", p_err)

                try:
                    from k8s_autopilot.mcp.discovery import MCPDiscovery
                    from k8s_autopilot.mcp.session_manager import MCPSessionManager

                    mgr = MCPSessionManager.get_instance()
                    await mgr.close_all()
                    MCPSessionManager._instance = None
                    await MCPDiscovery(store=new_store).discover_and_sync_async(store=new_store)
                except Exception as m_err:
                    logger.debug("MCP session cleanup warning on storage switch: %s", m_err)
            except Exception as exc:
                logger.error("Failed hot-swapping checkpointer/config storage: %s", exc)
                errors.append(f"Storage switch error: {exc}")

        if updated > 0:
            from k8s_autopilot.config.settings import reload_from_store
            await reload_from_store(store)
            await _invalidate_mcp_and_agent_caches()

        resp: dict[str, Any] = {"success": True, "updated": updated}
        if errors:
            resp["errors"] = errors
        return JSONResponse(resp)

    async def delete_setting(request: Request) -> JSONResponse:
        """DELETE /api/settings/{key} — reset to default or delete custom configuration."""
        key = request.path_params["key"]
        option = _find_option(key)
        store = await get_config_store()

        if option is not None:
            target_key = option.db_key
            default_val = option.default
        else:
            target_key = key
            default_val = None

        deleted = await store.delete(target_key)
        if not deleted and target_key.upper() != target_key:
            deleted = await store.delete(target_key.upper())
            if deleted:
                target_key = target_key.upper()

        if not deleted:
            return JSONResponse({"detail": f"Setting '{key}' not found in DB"}, status_code=404)

        # Clear from os.environ and .env file across option keys and generic aliases
        from k8s_autopilot.config.paths import delete_env_vars
        keys_to_del = [target_key]
        if option and option.effective_env_var:
            keys_to_del.append(option.effective_env_var)
        for alias_key, canonical_target in _SETTING_ALIASES.items():
            if canonical_target in keys_to_del:
                keys_to_del.append(alias_key)
            elif alias_key in keys_to_del:
                keys_to_del.append(canonical_target)

        for k in set(keys_to_del):
            os.environ.pop(k, None)
        delete_env_vars(list(set(keys_to_del)))

        from k8s_autopilot.config.settings import reload_from_store
        await reload_from_store(store)
        await _invalidate_mcp_and_agent_caches()

        return JSONResponse({"success": True, "deleted": target_key, "reset_to_default": default_val})

    async def test_integration(request: Request) -> JSONResponse:
        """POST /api/settings/test-integration — test connectivity."""
        data = await request.json()
        integration_type = data.get("type")
        payload = data.get("payload", {})
        store = await get_config_store()

        success = False
        error = ""

        if integration_type == "slack":
            token = payload.get("SLACK_BOT_TOKEN")
            if token == "******" or not token:
                token = await store.get("SLACK_BOT_TOKEN")
            if token:
                success, error = await test_slack_token(token)
            else:
                error = "Missing SLACK_BOT_TOKEN"

        elif integration_type == "github":
            token = payload.get("GITHUB_PERSONAL_ACCESS_TOKEN")
            if token == "******" or not token:
                token = await store.get("GITHUB_PERSONAL_ACCESS_TOKEN")
            if token:
                success, error = await test_github_token(token)
            else:
                error = "Missing GITHUB_PERSONAL_ACCESS_TOKEN"
        elif integration_type in ("langsmith", "langchain"):
            key = payload.get("LANGSMITH_API_KEY") or payload.get("LANGCHAIN_API_KEY")
            if key == "******" or not key:
                key = await store.get("LANGCHAIN_API_KEY") or await store.get("LANGSMITH_API_KEY")
            if key:
                try:
                    from langsmith import Client

                    endpoint = (
                        payload.get("LANGSMITH_ENDPOINT")
                        or payload.get("LANGCHAIN_ENDPOINT")
                        or await store.get("LANGCHAIN_ENDPOINT")
                    )
                    client = Client(api_key=key, api_url=endpoint) if endpoint else Client(api_key=key)
                    list(client.list_projects(limit=1))
                    success = True
                except Exception as e:
                    error = str(e)
            else:
                error = "Missing LANGSMITH_API_KEY"
        elif integration_type == "argocd":
            server_url = payload.get("ARGOCD_SERVER_URL") or await store.get("ARGOCD_SERVER_URL") or os.environ.get("ARGOCD_SERVER_URL")
            token = payload.get("ARGOCD_AUTH_TOKEN")
            if token == "******" or not token:
                token = await store.get("ARGOCD_AUTH_TOKEN") or os.environ.get("ARGOCD_AUTH_TOKEN")
            insecure_raw = payload.get("ARGOCD_INSECURE")
            if insecure_raw is None:
                insecure_raw = await store.get("ARGOCD_INSECURE") or os.environ.get("ARGOCD_INSECURE", "true")
            insecure = str(insecure_raw).lower() in ("true", "1", "yes")

            if not server_url:
                error = "Missing ARGOCD_SERVER_URL"
            elif not token:
                error = "Missing ARGOCD_AUTH_TOKEN"
            else:
                import httpx
                try:
                    async with httpx.AsyncClient(verify=not insecure, timeout=10.0) as client:
                        resp = await client.get(
                            f"{server_url.rstrip('/')}/api/v1/applications?limit=1",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        if resp.status_code == 200:
                            success = True
                        elif resp.status_code == 401:
                            error = "Unauthorized: Invalid or expired ArgoCD token"
                        else:
                            error = f"ArgoCD API returned HTTP {resp.status_code}"
                except Exception as e:
                    error = f"Failed to connect to ArgoCD server: {e}"

        elif integration_type in ("postgres", "postgresql", "database"):
            uri = payload.get("POSTGRES_URI")
            if uri == "******" or not uri:
                uri = await store.get("POSTGRES_URI") or os.environ.get("POSTGRES_URI")
            if uri:
                try:
                    import asyncio
                    from psycopg import AsyncConnection

                    conn = await asyncio.wait_for(AsyncConnection.connect(uri), timeout=5.0)
                    async with conn:
                        async with conn.cursor() as cur:
                            await cur.execute("SELECT version();")
                            row = await cur.fetchone()
                            version = row[0] if row else "PostgreSQL"
                    return JSONResponse({"success": True, "version": version, "message": f"Connected: {version}"})
                except Exception as e:
                    return JSONResponse({"success": False, "error": f"Connection failed: {e}"})
            else:
                return JSONResponse({"success": False, "error": "Missing POSTGRES_URI"})
        elif integration_type == "sqlite":
            try:
                from k8s_autopilot.state.session import get_db_path
                import aiosqlite

                db_path = get_db_path()
                async with aiosqlite.connect(str(db_path)) as db:
                    async with db.execute("SELECT sqlite_version();") as cur:
                        row = await cur.fetchone()
                        version = row[0] if row else "SQLite"
                return JSONResponse({"success": True, "version": f"SQLite {version}", "message": f"Connected: SQLite {version}"})
            except Exception as e:
                return JSONResponse({"success": False, "error": f"SQLite check failed: {e}"})
        else:
            error = f"Unsupported integration type: {integration_type}"

        return JSONResponse({"success": success, "error": error})

    async def get_thread_trace(request: Request) -> JSONResponse:
        """GET /api/trace/{thread_id} — resolve LangSmith trace URL for a thread."""
        thread_id = request.path_params.get("thread_id", "default")
        from k8s_autopilot.config.langsmith import (
            _assemble_langsmith_thread_url,
            fetch_langsmith_project_url_or_raise,
            get_langsmith_project_name,
        )

        project_name = get_langsmith_project_name()
        if not project_name:
            return JSONResponse({
                "configured": False,
                "project_name": None,
                "url": None,
                "message": "LangSmith tracing is not enabled or configured.",
            })

        try:
            import asyncio

            project_url = await asyncio.to_thread(fetch_langsmith_project_url_or_raise, project_name)
            url = _assemble_langsmith_thread_url(project_url, thread_id)
            return JSONResponse({
                "configured": True,
                "project_name": project_name,
                "url": url,
                "project_url": project_url,
            })
        except Exception as e:
            return JSONResponse({
                "configured": True,
                "project_name": project_name,
                "url": None,
                "error": str(e),
            })

    async def get_settings_health(request: Request) -> JSONResponse:
        """GET /api/settings/health — health check."""
        try:
            store = await get_config_store()
            entries = await store.list_all()
            return JSONResponse({
                "status": "healthy",
                "db_connected": True,
                "entry_count": len(entries),
            })
        except Exception as e:
            return JSONResponse(
                {"status": "unhealthy", "db_connected": False, "error": str(e)},
                status_code=500,
            )

    # ── MCP Server CRUD & Diagnostics ─────────────────────

    async def list_mcp_servers(request: Request) -> JSONResponse:
        store = await get_config_store()
        servers = await store.list_mcp_servers()
        return JSONResponse(servers)

    async def list_mcp_servers_status(request: Request) -> JSONResponse:
        from k8s_autopilot.mcp.preload import preload_mcp_metadata, format_mcp_status_response
        store = await get_config_store()
        db_servers = await store.list_mcp_servers()
        config_map = {s["name"]: s for s in db_servers}
        probed_infos = await preload_mcp_metadata(config_map)
        status_payload = format_mcp_status_response(probed_infos)
        return JSONResponse(status_payload)

    async def probe_single_mcp_server(request: Request) -> JSONResponse:
        from k8s_autopilot.mcp.preload import probe_one_mcp_server
        name = request.path_params["name"]
        store = await get_config_store()
        server = await store.get_mcp_server(name)
        if not server:
            return JSONResponse({"detail": f"MCP server '{name}' not found"}, status_code=404)
        info = await probe_one_mcp_server(name, server)
        return JSONResponse(info.to_dict())

    async def toggle_mcp_server(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        data = await request.json()
        enabled = bool(data.get("enabled", True))
        store = await get_config_store()
        server = await store.get_mcp_server(name)
        if not server:
            return JSONResponse({"detail": f"MCP server '{name}' not found"}, status_code=404)
        server["enabled"] = enabled
        await store.upsert_mcp_server(server)

        # Update session manager, probed cache, and invalidate executor agents
        try:
            from k8s_autopilot.mcp.session_manager import MCPSessionManager
            from k8s_autopilot.mcp.preload import (
                probe_one_mcp_server,
                set_cached_mcp_server_info,
                evict_cached_mcp_server_info,
            )
            from k8s_autopilot.server.executor import A2AAutoPilotExecutor

            mcp_mgr = MCPSessionManager.get_instance()
            if not enabled:
                await mcp_mgr.remove_server(name)
                info = await probe_one_mcp_server(name, server)
                set_cached_mcp_server_info(info)
            else:
                evict_cached_mcp_server_info(name)
                info = await probe_one_mcp_server(name, server)
                set_cached_mcp_server_info(info)
                mcp_mgr._config[name] = server

            A2AAutoPilotExecutor.invalidate_all_agents()
        except Exception as exc:
            logger.debug("Failed syncing MCP state on toggle for %s: %s", name, exc)

        return JSONResponse({"name": name, "enabled": enabled, "success": True})

    async def upsert_mcp_servers(request: Request) -> JSONResponse:
        data = await request.json()
        store = await get_config_store()
        names: list[str] = []
        if isinstance(data, dict):
            if not data.get("name"):
                return JSONResponse({"detail": "MCP server name is required"}, status_code=400)
            await store.upsert_mcp_server(data)
            names.append(data["name"])
        elif isinstance(data, list):
            for server in data:
                if server.get("name"):
                    await store.upsert_mcp_server(server)
                    names.append(server["name"])
        else:
            return JSONResponse({"detail": "Invalid request body"}, status_code=400)

        # Update session manager & preload cache & invalidate agents
        try:
            from k8s_autopilot.mcp.session_manager import MCPSessionManager
            from k8s_autopilot.mcp.preload import (
                evict_cached_mcp_server_info,
                probe_one_mcp_server,
                set_cached_mcp_server_info,
            )
            from k8s_autopilot.server.executor import A2AAutoPilotExecutor

            mcp_mgr = MCPSessionManager.get_instance()
            for n in names:
                srv = await store.get_mcp_server(n)
                if srv:
                    if not srv.get("enabled", True):
                        await mcp_mgr.remove_server(n)
                        info = await probe_one_mcp_server(n, srv)
                        set_cached_mcp_server_info(info)
                    else:
                        await mcp_mgr.invalidate(n)
                        mcp_mgr._config[n] = srv
                        evict_cached_mcp_server_info(n)
                        info = await probe_one_mcp_server(n, srv)
                        set_cached_mcp_server_info(info)
            A2AAutoPilotExecutor.invalidate_all_agents()
        except Exception as exc:
            logger.debug("Failed syncing MCP state on upsert: %s", exc)

        if isinstance(data, dict):
            return JSONResponse({"success": True, "name": data["name"]})
        return JSONResponse({"success": True, "count": len(names), "names": names})

    async def delete_mcp_server(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        store = await get_config_store()
        deleted = await store.delete_mcp_server(name)
        if not deleted:
            return JSONResponse({"detail": "MCP server not found"}, status_code=404)

        try:
            from k8s_autopilot.mcp.session_manager import MCPSessionManager
            from k8s_autopilot.mcp.preload import evict_cached_mcp_server_info
            from k8s_autopilot.server.executor import A2AAutoPilotExecutor

            mcp_mgr = MCPSessionManager.get_instance()
            await mcp_mgr.remove_server(name)
            evict_cached_mcp_server_info(name)
            A2AAutoPilotExecutor.invalidate_all_agents()
        except Exception as exc:
            logger.debug("Failed syncing MCP state on delete for %s: %s", name, exc)

        return JSONResponse({"success": True, "name": name})

    async def get_raw_mcp_config(request: Request) -> JSONResponse:
        from k8s_autopilot.mcp.raw_config import export_raw_mcp_config
        store = await get_config_store()
        db_servers = await store.list_mcp_servers()
        payload = export_raw_mcp_config(db_servers)
        return JSONResponse(payload)

    async def put_raw_mcp_config(request: Request) -> JSONResponse:
        from k8s_autopilot.mcp.raw_config import import_raw_mcp_config
        data = await request.json()
        store = await get_config_store()
        try:
            synced = await import_raw_mcp_config(data, store)
            await _invalidate_mcp_and_agent_caches()
            return JSONResponse({"success": True, "synced_servers": synced})
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)



    # ── Marketplace CRUD ──────────────────────────────────

    async def list_marketplaces_route(request: Request) -> JSONResponse:
        store = await get_config_store()
        marketplaces = await store.list_marketplaces()
        return JSONResponse(marketplaces)

    async def add_marketplace_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import add_marketplace_source_async
        data = await request.json()
        source = data.get("source") or data.get("source_value")
        if not source:
            return JSONResponse({"detail": "Marketplace source is required"}, status_code=400)
        store = await get_config_store()
        try:
            marketplace = await add_marketplace_source_async(source, store=store)
            return JSONResponse({
                "name": marketplace.name,
                "plugin_count": len(marketplace.plugins),
                "plugins": [p.name for p in marketplace.plugins],
            })
        except Exception as exc:
            logger.warning("Failed to add marketplace: %s", exc)
            return JSONResponse({"detail": str(exc)}, status_code=400)

    async def delete_marketplace_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import remove_marketplace_async
        name = request.path_params["name"]
        store = await get_config_store()
        deleted = await remove_marketplace_async(name, store=store)
        if not deleted:
            return JSONResponse({"detail": "Marketplace not found"}, status_code=404)
        return JSONResponse({"success": True})

    # ── Plugin Discovery & Lifecycle ──────────────────────

    async def discover_available_plugins_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import list_available_plugins_async
        include_installed = request.query_params.get("include_installed", "false").lower() == "true"
        store = await get_config_store()
        available = await list_available_plugins_async(store=store, include_installed=include_installed)
        return JSONResponse(available)

    async def list_installed_plugins_route(request: Request) -> JSONResponse:
        store = await get_config_store()
        plugins = await store.list_plugins()
        return JSONResponse(plugins)

    async def list_plugin_errors_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import get_plugin_errors_async
        store = await get_config_store()
        errors = await get_plugin_errors_async(store=store)
        return JSONResponse(errors)

    async def install_plugin_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import install_plugin_async
        data = await request.json()
        plugin_id = data.get("plugin_id")
        scope = data.get("scope", "global")
        if not plugin_id:
            return JSONResponse({"detail": "plugin_id is required"}, status_code=400)
        store = await get_config_store()
        try:
            instance = await install_plugin_async(plugin_id, scope=scope, store=store)
            try:
                from k8s_autopilot.server.executor import A2AAutoPilotExecutor
                A2AAutoPilotExecutor.invalidate_all_agents()
            except Exception:
                pass
            return JSONResponse({
                "plugin_id": instance.plugin_id,
                "name": instance.name,
                "marketplace": instance.marketplace,
                "version": instance.version,
                "root": str(instance.root),
                "skills_count": len(instance.inventory.skills),
            })
        except Exception as exc:
            logger.warning("Failed to install plugin %s: %s", plugin_id, exc)
            return JSONResponse({"detail": str(exc)}, status_code=400)

    async def uninstall_plugin_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import uninstall_plugin_async
        plugin_id = request.path_params["plugin_id"]
        store = await get_config_store()
        success = await uninstall_plugin_async(plugin_id, store=store)
        if not success:
            return JSONResponse({"detail": "Plugin not found"}, status_code=404)
        try:
            from k8s_autopilot.server.executor import A2AAutoPilotExecutor
            A2AAutoPilotExecutor.invalidate_all_agents()
        except Exception:
            pass
        return JSONResponse({"success": True})

    async def enable_plugin_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import set_plugin_enabled_async
        plugin_id = request.path_params["plugin_id"]
        store = await get_config_store()
        await set_plugin_enabled_async(plugin_id, True, store=store)
        try:
            from k8s_autopilot.server.executor import A2AAutoPilotExecutor
            A2AAutoPilotExecutor.invalidate_all_agents()
        except Exception:
            pass
        return JSONResponse({"success": True})

    async def disable_plugin_route(request: Request) -> JSONResponse:
        from k8s_autopilot.plugins.discovery import set_plugin_enabled_async
        plugin_id = request.path_params["plugin_id"]
        store = await get_config_store()
        await set_plugin_enabled_async(plugin_id, False, store=store)
        try:
            from k8s_autopilot.server.executor import A2AAutoPilotExecutor
            A2AAutoPilotExecutor.invalidate_all_agents()
        except Exception:
            pass
        return JSONResponse({"success": True})

    # ── Plugin Metadata CRUD ──────────────────────────────

    async def list_plugins(request: Request) -> JSONResponse:
        store = await get_config_store()
        plugins = await store.list_plugins()
        return JSONResponse(plugins)

    async def upsert_plugin(request: Request) -> JSONResponse:
        data = await request.json()
        store = await get_config_store()
        await store.upsert_plugin(data)
        return JSONResponse({"success": True})

    async def delete_plugin(request: Request) -> JSONResponse:
        plugin_id = request.path_params["plugin_id"]
        store = await get_config_store()
        deleted = await store.delete_plugin(plugin_id)
        if not deleted:
            return JSONResponse({"detail": "Plugin not found"}, status_code=404)
        return JSONResponse({"success": True})

    # ── Skill Metadata & Discovery ────────────────────────

    async def list_skills(request: Request) -> JSONResponse:
        from k8s_autopilot.skills.loader import list_skills as discover_skills
        include_subagents = request.query_params.get("include_subagents", "false").lower() == "true"
        store = await get_config_store()
        skills = discover_skills(
            include_subagents=include_subagents,
            store=store,
        )
        return JSONResponse(skills)

    async def get_skill(request: Request) -> JSONResponse:
        from k8s_autopilot.skills.loader import get_skill_content_by_name
        name = request.path_params["name"]
        store = await get_config_store()
        skill, content = get_skill_content_by_name(name, store=store)
        if not skill:
            return JSONResponse({"detail": f"Skill '{name}' not found"}, status_code=404)
        return JSONResponse({
            "skill": skill,
            "content": content or "",
        })

    async def get_skill_content(request: Request) -> JSONResponse:
        from k8s_autopilot.skills.loader import get_skill_content_by_name
        name = request.path_params["name"]
        store = await get_config_store()
        skill, content = get_skill_content_by_name(name, store=store)
        if not skill or content is None:
            return JSONResponse({"detail": f"Content for skill '{name}' not found"}, status_code=404)
        return JSONResponse({
            "name": name,
            "content": content,
            "path": skill.get("path"),
            "scope": skill.get("scope"),
        })

    async def upsert_skill(request: Request) -> JSONResponse:
        data = await request.json()
        store = await get_config_store()
        await store.upsert_skill(data)
        return JSONResponse({"success": True})

    async def delete_skill(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        store = await get_config_store()
        deleted = await store.delete_skill(name)
        if not deleted:
            return JSONResponse({"detail": "Skill not found"}, status_code=404)
        return JSONResponse({"success": True})

    # ── Subagent Metadata CRUD ────────────────────────────

    async def list_subagents(request: Request) -> JSONResponse:
        store = await get_config_store()
        subagents = await store.list_subagents()
        return JSONResponse(subagents)

    async def upsert_subagent(request: Request) -> JSONResponse:
        data = await request.json()
        store = await get_config_store()
        await store.upsert_subagent(data)
        return JSONResponse({"success": True})

    async def delete_subagent(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        store = await get_config_store()
        deleted = await store.delete_subagent(name)
        if not deleted:
            return JSONResponse({"detail": "Subagent not found"}, status_code=404)
        return JSONResponse({"success": True})

    # ── Approval Mode CRUD ────────────────────────────────

    async def get_approval_mode(request: Request) -> JSONResponse:
        """DEPRECATED: Approval mode is now session-scoped via A2A metadata."""
        thread_id = request.query_params.get("thread_id")
        logger.warning(
            "DEPRECATED: Endpoint '%s' called; migrate to '%s'",
            "GET /api/settings/approval-mode",
            "A2A session metadata",
            extra={"endpoint": "GET /api/settings/approval-mode", "thread_id": thread_id},
        )

        if thread_id:
            try:
                from k8s_autopilot.api.service import get_thread_service
                from k8s_autopilot.security.approval_mode import (
                    approval_mode_key,
                    aread_approval_mode_from_store,
                )

                service = get_thread_service()
                store = getattr(service, "_checkpointer", None)
                if store and hasattr(store, "store"):
                    store = store.store
                if store:
                    stored = await aread_approval_mode_from_store(store, approval_mode_key(thread_id))
                    if stored:
                        return JSONResponse({"status": "success", "approval_mode": stored.value})
            except Exception as exc:
                logger.debug(f"Failed to read thread approval mode from store: {exc}")

        store = await get_config_store()
        db_mode = await store.get("APPROVAL_MODE")
        if db_mode:
            mode = db_mode.lower().strip()
        else:
            mode = getattr(get_settings(), "approval_mode", "manual") or "manual"

        return JSONResponse({"status": "success", "approval_mode": mode})

    async def update_approval_mode(request: Request) -> JSONResponse:
        """DEPRECATED: Approval mode is now session-scoped via A2A metadata."""
        logger.warning(
            "DEPRECATED: Endpoint '%s' called; migrate to '%s'",
            "POST /api/settings/approval-mode",
            "A2A session metadata",
            extra={"endpoint": "POST /api/settings/approval-mode"},
        )
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"detail": "Invalid JSON body"}, status_code=400)

        raw_mode = data.get("mode")
        if not raw_mode or str(raw_mode).lower().strip() not in ("manual", "auto", "yolo"):
            return JSONResponse(
                {"detail": "Invalid approval mode. Must be one of: manual, auto, yolo"},
                status_code=400,
            )

        mode = str(raw_mode).lower().strip()
        thread_id = data.get("thread_id")

        # 1. Update ConfigStore
        store = await get_config_store()
        await store.set(
            key="APPROVAL_MODE",
            value=mode,
            category=ConfigCategory.SECURITY,
            display_name="Approval Mode",
        )

        # 2. Update Settings
        settings = get_settings()
        settings.approval_mode = mode

        # 3. If thread_id is provided, update LangGraph store for thread
        if thread_id:
            try:
                from k8s_autopilot.api.service import get_thread_service
                from k8s_autopilot.security.approval_mode import (
                    APPROVAL_MODE_NAMESPACE,
                    approval_mode_key,
                    approval_mode_payload,
                )

                service = get_thread_service()
                store_obj = getattr(service, "_checkpointer", None)
                if store_obj and hasattr(store_obj, "store"):
                    store_obj = store_obj.store
                if store_obj and hasattr(store_obj, "aput"):
                    await store_obj.aput(
                        APPROVAL_MODE_NAMESPACE,
                        approval_mode_key(str(thread_id)),
                        approval_mode_payload(mode=mode),
                    )
            except Exception as exc:
                logger.debug(f"Failed to persist thread approval mode: {exc}")

        return JSONResponse({"status": "success", "approval_mode": mode})

    async def generate_argocd_token(request: Request) -> JSONResponse:
        """POST /api/settings/generate-argocd-token — authenticate with ArgoCD and generate token."""
        data = await request.json()
        store = await get_config_store()

        server_url = (data.get("server_url") or await store.get("ARGOCD_SERVER_URL") or os.environ.get("ARGOCD_SERVER_URL") or "").strip()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        insecure_raw = data.get("insecure")
        if insecure_raw is None:
            insecure_raw = await store.get("ARGOCD_INSECURE") or os.environ.get("ARGOCD_INSECURE", "true")
        insecure = str(insecure_raw).lower() in ("true", "1", "yes")

        if not server_url:
            return JSONResponse({"success": False, "error": "Missing ArgoCD Server URL"}, status_code=400)
        if not username:
            return JSONResponse({"success": False, "error": "Missing ArgoCD Username"}, status_code=400)
        if not password:
            return JSONResponse({"success": False, "error": "Missing ArgoCD Password"}, status_code=400)

        import httpx
        try:
            base = server_url.rstrip('/')
            session_url = f"{base}/api/v1/session"
            async with httpx.AsyncClient(verify=not insecure, timeout=15.0) as client:
                resp = await client.post(
                    session_url,
                    json={"username": username, "password": password},
                )
                if resp.status_code != 200:
                    err_msg = resp.text
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("error") or err_json.get("message") or err_msg
                    except Exception:
                        pass
                    return JSONResponse(
                        {"success": False, "error": f"ArgoCD authentication failed (HTTP {resp.status_code}): {err_msg}"},
                        status_code=400,
                    )

                body = resp.json()
                token = body.get("token")
                if not token:
                    return JSONResponse(
                        {"success": False, "error": "No token returned in ArgoCD session response"},
                        status_code=500,
                    )

                # Validate the token
                probe_resp = await client.get(
                    f"{base}/api/v1/applications?limit=1",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if probe_resp.status_code != 200:
                    return JSONResponse(
                        {"success": False, "error": f"Token generated but validation probe failed (HTTP {probe_resp.status_code})"},
                        status_code=400,
                    )

            # Persist in DB store and environment
            env_updates: dict[str, str] = {"ARGOCD_AUTH_TOKEN": token}
            await store.set("ARGOCD_AUTH_TOKEN", token)
            os.environ["ARGOCD_AUTH_TOKEN"] = token
            if data.get("server_url"):
                await store.set("ARGOCD_SERVER_URL", base)
                os.environ["ARGOCD_SERVER_URL"] = base
                env_updates["ARGOCD_SERVER_URL"] = base
            if data.get("insecure") is not None:
                insec_val = "true" if insecure else "false"
                await store.set("ARGOCD_INSECURE", insec_val)
                os.environ["ARGOCD_INSECURE"] = insec_val
                env_updates["ARGOCD_INSECURE"] = insec_val

            try:
                from k8s_autopilot.config.paths import upsert_env_vars
                upsert_env_vars(env_updates)
            except Exception:
                pass

            try:
                from k8s_autopilot.config.settings import reload_from_store
                await reload_from_store(store)
            except Exception:
                pass

            await _invalidate_mcp_and_agent_caches()

            return JSONResponse({
                "success": True,
                "token": token,
                "server_url": base,
                "message": "ArgoCD authentication token generated and saved successfully",
            })
        except httpx.ConnectError as ce:
            return JSONResponse({"success": False, "error": f"Cannot connect to ArgoCD at {server_url}: {ce}"}, status_code=400)
        except Exception as e:
            return JSONResponse({"success": False, "error": f"Unexpected error generating ArgoCD token: {e}"}, status_code=500)


    return [
        # Models
        Route("/api/models", list_models, methods=["GET"]),
        Route("/api/models/effort", get_model_effort_options, methods=["GET"]),
        Route("/api/models/select", select_model, methods=["POST"]),
        # Settings
        Route("/api/settings", get_settings_list, methods=["GET"]),
        Route("/api/settings/health", get_settings_health, methods=["GET"]),
        Route("/api/settings/approval-mode", get_approval_mode, methods=["GET"]),
        Route("/api/settings/approval-mode", update_approval_mode, methods=["POST"]),
        Route("/api/settings/test-integration", test_integration, methods=["POST"]),
        Route("/api/settings/generate-argocd-token", generate_argocd_token, methods=["POST"]),
        Route("/api/settings/{key:path}", get_setting, methods=["GET"]),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/settings/{key:path}", delete_setting, methods=["DELETE"]),
        # Traces
        Route("/api/trace", get_thread_trace, methods=["GET"]),
        Route("/api/trace/{thread_id:path}", get_thread_trace, methods=["GET"]),
        # MCP Servers
        Route("/api/mcp-servers/status", list_mcp_servers_status, methods=["GET"]),
        Route("/api/mcp-servers/{name}/probe", probe_single_mcp_server, methods=["POST"]),
        Route("/api/mcp-servers/{name}/toggle", toggle_mcp_server, methods=["POST"]),
        Route("/api/mcp-servers", list_mcp_servers, methods=["GET"]),
        Route("/api/mcp-servers", upsert_mcp_servers, methods=["PUT", "POST"]),
        Route("/api/mcp-servers/{name}", delete_mcp_server, methods=["DELETE"]),
        Route("/api/mcp/raw-config", get_raw_mcp_config, methods=["GET"]),
        Route("/api/mcp/raw-config", put_raw_mcp_config, methods=["PUT", "POST"]),
        # Marketplaces
        Route("/api/marketplaces", list_marketplaces_route, methods=["GET"]),
        Route("/api/marketplaces", add_marketplace_route, methods=["POST"]),
        Route("/api/marketplaces/{name:path}", delete_marketplace_route, methods=["DELETE"]),
        # Plugin Discovery & Lifecycle
        Route("/api/plugins/discover", discover_available_plugins_route, methods=["GET"]),
        Route("/api/plugins/installed", list_installed_plugins_route, methods=["GET"]),
        Route("/api/plugins/errors", list_plugin_errors_route, methods=["GET"]),
        Route("/api/plugins/install", install_plugin_route, methods=["POST"]),
        Route("/api/plugins/{plugin_id:path}/uninstall", uninstall_plugin_route, methods=["POST"]),
        Route("/api/plugins/{plugin_id:path}/enable", enable_plugin_route, methods=["POST"]),
        Route("/api/plugins/{plugin_id:path}/disable", disable_plugin_route, methods=["POST"]),
        # Plugins (Legacy / Direct CRUD)
        Route("/api/plugins", list_plugins, methods=["GET"]),
        Route("/api/plugins", upsert_plugin, methods=["POST", "PUT"]),
        Route("/api/plugins/{plugin_id:path}", delete_plugin, methods=["DELETE"]),
        # Skills
        Route("/api/skills", list_skills, methods=["GET"]),
        Route("/api/skills", upsert_skill, methods=["POST", "PUT"]),
        Route("/api/skills/{name:path}/content", get_skill_content, methods=["GET"]),
        Route("/api/skills/{name:path}", get_skill, methods=["GET"]),
        Route("/api/skills/{name:path}", delete_skill, methods=["DELETE"]),
        # Subagents
        Route("/api/subagents", list_subagents, methods=["GET"]),
        Route("/api/subagents", upsert_subagent, methods=["POST", "PUT"]),
        Route("/api/subagents/{name:path}", delete_subagent, methods=["DELETE"]),
    ]
