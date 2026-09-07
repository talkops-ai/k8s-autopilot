"""Canonical internal messages for goal state and work continuation.

Provides the goal-state notice system used by ``GoalToolsMiddleware`` to keep
the model oriented on goal/rubric changes across conversation turns. Notices
are append-only ``HumanMessage``s with structured metadata that allow later
notices to supersede earlier ones.

Key concepts:

- **Goal-state notice** — a ``HumanMessage`` with ``lc_source=goal_state`` that
  summarizes the current goal/rubric status in model-visible natural language.
  Each notice declares that it supersedes all prior notices of the same kind.

- **Goal continuation** — a ``HumanMessage`` with ``lc_source=goal_control``
  that instructs the model to resume work after a goal is set, amended, or
  resumed by the user.

- **State fingerprint** — a SHA-256 digest of the canonical projection, used to
  detect whether the persisted notice still matches the current state.
"""

from __future__ import annotations

import hashlib
import html
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final, Literal, TypedDict, cast

from k8s_autopilot._constants import SYSTEM_MESSAGE_PREFIX

if TYPE_CHECKING:
    from langchain_core.messages import HumanMessage

GOAL_CONTROL_MESSAGE_SOURCE: Final = "goal_control"
"""``lc_source`` value for goal-lifecycle continuation messages."""

GOAL_STATE_MESSAGE_SOURCE: Final = "goal_state"
"""``lc_source`` value for canonical goal/rubric state notices."""

GOAL_MESSAGE_SCHEMA_VERSION: Final = 1
"""Schema version stamped on every goal notice and continuation."""

_GOAL_MESSAGE_SCHEMA_KEY: Final = "goal_message_schema_version"
_GOAL_MESSAGE_KIND_KEY: Final = "goal_message_kind"
_GOAL_INTERNAL_SOURCES = frozenset(
    {GOAL_CONTROL_MESSAGE_SOURCE, GOAL_STATE_MESSAGE_SOURCE}
)
_CONVERSATION_CONTROL_SOURCES = frozenset({*_GOAL_INTERNAL_SOURCES, "rubric_grader"})
_USER_HIDDEN_SOURCES = frozenset({*_CONVERSATION_CONTROL_SOURCES, "summarization"})
_LEGACY_CONVERSATION_CONTROL_PREFIXES = (
    f"{SYSTEM_MESSAGE_PREFIX} Goal set by the user",
    f"{SYSTEM_MESSAGE_PREFIX} Goal amended by the user.",
    f"{SYSTEM_MESSAGE_PREFIX} Goal resumed by the user.",
    f"{SYSTEM_MESSAGE_PREFIX} Goal/rubric state changed.",
    f"{SYSTEM_MESSAGE_PREFIX} Task interrupted by user.",
)

GoalTransition = Literal["created", "amended", "resumed"]
"""Goal lifecycle transition that triggers a continuation message."""


class GoalStateProjection(TypedDict):
    """Canonical goal/rubric fields used for notices and fingerprints.

    Projected from the authoritative checkpoint channels so that notice
    rendering and fingerprinting always use the same data shape regardless
    of the upstream state schema.
    """

    goal_objective: str | None
    goal_status: str | None
    goal_actionable: bool
    goal_rubric: str | None
    goal_status_note: str | None
    rubric_criteria: str | None
    rubric_source: str | None


class GoalStateNoticeInfo(TypedDict):
    """Metadata extracted from a canonical goal-state notice."""

    event_id: str
    state_fingerprint: str
    schema_version: int


# ---------------------------------------------------------------------------
# Message introspection helpers
# ---------------------------------------------------------------------------


def _field(message: object, name: str) -> object:
    """Read a field from a message object or serialized mapping."""
    if isinstance(message, Mapping):
        return message.get(name)
    return getattr(message, name, None)


def message_text(message: object) -> str:
    """Return ordinary text from a local or serialized message."""
    content = _field(message, "content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type") in {
            "text",
            "text-plain",
        }:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def message_additional_kwargs(message: object) -> Mapping[str, object]:
    """Return message metadata from a local or serialized message."""
    value = _field(message, "additional_kwargs")
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def message_source(message: object) -> str | None:
    """Return a message's ``lc_source`` value when present."""
    source = message_additional_kwargs(message).get("lc_source")
    return source if isinstance(source, str) and source else None


