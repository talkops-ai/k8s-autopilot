"""Rubric middleware with transport retry and context-aware grading.

``ReliableRubricMiddleware`` extends the SDK's ``RubricMiddleware`` with:

- **Grader middleware**: the nested grader receives verification middleware
  and runtime context without requiring those application-specific
  capabilities in the SDK's ``RubricMiddleware``.
- **Transport retry**: transient HTTP read errors (``httpx.ReadError``,
  ``RemoteProtocolError``) are retried exactly once. The retry re-invokes only
  the grader, never the task agent, so grader tools must be read-only or
  idempotent.
- **Internal message filtering**: goal-state notices and rubric-grader evidence
  are stripped from the message history before the SDK builds grader evidence,
  so they do not confuse the nested grader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NotRequired, cast
import warnings

# Private SDK helpers accessed at runtime via getattr
import deepagents.middleware.rubric as _rubric_mod
from deepagents.middleware.rubric import (
    RUBRIC_GRADER_MESSAGE_SOURCE,
    GraderResponse,
    RubricMiddleware as BaseRubricMiddleware,
    RubricState,
)
import httpx
from langchain.agents.middleware.types import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphBubbleUp

from k8s_autopilot.middleware.goal_state_notice import is_conversation_control_message
from k8s_autopilot.middleware.registry import register_middleware
from k8s_autopilot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from deepagents.middleware.rubric import RubricEvaluation
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AnyMessage
    from langchain_core.tools import BaseTool
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

_strategy_from_result = getattr(_rubric_mod, "_strategy_from_result", None)

__all__ = ["ReliableRubricMiddleware", "RubricMiddleware"]


# ---------------------------------------------------------------------------
# Transport error detection
# ---------------------------------------------------------------------------


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield an exception, its explicit/implicit causes, and group members once."""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)


