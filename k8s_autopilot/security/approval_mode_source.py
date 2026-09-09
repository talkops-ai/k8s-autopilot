"""Unified approval-mode resolution and security policy evaluation for K8s Autopilot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from k8s_autopilot.security.approval_mode import (
    ApprovalMode,
    approval_mode_key,
    aread_approval_mode_from_store,
    coerce_approval_mode,
    read_approval_mode_from_store,
)
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "ApprovalPolicyResolver",
    "_DecidedMode",
    "_LiveLookup",
    "_approval_mode_source",
    "_aresolve_approval_mode",
    "_resolve_approval_mode",
]


@dataclass(frozen=True)
class _DecidedMode:
    """Context-only approval decision — no store lookup required."""

    mode: ApprovalMode


@dataclass(frozen=True)
class _LiveLookup:
    """A trusted Store key whose record must be read, failing closed to Auto/Manual."""

    key: str


class ApprovalPolicyResolver:
    """Evaluates execution context to determine active approval constraints."""

    @staticmethod
    def validate_store_key(raw_key: str, thread_id: object) -> str | None:
        """Validate that *raw_key* matches the canonical key for *thread_id*.

        Returns the key on success, ``None`` on mismatch or invalid input.
        """
        if not isinstance(thread_id, str) or not thread_id:
            logger.warning("Thread ID missing or invalid for approval-mode key validation")
            return None
        expected = approval_mode_key(thread_id)
        if raw_key != expected:
            logger.warning(
                "Approval-mode key %r does not match expected key for thread %r",
                raw_key,
                thread_id,
            )
            return None
        return raw_key

    @classmethod
    def resolve_source(cls, context: object) -> _DecidedMode | _LiveLookup:
        """Extract approval mode from invocation context (dataclass or dict)."""
        raw_key: object = None
        thread_id: object = None
        raw_mode: object = None
        legacy_auto: object = None
        has_typed_mode = False

        if (
            hasattr(context, "approval_mode_key")
            or hasattr(context, "approval_mode")
            or hasattr(context, "thread_id")
            or hasattr(context, "context_id")
        ):
            raw_key = getattr(context, "approval_mode_key", None)
            thread_id = (
                getattr(context, "thread_id", None)
                or getattr(context, "context_id", None)
                or getattr(context, "session_id", None)
            )
            raw_mode = getattr(context, "approval_mode", None)
            legacy_auto = getattr(context, "auto_approve", None)
            has_typed_mode = raw_mode is not None
        elif isinstance(context, dict):
            # Check direct keys
            raw_key = context.get("approval_mode_key")
            thread_id = context.get("thread_id") or context.get("context_id") or context.get("session_id")
            raw_mode = context.get("approval_mode")
            legacy_auto = context.get("auto_approve")

            # Check nested configurable or context dicts (e.g. RunnableConfig / ambient context)
            cfg_raw = context.get("configurable")
            cfg: dict[str, Any] = cfg_raw if isinstance(cfg_raw, dict) else {}
            ctx_raw = context.get("context")
            ctx_dict: dict[str, Any] = ctx_raw if isinstance(ctx_raw, dict) else {}

            if raw_key is None:
                raw_key = cfg.get("approval_mode_key") or ctx_dict.get("approval_mode_key")
            if thread_id is None:
                thread_id = (
                    cfg.get("thread_id")
                    or ctx_dict.get("thread_id")
                    or cfg.get("context_id")
                    or ctx_dict.get("context_id")
                )
            if raw_mode is None:
                raw_mode = cfg.get("approval_mode") or ctx_dict.get("approval_mode")
            if legacy_auto is None:
                legacy_auto = cfg.get("auto_approve") or ctx_dict.get("auto_approve")

            has_typed_mode = raw_mode is not None
        else:
            if context is not None:
                logger.warning(
                    "approval predicate received unexpected context type %s; interrupting for safety",
                    type(context).__name__,
                )
            return _DecidedMode(ApprovalMode.MANUAL)

        # 1. Store key present -> live store lookup
        if raw_key is not None:
            if not isinstance(raw_key, str) or not raw_key:
                logger.warning("Approval-mode Store key is malformed")
                return _DecidedMode(ApprovalMode.MANUAL)
            key = cls.validate_store_key(raw_key, thread_id)
            if key is None:
                return _DecidedMode(ApprovalMode.MANUAL)
            return _LiveLookup(key)

        # 2. Explicit typed mode present
        if has_typed_mode and raw_mode:
            requested = coerce_approval_mode(raw_mode)
            if requested is not ApprovalMode.MANUAL:
                return _DecidedMode(requested)
            if raw_mode == ApprovalMode.MANUAL.value and legacy_auto is True:
                return _DecidedMode(ApprovalMode.YOLO)
            return _DecidedMode(ApprovalMode.MANUAL)

        # 3. Thread ID available -> live store lookup
        if isinstance(thread_id, str) and thread_id:
            return _LiveLookup(approval_mode_key(thread_id))

        # 4. Legacy auto_approve flag fallback
        if legacy_auto is True:
            return _DecidedMode(ApprovalMode.YOLO)
        return _DecidedMode(ApprovalMode.MANUAL)

    @classmethod
    def resolve_sync(cls, context: object, store: object) -> ApprovalMode:
        """Resolve approval mode synchronously using local store."""
        source = cls.resolve_source(context)
        if isinstance(source, _DecidedMode):
            return source.mode
        mode = read_approval_mode_from_store(store, source.key)
        if mode is None:
            try:
                from k8s_autopilot.api.service import get_thread_service

                service = get_thread_service()
                if service:
                    store_obj = getattr(service, "_checkpointer", None)
                    store_obj = store_obj.store if store_obj and hasattr(store_obj, "store") else None
                    if store_obj and store_obj is not store:
                        mode = read_approval_mode_from_store(store_obj, source.key)
            except Exception:
                pass

        if mode is None:
            thread_id = None
            if hasattr(context, "thread_id"):
                thread_id = getattr(context, "thread_id", None)
            elif isinstance(context, dict):
                thread_id = context.get("thread_id") or (
                    context.get("configurable", {}).get("thread_id")
                    if isinstance(context.get("configurable"), dict)
                    else None
                )
            if isinstance(thread_id, str) and ":" in thread_id:
                root_key = approval_mode_key(thread_id.split(":", 1)[0])
                mode = read_approval_mode_from_store(store, root_key)
                if mode is None:
                    try:
                        from k8s_autopilot.api.service import get_thread_service

                        service = get_thread_service()
                        if service:
                            store_obj = getattr(service, "_checkpointer", None)
                            store_obj = store_obj.store if store_obj and hasattr(store_obj, "store") else None
                            if store_obj and store_obj is not store:
                                mode = read_approval_mode_from_store(store_obj, root_key)
                    except Exception:
                        pass

        if mode is None and isinstance(context, dict) and context.get("approval_mode"):
            mode = coerce_approval_mode(context.get("approval_mode"))

        if mode is None:
            logger.warning("Approval-mode store item is unavailable; interrupting for safety")
            return ApprovalMode.MANUAL
        return mode

    @classmethod
    async def resolve_async(cls, context: object, store: object) -> ApprovalMode:
        """Resolve approval mode asynchronously using server store."""
        source = cls.resolve_source(context)
        if isinstance(source, _DecidedMode):
            return source.mode
        mode = await aread_approval_mode_from_store(store, source.key)
        if mode is None:
            try:
                from k8s_autopilot.api.service import get_thread_service

                service = get_thread_service()
                if service:
                    store_obj = getattr(service, "_checkpointer", None)
                    store_obj = store_obj.store if store_obj and hasattr(store_obj, "store") else None
                    if store_obj and store_obj is not store:
                        mode = await aread_approval_mode_from_store(store_obj, source.key)
            except Exception:
                pass

        if mode is None:
            thread_id = None
            if hasattr(context, "thread_id"):
                thread_id = getattr(context, "thread_id", None)
            elif isinstance(context, dict):
                thread_id = context.get("thread_id") or (
                    context.get("configurable", {}).get("thread_id")
                    if isinstance(context.get("configurable"), dict)
                    else None
                )
            if isinstance(thread_id, str) and ":" in thread_id:
                root_key = approval_mode_key(thread_id.split(":", 1)[0])
                mode = await aread_approval_mode_from_store(store, root_key)
                if mode is None:
                    try:
                        from k8s_autopilot.api.service import get_thread_service

                        service = get_thread_service()
                        if service:
                            store_obj = getattr(service, "_checkpointer", None)
                            store_obj = store_obj.store if store_obj and hasattr(store_obj, "store") else None
                            if store_obj and store_obj is not store:
                                mode = await aread_approval_mode_from_store(store_obj, root_key)
                    except Exception:
                        pass

        if mode is None and isinstance(context, dict) and context.get("approval_mode"):
            mode = coerce_approval_mode(context.get("approval_mode"))

        if mode is None:
            logger.warning("Approval-mode store item is unavailable; interrupting for safety")
            return ApprovalMode.MANUAL
        return mode


def _validated_live_approval_key(raw_key: str, thread_id: object) -> str | None:
    return ApprovalPolicyResolver.validate_store_key(raw_key, thread_id)


def _approval_mode_source(context: object) -> _DecidedMode | _LiveLookup:
    return ApprovalPolicyResolver.resolve_source(context)


def _resolve_approval_mode(context: object, store: object) -> ApprovalMode:
    return ApprovalPolicyResolver.resolve_sync(context, store)


async def _aresolve_approval_mode(context: object, store: object) -> ApprovalMode:
    return await ApprovalPolicyResolver.resolve_async(context, store)
