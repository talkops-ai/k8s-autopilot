"""Approval-mode state shared by clients, server, and middleware."""

from __future__ import annotations

from collections.abc import Generator, Mapping
import contextlib
from contextlib import contextmanager
from enum import StrEnum
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, TypedDict

from filelock import FileLock, Timeout

from k8s_autopilot.config.paths import STATE_DIR
from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)

APPROVAL_MODE_NAMESPACE: tuple[str, str] = ("k8s_autopilot", "approval_mode")
"""Store namespace for per-thread approval-mode control records."""

YOLO_ACKNOWLEDGEMENT_POLICY_VERSION = "2026-08-23"
"""Version of the unrestricted-mode warning that must be acknowledged."""

YOLO_WARNING_KEY = "yolo"
"""`[warnings].suppress` key that mutes the recurring 'YOLO is active' toast."""

AUTO_NOTICE_VERSION = "2026-08-23"
"""Version of the first-run Auto mode education notice."""


class ApprovalMode(StrEnum):
    """Tool-approval policy selected for an interactive thread."""

    MANUAL = "manual"  # Always pause for human approval on modifying actions
    AUTO = "auto"  # Classifier decides (safe auto-executes, dangerous prompts)
    YOLO = "yolo"  # Auto-approve everything (dev / CI / testing only)


class ApprovalModePayload(TypedDict):
    """Stored approval-mode control payload."""

    mode: str


def coerce_approval_mode(value: object) -> ApprovalMode:
    """Return a validated mode, failing closed to `manual`."""
    if isinstance(value, bool):
        return ApprovalMode.YOLO if value else ApprovalMode.MANUAL
    if value is None:
        return ApprovalMode.MANUAL
    if isinstance(value, ApprovalMode):
        return value
    if isinstance(value, str):
        try:
            return ApprovalMode(value.strip().lower())
        except ValueError:
            return ApprovalMode.MANUAL
    return ApprovalMode.MANUAL


def next_approval_mode(
    current: ApprovalMode | str | object,
    *,
    auto_eligible: bool = True,
    yolo_switcher_enabled: bool = True,
) -> ApprovalMode | None:
    """Return the next approval mode in the cycle: Manual -> Auto -> YOLO -> Manual."""
    mode = current if isinstance(current, ApprovalMode) else coerce_approval_mode(current)
    if mode is ApprovalMode.MANUAL:
        if auto_eligible:
            return ApprovalMode.AUTO
        if yolo_switcher_enabled:
            return ApprovalMode.YOLO
        return None
    if mode is ApprovalMode.AUTO:
        if yolo_switcher_enabled:
            return ApprovalMode.YOLO
        return ApprovalMode.MANUAL
    return ApprovalMode.MANUAL


def approval_mode_key(thread_id: str) -> str:
    """Return the store key for a thread's live approval mode."""
    return sha256(thread_id.encode("utf-8")).hexdigest()


def approval_mode_payload(
    *,
    mode: ApprovalMode | str | None = None,
    auto_approve: bool | None = None,
) -> ApprovalModePayload:
    """Return the stored approval-mode payload."""
    if (mode is None) == (auto_approve is None):
        msg = "Provide exactly one of mode or auto_approve"
        raise ValueError(msg)
    if auto_approve is not None:
        resolved = ApprovalMode.YOLO if auto_approve else ApprovalMode.MANUAL
    else:
        try:
            resolved = coerce_approval_mode(mode)
        except (TypeError, ValueError) as exc:
            msg = f"Invalid approval mode: {mode!r}"
            raise ValueError(msg) from exc
    return {"mode": resolved.value}


def _item_value(item: object) -> object:
    """Extract a store item's value."""
    if isinstance(item, Mapping):
        return item.get("value")
    return getattr(item, "value", None)


def _approval_mode_from_item(item: object) -> ApprovalMode | None:
    """Extract a validated approval mode from a Store item."""
    if item is None:
        logger.debug("Approval-mode store item is missing")
        return None

    value = _item_value(item)
    raw_mode = value.get("mode") if isinstance(value, Mapping) else None
    if isinstance(raw_mode, str):
        try:
            return ApprovalMode(raw_mode.lower())
        except ValueError:
            pass

    logger.warning("Approval-mode store item has invalid contents")
    return None