def _is_transient_grader_transport_error(exc: BaseException) -> bool:
    """Return whether a grader failure is a retryable transport/read error."""
    for current in _exception_chain(exc):
        if isinstance(current, (httpx.ReadError, httpx.RemoteProtocolError)):
            return True
        error_type = type(current)
        if error_type.__module__.startswith("httpcore") and error_type.__name__ in {
            "ReadError",
            "RemoteProtocolError",
        }:
            return True
        if (
            error_type.__module__ == "aiohttp.http_exceptions"
            and error_type.__name__ == "TransferEncodingError"
            and "Not enough data to satisfy transfer length header" in str(current)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Internal message filtering
# ---------------------------------------------------------------------------


def _without_internal_control_messages(state: RubricState) -> RubricState:
    """Remove conversation control turns before the SDK builds grader evidence."""
    messages = state.get("messages", [])
    if not isinstance(messages, list):
        return state
    filtered: list[AnyMessage] = [message for message in messages if not is_conversation_control_message(message)]
    if len(filtered) == len(messages):
        return state
    updated = dict(state)
    updated["messages"] = filtered
    return cast("RubricState", updated)


# ---------------------------------------------------------------------------
# Nested grader state
# ---------------------------------------------------------------------------


class RubricGraderState(AgentState[GraderResponse]):
    """Nested-grader state used to scope verification-tool budgets."""

    rubric_grading_operation_id: NotRequired[str]


def _safe_build_grader_payload(
    middleware: Any,
    grader_state: Any,
    iteration: int,
    correction: str | None = None,
) -> str:
    build_fn = getattr(middleware, "_build_grader_payload", None)
    if not callable(build_fn):
        return ""
    if correction is not None:
        try:
            return str(build_fn(grader_state, iteration, correction))
        except TypeError:
            try:
                return str(build_fn(grader_state, iteration, correction=correction))
            except TypeError:
                pass
    return str(build_fn(grader_state, iteration))


# ---------------------------------------------------------------------------
# ReliableRubricMiddleware
# ---------------------------------------------------------------------------


with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="The middleware `RubricMiddleware` is in beta",
        category=Warning,
    )

    @register_middleware(name="reliable_rubric")
    class ReliableRubricMiddleware(BaseRubricMiddleware):
        """Run a context-aware nested grader and retry transient transport failures."""

        def __init__(
            self,
            *,
            model: str | BaseChatModel | None = None,
            system_prompt: str | None = None,
            tools: Sequence[BaseTool] | None = None,
            grader_middleware: Sequence[AgentMiddleware[Any, Any]] | None = None,
            grader_context_schema: type[Any] | None = None,
            max_iterations: int = 3,
            on_evaluation: Callable[[RubricEvaluation], None] | None = None,
        ) -> None:
            """Initialize the ReliableRubricMiddleware with grading model, prompt, and options.

            Args:
                model: Optional chat model or model spec string for grading.
                system_prompt: Optional custom evaluation prompt.
                tools: Optional tools accessible to the grader agent.
                grader_middleware: Optional sequence of middleware for the grader agent.
                grader_context_schema: Optional context schema type for the grader.
                max_iterations: Maximum evaluation loop iterations.
                on_evaluation: Optional callback invoked upon rubric evaluation.
            """
            kwargs: dict[str, Any] = {}
            if model is not None:
                kwargs["model"] = model
            if system_prompt is not None:
                kwargs["system_prompt"] = system_prompt
            if tools is not None:
                kwargs["tools"] = tools
            kwargs["max_iterations"] = max_iterations
            if on_evaluation is not None:
                kwargs["on_evaluation"] = on_evaluation
            super().__init__(**kwargs)
            self._grader_middleware = list(grader_middleware or ())
            self._grader_context_schema = grader_context_schema

        @hook_config(can_jump_to=["model"])
        def after_agent(
            self,
            state: RubricState,
            runtime: Runtime[Any],
        ) -> dict[str, Any] | None:
            """Grade synchronously while preserving nested graph interrupts."""
            prep = self._prepare_evaluation(state, runtime)
            if prep is None:
                return None
            grading_run_id, iteration = prep

            try:
                graded = self._grade(
                    state,
                    iteration,
                    context=getattr(runtime, "context", None),
                )
            except GraphBubbleUp:
                raise
            except Exception as exc:
                return self._handle_grader_exception(
                    runtime,
                    state,
                    grading_run_id,
                    iteration,
                    exc,
                )

            return self._finalize_evaluation(
                graded,
                state,
                runtime,
                grading_run_id,
                iteration,
            )

        @hook_config(can_jump_to=["model"])
        async def aafter_agent(
            self,
            state: RubricState,
            runtime: Runtime[Any],
        ) -> dict[str, Any] | None:
            """Grade asynchronously while preserving nested graph interrupts."""
            prep = self._prepare_evaluation(state, runtime)
            if prep is None:
                return None
            grading_run_id, iteration = prep

            try:
                graded = await self._agrade(
                    state,
                    iteration,
                    context=getattr(runtime, "context", None),
                )
            except GraphBubbleUp:
                raise
            except Exception as exc:
                return self._handle_grader_exception(
                    runtime,
                    state,
                    grading_run_id,
                    iteration,
                    exc,
                )

            return self._finalize_evaluation(
                graded,
                state,
                runtime,
                grading_run_id,
                iteration,
            )

        def _compose_update(
            self,
            state: RubricState,
            evaluation: Any,
        ) -> dict[str, Any]:
            """Compose state update and synchronize goal completion lifecycle."""
            update = super()._compose_update(state, evaluation)
            result = str(evaluation.get("result", "")).lower()

            # If an active goal is present in state, bridge rubric verdict to goal lifecycle
            if state.get("_goal_objective"):
                if result in ("satisfied", "passed", "complete"):
                    update["_goal_status"] = "complete"
                    pending_note = state.get("_pending_goal_completion_note")
                    if pending_note:
                        update["_goal_status_note"] = pending_note
                        update["_pending_goal_completion_note"] = None
                    logger.info(
                        "Goal '%s' marked complete following satisfied rubric evaluation",
                        str(state.get("_goal_objective", ""))[:40],
                    )
                elif result in ("max_iterations_reached", "failed"):
                    update["_goal_status"] = "blocked"
                    update["_goal_status_note"] = (
                        evaluation.get("explanation") or "Acceptance criteria not satisfied within iteration limit."
                    )
                    logger.warning(
                        "Goal '%s' marked blocked: rubric evaluation terminated with %s",
                        str(state.get("_goal_objective", ""))[:40],
                        result,
                    )

            return update

        # ------------------------------------------------------------------
        # Grader creation (override to inject middleware and context schema)
        # ------------------------------------------------------------------

        def _ensure_grader(self) -> Any:
            """Create or return the nested grader agent.

            Override of the SDK's ``_ensure_grader`` to inject the application's
            grader middleware and context schema.
            """
            if self._grader is not None:
                return self._grader

            from deepagents._models import resolve_model
            from langchain.agents import create_agent

            resolved_model = resolve_model(self._model)
            self._resolved_model = resolved_model
            logger.debug(
                "[HITL_TRACE_DEBUG] ReliableRubric._ensure_grader created agent | grader_middleware=%s",
                [getattr(m, "name", type(m).__name__) for m in self._grader_middleware],
            )
            self._grader = create_agent(
                model=resolved_model,
                system_prompt=self._system_prompt,
                tools=self._tools,
                middleware=self._grader_middleware,
                name=RUBRIC_GRADER_MESSAGE_SOURCE,
                response_format=GraderResponse,
                state_schema=RubricGraderState,
                context_schema=self._grader_context_schema,
            )
            return self._grader

        # ------------------------------------------------------------------
        # Private SDK method accessors (Pyrefly-safe wrappers)
        # ------------------------------------------------------------------
        # The parent ``RubricMiddleware`` exposes ``_grader_trace_metadata``,
        # ``_record_grader_trace_metadata``, and ``_grader_invocation_config``
        # as private methods. Pyrefly cannot resolve inherited private
        # attributes, so we access them via ``getattr`` in thin wrappers.

        def _get_trace_metadata(self, **kwargs: Any) -> dict[str, str]:
            """Delegate to ``RubricMiddleware._grader_trace_metadata``."""
            fn = getattr(self, "_grader_trace_metadata", None)
            if fn is not None:
                return fn(**kwargs)
            return {}

        def _record_trace_metadata(self, metadata: dict[str, str]) -> None:
            """Delegate to ``RubricMiddleware._record_grader_trace_metadata``."""
            fn = getattr(self, "_record_grader_trace_metadata", None)
            if fn is not None:
                fn(metadata)

        def _get_invocation_config(self, metadata: dict[str, str]) -> dict[str, Any]:
            """Delegate to ``RubricMiddleware._grader_invocation_config``."""
            fn = getattr(self, "_grader_invocation_config", None)
            if fn is not None:
                return fn(metadata)
            return {}

        def _grader_input(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
        ) -> dict[str, Any]:
            grading_run_id = state.get("_current_grading_run_id") or "untracked"
            grader_state = _without_internal_control_messages(state)
            payload = _safe_build_grader_payload(self, grader_state, iteration, correction)
            return {
                "messages": [HumanMessage(content=payload)],
                "rubric_grading_operation_id": f"{grading_run_id}:{iteration}",
            }

        def _grade_once(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None,
        ) -> GraderResponse:
            grader = self._ensure_grader()
            metadata = self._get_trace_metadata()
            self._record_trace_metadata(metadata)
            result = grader.invoke(
                self._grader_input(state, iteration, correction),
                config=self._get_invocation_config(metadata),
                context=context,
            )
            strategy = _strategy_from_result(result) if _strategy_from_result is not None else None
            self._record_trace_metadata(self._get_trace_metadata(effective_strategy=strategy))
            return self._extract_graded(result)

        async def _agrade_once(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None,
        ) -> GraderResponse:
            grader = self._ensure_grader()
            metadata = self._get_trace_metadata()
            self._record_trace_metadata(metadata)
            result = await grader.ainvoke(
                self._grader_input(state, iteration, correction),
                config=self._get_invocation_config(metadata),
                context=context,
            )
            strategy = _strategy_from_result(result) if _strategy_from_result is not None else None
            self._record_trace_metadata(self._get_trace_metadata(effective_strategy=strategy))
            return self._extract_graded(result)

        def _grade(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            try:
                return self._grade_once(state, iteration, correction, context=context)
            except Exception as exc:
                if not _is_transient_grader_transport_error(exc):
                    raise
                logger.warning(
                    "Rubric grader transport failed; retrying grading once",
                    exc_info=True,
                )
            return self._grade_once(state, iteration, correction, context=context)

        async def _agrade(
            self,
            state: RubricState,
            iteration: int,
            correction: str | None = None,
            *,
            context: object | None = None,
        ) -> GraderResponse:
            try:
                return await self._agrade_once(state, iteration, correction, context=context)
            except Exception as exc:
                if not _is_transient_grader_transport_error(exc):
                    raise
                logger.warning(
                    "Rubric grader transport failed; retrying grading once",
                    exc_info=True,
                )
            return await self._agrade_once(state, iteration, correction, context=context)


RubricMiddleware = ReliableRubricMiddleware
