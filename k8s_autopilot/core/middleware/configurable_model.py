"""Middleware for runtime model selection via LangGraph runtime context."""

from __future__ import annotations

import asyncio
from k8s_autopilot.utils.logger import AgentLogger
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models import BaseChatModel
from langgraph.types import Command

logger = AgentLogger("ConfigurableModel")


# ---------------------------------------------------------------------------
# Inline CLIContext from _cli_context.py
# ---------------------------------------------------------------------------

@dataclass
class CLIContextSchema:
    model: str | None = None
    model_params: dict[str, Any] = field(default_factory=dict)
    auto_approve: bool = False
    approval_mode_key: str | None = None
    thread_id: str | None = None
    blocked_goal_retry_context: str | None = None
    reasoning_effort: str | None = None  # "low" | "medium" | "high"


# ---------------------------------------------------------------------------
# Resolved Request Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ResolvedModelRequest:
    request: ModelRequest
    model_spec: str | None
    model_params: dict[str, Any] | None = None
    model_params_known: bool = False


def _get_ls_provider(model: object) -> str | None:
    try:
        ls_params = model._get_ls_params()  # type: ignore
        if isinstance(ls_params, dict):
            return ls_params.get("ls_provider")
    except Exception:
        pass
    return None


def _is_anthropic_model(model: object) -> bool:
    """Check whether a resolved model reports ``'anthropic'`` as its provider."""
    return _get_ls_provider(model) == "anthropic"


def _is_fireworks_model(model: object) -> bool:
    return _get_ls_provider(model) == "fireworks"


# Settings injected by Anthropic-specific middleware that must be stripped
# when swapping to a non-Anthropic provider.  Adapted from dcode.
_ANTHROPIC_ONLY_SETTINGS: frozenset[str] = frozenset({"cache_control"})

# Provider-specific thinking config keys that must be cleaned on swap
_PROVIDER_SPECIFIC_SETTINGS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"cache_control", "thinking"}),
    "google_genai": frozenset({"include_thoughts", "thinking_budget", "thinking_level"}),
    "ollama": frozenset({"think"}),
}


def _strip_cross_provider_settings(
    model_settings: dict[str, Any],
    old_model: object,
    new_model: object,
) -> dict[str, Any]:
    """Strip provider-specific settings when swapping providers.

    Adapted from dcode's cross-provider setting stripping. Prevents
    ``TypeError`` crashes when e.g. ``cache_control`` (Anthropic-only)
    is passed to the OpenAI SDK.

    Returns:
        A new settings dict with incompatible keys removed, or the
        original dict if nothing was stripped.
    """
    old_provider = _get_ls_provider(old_model) or ""
    new_provider = _get_ls_provider(new_model) or ""

    if old_provider == new_provider:
        return model_settings

    # Collect all keys that belong to the OLD provider but not the new one
    keys_to_strip = _PROVIDER_SPECIFIC_SETTINGS.get(old_provider, frozenset())
    if not keys_to_strip:
        return model_settings

    dropped = model_settings.keys() & keys_to_strip
    if not dropped:
        return model_settings

    logger.debug(f"Stripped {old_provider}-only settings {dropped} for {new_provider} model")
    return {k: v for k, v in model_settings.items() if k not in dropped}


def _with_fireworks_session_settings(settings: dict[str, Any], thread_id: str) -> dict[str, Any]:
    # Shallow-copy and append Fireworks session ID settings if needed
    res = dict(settings)
    if "user_metadata" not in res:
        res["user_metadata"] = {}
    res["user_metadata"]["session_id"] = thread_id
    return res


def _checkpoint_command(resolved: _ResolvedModelRequest) -> Command[Any] | None:
    update: dict[str, Any] = {}
    if resolved.model_spec is not None:
        update["_model_spec"] = resolved.model_spec
    if resolved.model_params_known:
        update["_model_params"] = resolved.model_params
    if not update:
        return None
    return Command(update=update)