def read_approval_mode_from_store(store: object, key: str | None) -> ApprovalMode | None:
    """Read a live approval mode from the server-side LangGraph Store."""
    if store is None:
        logger.debug("Approval-mode store is unavailable")
        return None
    if not isinstance(key, str) or not key:
        logger.debug("Approval-mode store key is missing or invalid")
        return None

    try:
        from langgraph.checkpoint.base import BaseCheckpointSaver

        if isinstance(store, BaseCheckpointSaver):
            store = getattr(store, "store", None)
            if store is None:
                logger.debug("Approval-mode store is a CheckpointSaver without an attached store")
                return None
    except ImportError:
        pass

    get = getattr(store, "get", None)
    if get is None:
        aget = getattr(store, "aget", None)
        if callable(aget):

            async def _fetch() -> Any:
                res = aget(APPROVAL_MODE_NAMESPACE, key)
                return await res if inspect.isawaitable(res) else res

            try:
                import asyncio

                loop = None
                with contextlib.suppress(RuntimeError):
                    loop = asyncio.get_running_loop()
                if loop and loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        item = pool.submit(lambda: asyncio.run(_fetch())).result()
                else:
                    item = asyncio.run(_fetch())
            except Exception:
                return None
        else:
            logger.debug("Approval-mode store does not expose get() or aget()")
            return None
    else:
        try:
            item = get(APPROVAL_MODE_NAMESPACE, key)
        except Exception:
            logger.warning("Could not read approval-mode store item", exc_info=True)
            return None
    return _approval_mode_from_item(item)


async def aread_approval_mode_from_store(store: object, key: str | None) -> ApprovalMode | None:
    """Asynchronously read a live approval mode from a LangGraph Store."""
    if store is None:
        logger.debug("Approval-mode store is unavailable")
        return None
    if not isinstance(key, str) or not key:
        logger.debug("Approval-mode store key is missing or invalid")
        return None

    try:
        from langgraph.checkpoint.base import BaseCheckpointSaver

        if isinstance(store, BaseCheckpointSaver):
            store = getattr(store, "store", None)
            if store is None:
                logger.debug("Approval-mode store is a CheckpointSaver without an attached store")
                return None
    except ImportError:
        pass

    aget = getattr(store, "aget", None)
    get = getattr(store, "get", None)
    try:
        if callable(aget):
            result = aget(APPROVAL_MODE_NAMESPACE, key)
            item = await result if inspect.isawaitable(result) else result
        elif callable(get):
            item = get(APPROVAL_MODE_NAMESPACE, key)
        else:
            logger.debug("Approval-mode store does not expose get() or aget()")
            return None
    except Exception:
        logger.warning("Could not read approval-mode store item", exc_info=True)
        return None
    return _approval_mode_from_item(item)


async def awrite_approval_mode(
    agent: object,
    thread_id: str,
    *,
    mode: ApprovalMode | str | None = None,
    auto_approve: bool | None = None,
) -> str | None:
    """Persist approval mode through an agent's store client, LangGraph store, or thread service."""
    if not isinstance(thread_id, str) or not thread_id:
        return None

    key = approval_mode_key(thread_id)
    payload = approval_mode_payload(mode=mode, auto_approve=auto_approve)

    # 1. Agent with explicit remote/custom store method
    put = getattr(agent, "aput_store_item", None)
    if callable(put):
        try:
            res = put(APPROVAL_MODE_NAMESPACE, key, payload)
            if inspect.isawaitable(res):
                await res
            return key
        except Exception:
            logger.warning("Failed to put approval mode via aput_store_item", exc_info=True)

    # 2. Agent with attached LangGraph store
    store = getattr(agent, "store", None)
    if store is None:
        chk = getattr(agent, "_checkpointer", None) or getattr(agent, "checkpointer", None)
        if chk is not None:
            store = getattr(chk, "store", None)
    if store is None and (hasattr(agent, "aput") or hasattr(agent, "put")):
        store = agent

    if store is not None:
        aput = getattr(store, "aput", None)
        put_fn = getattr(store, "put", None)
        try:
            if callable(aput):
                res = aput(APPROVAL_MODE_NAMESPACE, key, payload)
                if inspect.isawaitable(res):
                    await res
                return key
            elif callable(put_fn):
                put_fn(APPROVAL_MODE_NAMESPACE, key, payload)
                return key
        except Exception:
            logger.warning("Failed to put approval mode to LangGraph store", exc_info=True)

    # 3. Fallback to thread service checkpointer store
    try:
        from k8s_autopilot.api.service import get_thread_service

        service = get_thread_service()
        if service:
            store_obj = getattr(service, "_checkpointer", None)
            if store_obj and hasattr(store_obj, "store"):
                store_obj = store_obj.store
            if store_obj and hasattr(store_obj, "aput"):
                await store_obj.aput(APPROVAL_MODE_NAMESPACE, key, payload)
                logger.info(
                    "Approval mode updated to '%s' for thread '%s'",
                    mode,
                    thread_id,
                    context_id=thread_id,
                    extra={"approval_mode": mode},
                )
                return key
    except Exception:
        pass

    logger.info(
        "Approval mode updated to '%s' for thread '%s'",
        mode,
        thread_id,
        context_id=thread_id,
        extra={"approval_mode": mode},
    )
    return None