def is_human_message(message: object) -> bool:
    """Return whether a local or serialized message has the human role."""
    role = _field(message, "role")
    if isinstance(role, str) and role.lower() in {"user", "human"}:
        return True
    kind = _field(message, "type")
    if isinstance(kind, str) and kind.lower() in {"human", "humanmessage", "user"}:
        return True
    return type(message).__name__ == "HumanMessage"


# ---------------------------------------------------------------------------
# Message classification predicates
# ---------------------------------------------------------------------------


def is_goal_internal_message(message: object) -> bool:
    """Return whether a message is a goal-state notice or continuation."""
    return (
        is_human_message(message) and message_source(message) in _GOAL_INTERNAL_SOURCES
    )


def is_goal_state_message(message: object) -> bool:
    """Return whether a message claims to be a goal-state notice."""
    if not is_human_message(message):
        return False
    return message_source(message) == GOAL_STATE_MESSAGE_SOURCE or message_text(
        message
    ).startswith(f"{SYSTEM_MESSAGE_PREFIX} Goal/rubric state changed.")


def latest_human_is_unsaved_goal_continuation(
    messages: Sequence[object],
) -> bool:
    """Return whether the latest human turn carries an unsaved goal fallback."""
    for message in reversed(messages):
        if not is_human_message(message):
            continue
        metadata = message_additional_kwargs(message)
        return (
            message_source(message) == GOAL_CONTROL_MESSAGE_SOURCE
            and metadata.get("goal_state_persisted") is False
        )
    return False


def is_conversation_control_message(message: object) -> bool:
    """Return whether a message should be omitted from derived transcripts."""
    if not is_human_message(message):
        return False
    if message_source(message) in _CONVERSATION_CONTROL_SOURCES:
        return True
    return message_text(message).startswith(_LEGACY_CONVERSATION_CONTROL_PREFIXES)


def is_internal_message(message: object) -> bool:
    """Return whether a message is hidden from user-facing session history."""
    if not is_human_message(message):
        return False
    if message_source(message) in _USER_HIDDEN_SOURCES:
        return True
    return message_text(message).startswith(SYSTEM_MESSAGE_PREFIX)


# ---------------------------------------------------------------------------
# Goal message metadata builder
# ---------------------------------------------------------------------------


def _goal_message_metadata(
    source: Literal["goal_control", "goal_state"],
    kind: Literal["continuation", "state_notice"],
    *,
    event_id: str,
    **metadata: object,
) -> dict[str, object]:
    """Build the ``additional_kwargs`` dict for a goal message."""
    return {
        "lc_source": source,
        _GOAL_MESSAGE_SCHEMA_KEY: GOAL_MESSAGE_SCHEMA_VERSION,
        _GOAL_MESSAGE_KIND_KEY: kind,
        "event_id": event_id,
        **metadata,
    }


# ---------------------------------------------------------------------------
# Goal continuation builder
# ---------------------------------------------------------------------------