def _get_context(request: ModelRequest) -> CLIContextSchema | None:
    ctx = getattr(request.runtime, "context", None)
    if isinstance(ctx, CLIContextSchema):
        return ctx
    if isinstance(ctx, dict):
        return CLIContextSchema(
            model=ctx.get("model"),
            model_params=ctx.get("model_params") or {},
            auto_approve=ctx.get("auto_approve", False),
            approval_mode_key=ctx.get("approval_mode_key"),
            thread_id=ctx.get("thread_id"),
            blocked_goal_retry_context=ctx.get("blocked_goal_retry_context"),
            reasoning_effort=ctx.get("reasoning_effort"),
        )
    return None


def _apply_overrides(request: ModelRequest) -> _ResolvedModelRequest:
    ctx = _get_context(request)
    if not ctx:
        return _ResolvedModelRequest(request, None)

    overrides: dict[str, Any] = {}
    
    # Switch model if needed (if new model is provided and differs)
    model_spec = ctx.model
    create_model_fn = getattr(request.runtime, "create_model", None)
    if model_spec and create_model_fn is not None:
        # Swapping is possible only if runtime has create_model factory
        try:
            new_model = create_model_fn(model_spec)
            overrides["model"] = new_model
        except Exception:
            logger.warning(f"Failed to dynamically create model for {model_spec}", exc_info=True)

    # Param merge
    model_params = ctx.model_params
    if model_params:
        overrides["model_settings"] = {**request.model_settings, **model_params}

    # ── Reasoning effort mapping (ModelCapabilityRegistry) ────────────
    # Translate generic "low/medium/high" effort to provider-specific kwargs
    if ctx.reasoning_effort:
        try:
            from k8s_autopilot.core.middleware.model_capability import get_capability_registry
            effective_model = overrides.get("model", request.model)
            provider = _get_ls_provider(effective_model) or ""
            model_name = getattr(effective_model, "model", None) or getattr(effective_model, "model_name", None)
            registry = get_capability_registry()
            reasoning_params = registry.map_reasoning_params(provider, ctx.reasoning_effort, model_name)
            if reasoning_params:
                settings = overrides.get("model_settings", request.model_settings)
                overrides["model_settings"] = {**settings, **reasoning_params}
        except Exception:
            logger.warning("Failed to map reasoning effort", exc_info=True)

    effective_model = overrides.get("model", request.model)
    if ctx.thread_id and _is_fireworks_model(effective_model):
        settings = overrides.get("model_settings", request.model_settings)
        overrides["model_settings"] = _with_fireworks_session_settings(settings, ctx.thread_id)

    if not overrides:
        return _ResolvedModelRequest(request, None)

    # ── Cross-provider setting stripping (dcode pattern) ──────────────
    # When swapping providers (e.g., Anthropic → OpenAI), strip settings
    # that would cause TypeError/errors on the new provider's SDK.
    new_model = overrides.get("model")
    if new_model is not None:
        settings = overrides.get("model_settings", request.model_settings)
        cleaned = _strip_cross_provider_settings(
            settings, request.model, new_model,
        )
        if cleaned is not settings:
            overrides["model_settings"] = cleaned

    return _ResolvedModelRequest(
        request=request.override(**overrides),
        model_spec=model_spec,
        model_params=model_params or None,
        model_params_known=bool(model_params),
    )


from k8s_autopilot.core.middleware.registry import BaseAgentMiddleware, register_middleware


@register_middleware(name="configurable_model")
class ConfigurableModelMiddleware(BaseAgentMiddleware):
    """Swap the model or per-call settings from `runtime.context`."""

    def __init__(self, *, persist_model_state: bool = True) -> None:
        super().__init__()
        self._persist_model_state = persist_model_state

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        resolved = _apply_overrides(request)
        response = handler(resolved.request)
        command = _checkpoint_command(resolved) if self._persist_model_state else None
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        resolved = _apply_overrides(request)
        response = await handler(resolved.request)
        command = _checkpoint_command(resolved) if self._persist_model_state else None
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)