_APPROVAL_STATE_LOCK_TIMEOUT_SECONDS = 5.0
_APPROVAL_STATE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_APPROVAL_STATE_THREAD_LOCKS_GUARD = threading.Lock()


def yolo_acknowledgement_path() -> Path:
    """Return the installation-local acknowledgement file path."""
    return STATE_DIR / "approval.json"


def _approval_state_lock_path(path: Path) -> Path:
    """Return the sibling lock file path for approval-state saves."""
    return path.with_name(f"{path.name}.lock")


def _approval_state_thread_lock(path: Path) -> threading.Lock:
    """Return the process-local mutation lock for an approval-state path."""
    key = str(path)
    with _APPROVAL_STATE_THREAD_LOCKS_GUARD:
        lock = _APPROVAL_STATE_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _APPROVAL_STATE_THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _approval_state_lock(path: Path) -> Generator[None, None, None]:
    """Serialize read-merge-write updates to install-local approval state."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            path.parent.chmod(0o700)
    file_lock = FileLock(
        str(_approval_state_lock_path(path)),
        timeout=_APPROVAL_STATE_LOCK_TIMEOUT_SECONDS,
        thread_local=False,
    )
    with _approval_state_thread_lock(path), file_lock:
        yield


def _load_approval_state(path: Path) -> dict[str, object]:
    """Load the install-local approval state file, or an empty dict."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning(
            "Ignoring unreadable or corrupt approval state at %s",
            path,
            exc_info=True,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring non-object approval state at %s", path)
        return {}
    return data


def _write_approval_state(
    path: Path,
    payload: Mapping[str, object],
    *,
    failure_label: str,
) -> bool:
    """Atomically write install-local approval state."""
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                path.parent.chmod(0o700)
        fd, raw_tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        tmp_path = Path(raw_tmp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        if os.name != "nt":
            with contextlib.suppress(OSError):
                tmp_path.chmod(0o600)
        tmp_path.replace(path)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                path.chmod(0o600)
    except OSError:
        logger.warning("Could not persist %s", failure_label, exc_info=True)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        return False
    return True


def _merge_approval_state(
    path: Path,
    updates: Mapping[str, object],
    *,
    failure_label: str,
) -> bool:
    """Load, merge, and write install-local approval state under a lock."""
    try:
        with _approval_state_lock(path):
            payload = {
                **_load_approval_state(path),
                **updates,
                "version": 1,
            }
            return _write_approval_state(
                path,
                payload,
                failure_label=failure_label,
            )
    except Timeout:
        logger.warning("Timed out waiting to persist %s", failure_label, exc_info=True)
        return False
    except OSError:
        logger.warning("Could not lock approval state for %s", failure_label, exc_info=True)
        return False


def has_yolo_acknowledgement(path: Path | None = None) -> bool:
    """Return whether the current unrestricted-mode warning was accepted."""
    target = path or yolo_acknowledgement_path()
    data = _load_approval_state(target)
    return (
        data.get("version") == 1
        and data.get("policy_version") == YOLO_ACKNOWLEDGEMENT_POLICY_VERSION
        and data.get("acknowledged") is True
    )


def save_yolo_acknowledgement(path: Path | None = None) -> bool:
    """Persist the current unrestricted-mode warning acknowledgement."""
    target = path or yolo_acknowledgement_path()
    return _merge_approval_state(
        target,
        {
            "policy_version": YOLO_ACKNOWLEDGEMENT_POLICY_VERSION,
            "acknowledged": True,
        },
        failure_label="YOLO acknowledgement",
    )


def has_auto_mode_notice(path: Path | None = None) -> bool:
    """Return whether the current Auto first-enable notice was already shown."""
    target = path or yolo_acknowledgement_path()
    data = _load_approval_state(target)
    return data.get("auto_notice_shown") is True and data.get("auto_notice_version") == AUTO_NOTICE_VERSION


def save_auto_mode_notice(path: Path | None = None) -> bool:
    """Persist that the Auto first-enable notice was shown."""
    target = path or yolo_acknowledgement_path()
    return _merge_approval_state(
        target,
        {
            "auto_notice_version": AUTO_NOTICE_VERSION,
            "auto_notice_shown": True,
        },
        failure_label="Auto mode notice",
    )