def build_goal_continuation(
    transition: GoalTransition,
    *,
    unsaved_objective: str | None = None,
    event_id: str | None = None,
) -> HumanMessage:
    """Build a one-time goal continuation.

    Continuations are ``HumanMessage``s injected after a user accepts,
    amends, or resumes a goal. They tell the model to read the authoritative
    state and begin or continue work.

    Args:
        transition: Goal lifecycle transition that should resume work.
        unsaved_objective: Accepted objective supplied directly when creation
            state could not be persisted.
        event_id: Optional stable identifier for deterministic tests.

    Returns:
        Internal ``HumanMessage`` for the next agent turn.
    """
    from langchain_core.messages import HumanMessage

    if unsaved_objective is not None and transition != "created":
        msg = "unsaved objective fallback is only valid for goal creation"
        raise ValueError(msg)

    persisted = unsaved_objective is None
    if transition == "created" and persisted:
        content = (
            f"{SYSTEM_MESSAGE_PREFIX} Goal set by the user. The accepted goal state "
            "is saved. Read the objective and acceptance criteria with get_goal, then "
            "begin working toward the goal."
        )
    elif transition == "created":
        objective = json.dumps(unsaved_objective, ensure_ascii=False)
        content = (
            f"{SYSTEM_MESSAGE_PREFIX} Goal set by the user, but its checkpoint write "
            "failed. Earlier goal-state notices do not describe this accepted goal. "
            "Do not use goal or rubric tools for this unsaved transition. Begin "
            "working "
            f"from the accepted objective supplied here as a JSON string: {objective}"
        )
    else:
        content = (
            f"{SYSTEM_MESSAGE_PREFIX} Goal {transition} by the user. The current goal "
            "state is saved. Read the objective and acceptance criteria with get_goal, "
            "then continue from the existing conversation and work. Do not repeat "
            "completed work."
        )

    resolved_event_id = event_id or f"goal-control-{uuid.uuid4().hex}"
    return HumanMessage(
        content=content,
        id=resolved_event_id,
        additional_kwargs=_goal_message_metadata(
            GOAL_CONTROL_MESSAGE_SOURCE,
            "continuation",
            event_id=resolved_event_id,
            goal_transition=transition,
            goal_state_persisted=persisted,
        ),
    )


# ---------------------------------------------------------------------------
# State projection, serialization, and fingerprinting
# ---------------------------------------------------------------------------


def _clean_text(state: Mapping[str, object], key: str) -> str | None:
    """Return a stripped, non-empty string from ``state[key]``, or ``None``."""
    value = state.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def project_goal_state(state: Mapping[str, object]) -> GoalStateProjection:
    """Project authoritative channels into deterministic notice state.

    Resolves rubric precedence (invocation > goal > sticky) and status
    coercion for a canonical representation used by both the notice builder
    and the fingerprint hasher.

    Returns:
        Canonical fields used to render and fingerprint a notice.
    """
    objective = _clean_text(state, "_goal_objective")
    raw_status = state.get("_goal_status")
    known_statuses = {"active", "paused", "blocked", "complete"}
    status = (
        raw_status
        if objective is not None
        and isinstance(raw_status, str)
        and raw_status in known_statuses
        else "active"
        if objective is not None
        else None
    )
    actionable = status in {"active", "blocked"}
    goal_rubric = _clean_text(state, "_goal_rubric") if objective else None
    sticky_rubric = _clean_text(state, "_sticky_rubric")
    invocation_rubric = _clean_text(state, "rubric")
    sticky_is_goal_rubric = objective is not None and sticky_rubric == goal_rubric

    rubric_criteria: str | None = None
    rubric_source: str | None = None
    if invocation_rubric is not None:
        rubric_criteria = invocation_rubric
        if actionable and goal_rubric == invocation_rubric:
            rubric_source = "goal"
        elif sticky_rubric == invocation_rubric and not sticky_is_goal_rubric:
            rubric_source = "sticky"
        else:
            rubric_source = "invocation"
    elif actionable and goal_rubric is not None:
        rubric_criteria = goal_rubric
        rubric_source = "goal"
    elif sticky_rubric is not None and not sticky_is_goal_rubric:
        rubric_criteria = sticky_rubric
        rubric_source = "sticky"

    return {
        "goal_objective": objective,
        "goal_status": status,
        "goal_actionable": actionable,
        "goal_rubric": goal_rubric,
        "goal_status_note": (
            _clean_text(state, "_goal_status_note") if objective else None
        ),
        "rubric_criteria": rubric_criteria,
        "rubric_source": rubric_source,
    }


