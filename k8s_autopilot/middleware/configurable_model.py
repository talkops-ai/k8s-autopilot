"""Middleware for runtime model selection via LangGraph runtime context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from deepagents._models import (
    get_model_identifier,
    model_matches_spec,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langgraph.types import Command

from k8s_autopilot.middleware.registry import register_middleware
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

_ANTHROPIC_ONLY_SETTINGS: set[str] = {"cache_control", "thinking", "output_config"}
_GOOGLE_ONLY_SETTINGS: set[str] = {"thinking_level", "thinking_budget", "include_thoughts", "thinking_config"}
_OPENAI_ONLY_SETTINGS: set[str] = {"reasoning"}


def _get_ls_provider(model: object) -> str | None:
    """Return the provider name reported by the chat model."""
    try:
        ls_params = getattr(model, "_get_ls_params", None)
        if ls_params:
            params = ls_params()
            if isinstance(params, dict):
                provider = params.get("ls_provider")
                if isinstance(provider, str):
                    return provider
    except Exception:
        pass
    return None


def _is_anthropic_model(model: object) -> bool:
    return _get_ls_provider(model) == "anthropic"


@register_middleware(name="configurable_model")
class ConfigurableModelMiddleware(AgentMiddleware):
    """Swap the model or per-call settings from runtime.context."""

    def __init__(self, *, persist_model_state: bool = True) -> None:
        """Initialize ConfigurableModelMiddleware.

        Args:
            persist_model_state: Whether to record selected model state in checkpoints.
        """
        self._persist_model_state = persist_model_state

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Wrap synchronous model call to apply model or runtime parameter overrides.

        Args:
            request: Inbound model request.
            handler: Synchronous handler for model execution.

        Returns:
            ModelResponse or ExtendedModelResponse with checkpoint state updates.
        """
        resolved_req, resolved_spec, resolved_params = self._apply_overrides(request)
        response = handler(resolved_req)

        command = self._checkpoint_command(resolved_spec, resolved_params) if self._persist_model_state else None
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Wrap asynchronous model call to apply model or runtime parameter overrides.

        Args:
            request: Inbound model request.
            handler: Asynchronous handler for model execution.

        Returns:
            ModelResponse or ExtendedModelResponse with checkpoint state updates.
        """
        resolved_req, resolved_spec, resolved_params = await self._apply_overrides_async(request)
        response = await handler(resolved_req)

        command = self._checkpoint_command(resolved_spec, resolved_params) if self._persist_model_state else None
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)

    def _apply_overrides(self, request: ModelRequest) -> tuple[ModelRequest, str | None, dict[str, Any] | None]:
        """Apply model/param overrides synchronously."""
        ctx = self._get_context(request)
        if ctx is None:
            return request, self._model_spec_from_model(request.model), None

        model_spec = ctx.get("model")
        model_params = dict(ctx.get("model_params") or {})
        effort = ctx.get("reasoning_effort") or model_params.get("reasoning_effort")
        if effort and model_spec:
            from k8s_autopilot.model.reasoning import is_effort_supported_for_model, with_effort_model_params

            if is_effort_supported_for_model(model_spec, str(effort)):
                model_params = with_effort_model_params(model_spec, model_params, str(effort))

        model_result = None
        if model_spec and not model_matches_spec(request.model, model_spec):
            from k8s_autopilot.exceptions import ModelConfigError
            from k8s_autopilot.model.factory import create_model

            try:
                model_result = create_model(model_spec)
            except (ModelConfigError, Exception) as exc:
                logger.exception(
                    "Failed to resolve model override '%s'; keeping current model. Error: %s",
                    model_spec,
                    exc,
                )

        updated_request = self._build_overrides(request, model_result, model_params)

        resolved_spec = self._model_spec_from_result(model_result, updated_request.model)
        return updated_request, resolved_spec, model_params

    async def _apply_overrides_async(
        self, request: ModelRequest
    ) -> tuple[ModelRequest, str | None, dict[str, Any] | None]:
        """Apply model/param overrides asynchronously (offloading model construction)."""
        import asyncio

        ctx = self._get_context(request)
        if ctx is None:
            return request, self._model_spec_from_model(request.model), None

        model_spec = ctx.get("model")
        model_params = dict(ctx.get("model_params") or {})
        effort = ctx.get("reasoning_effort") or model_params.get("reasoning_effort")
        if effort and model_spec:
            from k8s_autopilot.model.reasoning import is_effort_supported_for_model, with_effort_model_params

            if is_effort_supported_for_model(model_spec, str(effort)):
                model_params = with_effort_model_params(model_spec, model_params, str(effort))

        model_result = None
        if model_spec and not model_matches_spec(request.model, model_spec):
            from k8s_autopilot.exceptions import ModelConfigError
            from k8s_autopilot.model.factory import create_model

            try:
                model_result = await asyncio.to_thread(create_model, model_spec)
            except (ModelConfigError, Exception) as exc:
                logger.exception(
                    "Failed to resolve model override '%s'; keeping current model. Error: %s",
                    model_spec,
                    exc,
                )

        updated_request = self._build_overrides(request, model_result, model_params)

        resolved_spec = self._model_spec_from_result(model_result, updated_request.model)
        return updated_request, resolved_spec, model_params

    def _build_overrides(
        self,
        request: ModelRequest,
        model_result: Any,
        model_params: dict[str, Any],
    ) -> ModelRequest:
        overrides: dict[str, Any] = {}

        new_model = getattr(model_result, "model", None) if model_result is not None else None
        if new_model is not None:
            overrides["model"] = new_model

            if request.system_prompt:
                from deepagents._models import get_model_provider

                from k8s_autopilot.config.settings import get_settings
                from k8s_autopilot.prompts import (
                    MODEL_IDENTITY_RE,
                    build_model_identity_section,
                )

                settings = get_settings()

                provider = _get_ls_provider(new_model) or get_model_provider(new_model) or "unknown"
                name = get_model_identifier(new_model) or "unknown"
                limit = model_result.context_limit if model_result is not None else settings.model_context_limit

                new_identity = build_model_identity_section(
                    name=name,
                    provider=provider,
                    context_limit=limit,
                )

                new_prompt = MODEL_IDENTITY_RE.sub(new_identity.rstrip() + "\n", request.system_prompt)
                overrides["system_prompt"] = new_prompt

        if model_params:
            overrides["model_settings"] = {**request.model_settings, **model_params}

        effective_model = new_model if new_model is not None else request.model
        effective_provider = (
            getattr(model_result, "provider", None)
            or _get_ls_provider(effective_model)
            or ""
        )

        settings_dict = overrides.get("model_settings", request.model_settings)
        if settings_dict:
            modified_settings = dict(settings_dict)
            changed = False

            # Switch away from Anthropic -> strip Anthropic settings
            if effective_provider != "anthropic":
                dropped = modified_settings.keys() & _ANTHROPIC_ONLY_SETTINGS
                if dropped:
                    for k in dropped:
                        modified_settings.pop(k)
                    changed = True

            # Switch away from Google -> strip Google settings
            if effective_provider not in {"google_genai", "google", "google_vertexai"}:
                dropped = modified_settings.keys() & _GOOGLE_ONLY_SETTINGS
                if dropped:
                    for k in dropped:
                        modified_settings.pop(k)
                    changed = True

            # Switch away from OpenAI -> strip OpenAI settings
            if effective_provider not in {"openai", "openai_codex", "azure_openai"}:
                dropped = modified_settings.keys() & _OPENAI_ONLY_SETTINGS
                if dropped:
                    for k in dropped:
                        modified_settings.pop(k)
                    changed = True
            else:
                # Effective provider is OpenAI/Azure OpenAI: ensure reasoning and reasoning_effort compose cleanly
                from k8s_autopilot.model.factory import _compose_openai_reasoning_effort

                composed = _compose_openai_reasoning_effort(
                    effective_provider,
                    modified_settings,
                    modified_settings.get("reasoning_effort"),
                    modified_settings.get("reasoning"),
                )
                if composed != modified_settings:
                    modified_settings = composed
                    changed = True

            if changed:
                overrides["model_settings"] = modified_settings

        if not overrides:
            return request

        return request.override(**overrides)

    def _get_context(self, request: ModelRequest) -> dict[str, Any] | None:
        runtime = request.runtime
        if runtime is None or runtime.context is None:
            return None
        ctx = runtime.context
        if isinstance(ctx, dict):
            return ctx
        # Map dataclass/object to dict
        return {
            "model": getattr(ctx, "model", None),
            "model_params": getattr(ctx, "model_params", None),
            "thread_id": getattr(ctx, "thread_id", None),
        }

    def _model_spec_from_model(self, model: Any) -> str | None:
        provider = _get_ls_provider(model)
        model_name = get_model_identifier(model)
        if provider and model_name:
            return f"{provider}:{model_name}"
        return None

    def _model_spec_from_result(self, model_result: Any, model: Any) -> str | None:
        if (
            model_result is not None
            and getattr(model_result, "provider", None)
            and getattr(model_result, "model_name", None)
        ):
            return f"{model_result.provider}:{model_result.model_name}"
        return self._model_spec_from_model(model)

    def _checkpoint_command(self, model_spec: str | None, model_params: dict[str, Any] | None) -> Command[Any] | None:
        update: dict[str, Any] = {}
        if model_spec:
            update["_model_spec"] = model_spec
        if model_params:
            update["_model_params"] = model_params
        if not update:
            return None
        return Command(update=update)
