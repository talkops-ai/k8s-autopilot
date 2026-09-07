"""Typed interrupt and resume payload schemas.

Provides deterministic, typed Pydantic models for interrupt resumption matching
OpsCode's interrupt contracts. Eliminates multi-layer duck-typing heuristics
across executor.py, goal_tools.py, and UI adapters.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

_CONFIRM_DECISIONS: frozenset[str] = frozenset({
    "accept",
    "confirm",
    "accepted",
    "confirmed",
    "approve",
    "approved",
    "approval",
    "auto_approve_all",
    "auto",
    "enable_auto",
    "proceed",
    "continue",
    "y",
    "yes",
    "ok",
    "true",
    "1",
    "goal_response",
})
_EDIT_DECISIONS: frozenset[str] = frozenset({
    "edit",
    "edited",
    "e",
    "modify",
    "modified",
    "update",
    "updated",
})
_REJECT_DECISIONS: frozenset[str] = frozenset({
    "reject",
    "rejected",
    "r",
    "deny",
    "denied",
    "disapprove",
    "disapproved",
})
_CANCEL_DECISIONS: frozenset[str] = frozenset({
    "cancel",
    "cancelled",
    "dismiss",
    "dismissed",
    "close",
    "closed",
    "n",
    "no",
})


class HitlDecision(BaseModel):
    """Individual tool approval decision."""

    type: Literal["approve", "reject"] = "approve"
    message: str | None = None


class HitlResumePayload(BaseModel):
    """Resume payload for HITL tool approval interrupts."""

    decisions: list[HitlDecision] = Field(default_factory=list)
    auto_approve_requested: bool = False

    @classmethod
    def from_raw(  # noqa: PLR0911, PLR0912, PLR0915
        cls, data: Any, count: int = 1,
    ) -> HitlResumePayload:
        """Parse arbitrary client data (dict, list, string) into HitlResumePayload."""
        if isinstance(data, cls):
            return data

        parsed = data
        if isinstance(data, str) and data.strip():
            trimmed = data.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or (
                trimmed.startswith("[") and trimmed.endswith("]")
            ):
                try:
                    parsed = json.loads(trimmed)
                except (json.JSONDecodeError, ValueError):
                    parsed = data

        auto_mode = False
        target_count = max(1, count)

        if isinstance(parsed, dict):
            # Check for decisions array from HITL response
            decisions_raw = parsed.get("decisions")
            if isinstance(decisions_raw, list) and decisions_raw:
                decisions: list[HitlDecision] = []
                for item in decisions_raw:
                    if isinstance(item, dict):
                        raw_t = str(
                            item.get("type") or item.get("decision") or "approve",
                        ).lower().strip()
                        if raw_t in (
                            "auto_approve_all",
                            "enable_auto",
                            "auto",
                            "a",
                        ):
                            auto_mode = True
                            dec_type: Literal["approve", "reject"] = "approve"
                        elif raw_t in (
                            "approve",
                            "yes",
                            "confirm",
                            "y",
                            "proceed",
                            "continue",
                            "ok",
                            "true",
                            "1",
                        ):
                            dec_type = "approve"
                        else:
                            dec_type = "reject"
                        msg = item.get("message") or item.get("feedback")
                        decisions.append(
                            HitlDecision(
                                type=dec_type,
                                message=str(msg) if msg else None,
                            ),
                        )
                    else:
                        s = str(item).lower().strip()
                        if s in (
                            "auto_approve_all",
                            "enable_auto",
                            "auto",
                            "a",
                        ):
                            auto_mode = True
                            dec_type = "approve"
                        elif s in (
                            "approve",
                            "yes",
                            "confirm",
                            "y",
                            "proceed",
                            "continue",
                            "ok",
                            "true",
                            "1",
                        ):
                            dec_type = "approve"
                        else:
                            dec_type = "reject"
                        decisions.append(HitlDecision(type=dec_type))
                if decisions:
                    return cls(
                        decisions=decisions,
                        auto_approve_requested=auto_mode,
                    )

            # Check single decision / action
            raw_dec = (
                parsed.get("decision")
                or parsed.get("action")
                or parsed.get("choice")
                or parsed.get("type")
                or parsed.get("status")
            )
            dec_str = str(raw_dec).lower().strip() if raw_dec is not None else ""
            msg = (
                parsed.get("message")
                or parsed.get("feedback")
                or parsed.get("rejectionReason")
            )
            msg_str = str(msg).strip() if msg else None

            if dec_str in ("auto_approve_all", "enable_auto", "auto", "a"):
                return cls(
                    decisions=[
                        HitlDecision(type="approve") for _ in range(target_count)
                    ],
                    auto_approve_requested=True,
                )
            if dec_str in (
                "approve",
                "yes",
                "confirm",
                "y",
                "proceed",
                "continue",
                "ok",
                "true",
                "1",
                "hitl_response",
            ):
                return cls(
                    decisions=[
                        HitlDecision(type="approve") for _ in range(target_count)
                    ],
                    auto_approve_requested=False,
                )
            if dec_str in ("reject", "deny", "cancel", "no", "n"):
                return cls(
                    decisions=[
                        HitlDecision(type="reject", message=msg_str)
                        for _ in range(target_count)
                    ],
                    auto_approve_requested=False,
                )
            return cls(
                decisions=[
                    HitlDecision(type="approve") for _ in range(target_count)
                ],
                auto_approve_requested=False,
            )

        if isinstance(parsed, list):
            decisions = []
            for item in parsed:
                s = str(item).lower().strip()
                if s in ("auto_approve_all", "enable_auto", "auto", "a"):
                    auto_mode = True
                    dec_type = "approve"
                elif s in ("approve", "yes", "confirm", "y", "proceed", "continue"):
                    dec_type = "approve"
                else:
                    dec_type = "reject"
                decisions.append(HitlDecision(type=dec_type))
            return cls(
                decisions=decisions or [HitlDecision(type="approve")],
                auto_approve_requested=auto_mode,
            )

        if isinstance(parsed, str):
            s = parsed.lower().strip()
            if s in ("auto_approve_all", "enable_auto", "auto", "a"):
                return cls(
                    decisions=[
                        HitlDecision(type="approve") for _ in range(target_count)
                    ],
                    auto_approve_requested=True,
                )
            if s in (
                "approve",
                "yes",
                "confirm",
                "y",
                "proceed",
                "continue",
                "ok",
                "true",
                "1",
            ):
                return cls(
                    decisions=[
                        HitlDecision(type="approve") for _ in range(target_count)
                    ],
                    auto_approve_requested=False,
                )
            if s in ("reject", "deny", "cancel", "no", "n"):
                return cls(
                    decisions=[
                        HitlDecision(type="reject") for _ in range(target_count)
                    ],
                    auto_approve_requested=False,
                )
            return cls(
                decisions=[
                    HitlDecision(type="approve") for _ in range(target_count)
                ],
                auto_approve_requested=False,
            )

        return cls(
            decisions=[
                HitlDecision(type="approve") for _ in range(target_count)
            ],
            auto_approve_requested=False,
        )


class AskUserResumePayload(BaseModel):
    """Resume payload for ask_user question prompts."""

    status: Literal["answered", "cancelled", "error"] = "answered"
    answers: list[str] = Field(default_factory=list)

    @classmethod
    def from_raw(  # noqa: PLR0911, PLR0912
        cls,
        data: Any,
        questions: list[Any] | None = None,
    ) -> AskUserResumePayload:
        """Parse arbitrary client data into AskUserResumePayload matching OpsCode."""
        if isinstance(data, cls):
            return data

        q_count = len(questions) if questions else 1
        parsed = data
        if isinstance(data, str) and data.strip():
            trimmed = data.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or (
                trimmed.startswith("[") and trimmed.endswith("]")
            ):
                try:
                    parsed = json.loads(trimmed)
                except (json.JSONDecodeError, ValueError):
                    parsed = data

        if isinstance(parsed, dict):
            status = str(parsed.get("status", "answered")).lower().strip()
            if status == "cancelled":
                return cls(
                    status="cancelled",
                    answers=["(cancelled)" for _ in range(q_count)],
                )

            if "answers" in parsed:
                raw_ans = parsed["answers"]
                ans_list = (
                    [str(x) for x in raw_ans]
                    if isinstance(raw_ans, list)
                    else [str(raw_ans)]
                )
                return cls(status="answered", answers=ans_list)

            if "choice" in parsed:
                return cls(status="answered", answers=[str(parsed["choice"])])

            if "text" in parsed:
                return cls(status="answered", answers=[str(parsed["text"])])

            if "answer" in parsed:
                return cls(status="answered", answers=[str(parsed["answer"])])

            return cls(status="answered", answers=[json.dumps(parsed)])

        if isinstance(parsed, list):
            return cls(status="answered", answers=[str(x) for x in parsed])

        if isinstance(parsed, str):
            s = parsed.strip()
            if s.lower() in ("cancel", "cancelled"):
                return cls(
                    status="cancelled",
                    answers=["(cancelled)" for _ in range(q_count)],
                )
            return cls(status="answered", answers=[s])

        return cls(
            status="answered",
            answers=[str(parsed) if parsed is not None else ""],
        )


class GoalReviewResumePayload(BaseModel):
    """Resume payload for goal_review / propose_goal interrupts."""

    decision: Literal["confirm", "edit", "reject", "cancel"] = "confirm"
    criteria: list[str] | None = None
    feedback: str | None = None

    @classmethod
    def from_raw(  # noqa: PLR0911, PLR0912, PLR0915
        cls, data: Any,
    ) -> GoalReviewResumePayload:
        """Parse arbitrary client data into GoalReviewResumePayload."""
        if isinstance(data, cls):
            return data

        parsed = data
        if isinstance(data, str) and data.strip():
            trimmed = data.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or (
                trimmed.startswith("[") and trimmed.endswith("]")
            ):
                try:
                    parsed = json.loads(trimmed)
                except (json.JSONDecodeError, ValueError):
                    parsed = data

        if isinstance(parsed, dict):
            # Unwrap nested dict if wrapped by interrupt ID
            if len(parsed) == 1:
                key = next(iter(parsed))
                if key not in (
                    "decision",
                    "decisions",
                    "action",
                    "status",
                    "feedback",
                    "criteria",
                    "goalText",
                    "objective",
                    "choice",
                    "message",
                    "type",
                ):
                    inner = parsed[key]
                    if isinstance(inner, dict):
                        parsed = inner
                    elif isinstance(inner, str):
                        try:
                            parsed = json.loads(inner)
                        except (json.JSONDecodeError, ValueError):
                            parsed = {"decision": inner}

            # Check decisions list from HITL
            decisions_val = parsed.get("decisions")
            answers_val = parsed.get("answers")
            if (
                isinstance(decisions_val, list)
                and decisions_val
            ):
                first = decisions_val[0]
                if isinstance(first, dict):
                    raw_dec = (
                        first.get("type")
                        or first.get("decision")
                        or "confirm"
                    )
                    feedback = first.get("message") or first.get("feedback")
                else:
                    raw_dec = str(first)
                    feedback = None
            elif (
                isinstance(answers_val, list)
                and answers_val
            ):
                raw_dec = answers_val[0]
                feedback = None
            else:
                raw_dec = (
                    parsed.get("decision")
                    or parsed.get("action")
                    or parsed.get("choice")
                    or parsed.get("type")
                    or parsed.get("status")
                    or "confirm"
                )
                feedback = (
                    parsed.get("feedback")
                    or parsed.get("rejectionReason")
                    or parsed.get("message")
                )

            raw_str = str(raw_dec).lower().strip()
            fb_str = str(feedback).strip() if feedback else None

            # Extract criteria if present
            criteria_list: list[str] | None = None
            crit_val = parsed.get("criteria")
            if isinstance(crit_val, list):
                criteria_list = [
                    str(x).strip() for x in crit_val if str(x).strip()
                ]
            elif isinstance(crit_val, str) and crit_val.strip():
                criteria_list = [
                    line.strip().lstrip("-*•0123456789.) ").strip()
                    for line in crit_val.splitlines()
                    if line.strip().lstrip("-*•0123456789.) ").strip()
                ]

            if raw_str in _EDIT_DECISIONS:
                return cls(
                    decision="edit", criteria=criteria_list, feedback=fb_str,
                )
            if raw_str in _REJECT_DECISIONS or (
                fb_str and raw_str not in _CONFIRM_DECISIONS
            ):
                return cls(
                    decision="reject", criteria=criteria_list, feedback=fb_str,
                )
            if raw_str in _CANCEL_DECISIONS:
                return cls(
                    decision="cancel", criteria=criteria_list, feedback=fb_str,
                )
            return cls(
                decision="confirm", criteria=criteria_list, feedback=fb_str,
            )

        if isinstance(parsed, str):
            s = parsed.lower().strip()
            if s in _EDIT_DECISIONS:
                return cls(decision="edit")
            if s in _REJECT_DECISIONS:
                return cls(decision="reject")
            if s in _CANCEL_DECISIONS:
                return cls(decision="cancel")
            return cls(decision="confirm")

        return cls(decision="confirm")


__all__ = [
    "AskUserResumePayload",
    "GoalReviewResumePayload",
    "HitlDecision",
    "HitlResumePayload",
]