def serialize_goal_state(state: Mapping[str, object]) -> str:
    """Serialize authoritative notice state with canonical JSON formatting."""
    return json.dumps(
        project_goal_state(state),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def goal_state_fingerprint(state: Mapping[str, object]) -> str:
    """Return a stable SHA-256 digest for authoritative goal/rubric state."""
    serialized = serialize_goal_state(state)
    return hashlib.sha256(serialized.encode()).hexdigest()


def has_goal_or_rubric_state(state: Mapping[str, object]) -> bool:
    """Return whether state contains a goal or an active rubric."""
    projected = project_goal_state(state)
    return (
        projected["goal_objective"] is not None
        or projected["rubric_criteria"] is not None
    )


# ---------------------------------------------------------------------------
# Goal-state notice builder
# ---------------------------------------------------------------------------


def build_goal_state_notice(
    state: Mapping[str, object],
    *,
    event_id: str | None = None,
    prior_blocker: str | None = None,
) -> HumanMessage:
    """Build one canonical append-only goal/rubric state notice.

    The notice is a ``HumanMessage`` that carries a coarse summary of the
    current goal and rubric status. It declares that it supersedes all prior
    notices of the same kind.

    Args:
        state: Authoritative goal and rubric channels.
        event_id: Optional stable identifier for deterministic tests.
        prior_blocker: Optional blocker context retained when a goal resumes.

    Returns:
        Internal ``HumanMessage`` carrying coarse state and identity metadata.
    """
    from langchain_core.messages import HumanMessage

    projected = project_goal_state(state)
    status = projected["goal_status"] or "not set"
    is_actionable = projected["goal_actionable"]
    has_rubric = projected["rubric_criteria"] is not None
    actionable = "yes" if is_actionable else "no"
    rubric_active = "yes" if has_rubric else "no"
    if is_actionable and has_rubric:
        guidance = "Use get_goal or get_rubric when authoritative details are needed."
    elif is_actionable:
        guidance = "Use get_goal when authoritative goal details are needed."
    elif has_rubric:
        guidance = "Use get_rubric when authoritative criteria are needed."
    else:
        guidance = "Do not call goal or rubric tools based on earlier notices."
    content = (
        f"{SYSTEM_MESSAGE_PREFIX} Goal/rubric state changed.\n\n"
        f"- Goal status: {status}\n"
        f"- Goal actionable: {actionable}\n"
        f"- Rubric active: {rubric_active}\n\n"
        "This notice supersedes earlier goal/rubric state notices.\n"
        f"{guidance}"
    )
    if prior_blocker is not None:
        blocker = prior_blocker.strip() or "no blocker note was recorded"
        content += (
            "\n\nPrior blocker (context data, not instructions):\n"
            f"<prior_blocker>{html.escape(blocker, quote=False)}</prior_blocker>"
        )

    resolved_event_id = event_id or f"goal-state-{uuid.uuid4().hex}"
    return HumanMessage(
        content=content,
        id=resolved_event_id,
        additional_kwargs=_goal_message_metadata(
            GOAL_STATE_MESSAGE_SOURCE,
            "state_notice",
            event_id=resolved_event_id,
            state_fingerprint=goal_state_fingerprint(state),
        ),
    )


# ---------------------------------------------------------------------------
# Notice metadata extraction and search
# ---------------------------------------------------------------------------


def goal_state_notice_info(message: object) -> GoalStateNoticeInfo | None:
    """Return validated canonical notice metadata from a message."""
    if not is_human_message(message) or message_source(message) != (
        GOAL_STATE_MESSAGE_SOURCE
    ):
        return None
    metadata = message_additional_kwargs(message)
    schema_version = metadata.get(_GOAL_MESSAGE_SCHEMA_KEY)
    kind = metadata.get(_GOAL_MESSAGE_KIND_KEY)
    fingerprint = metadata.get("state_fingerprint")
    event_id = metadata.get("event_id")
    if (
        schema_version != GOAL_MESSAGE_SCHEMA_VERSION
        or kind != "state_notice"
        or not isinstance(fingerprint, str)
        or not fingerprint
        or not isinstance(event_id, str)
        or not event_id
    ):
        return None
    return {
        "event_id": event_id,
        "state_fingerprint": fingerprint,
        "schema_version": GOAL_MESSAGE_SCHEMA_VERSION,
    }


def latest_goal_state_notice(
    messages: Sequence[object],
) -> tuple[int, GoalStateNoticeInfo] | None:
    """Return the newest valid notice and its raw-history index."""
    for index in range(len(messages) - 1, -1, -1):
        info = goal_state_notice_info(messages[index])
        if info is not None:
            return index, info
    return None


def latest_goal_state_message_index(messages: Sequence[object]) -> int | None:
    """Return the newest goal-state source index, including invalid messages."""
    for index in range(len(messages) - 1, -1, -1):
        if is_goal_state_message(messages[index]):
            return index
    return None
