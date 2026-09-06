"""Client↔server interrupt transport for Hooks v2 server-owned events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, TypeAdapter

from k8s_autopilot.hooks.models.adapters import HOOK_INVOCATION_RESPONSE_ADAPTER
from k8s_autopilot.hooks.models.transport import HookInvocationRequest, HookInvocationResponse

if TYPE_CHECKING:
    from uuid import UUID

HOOK_INVOCATION_INTERRUPT_TYPE: Literal["hook_invocation"] = "hook_invocation"


class HookInvocationInterrupt(BaseModel):
    """LangGraph interrupt envelope for a server-owned hook invocation."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["hook_invocation"] = HOOK_INVOCATION_INTERRUPT_TYPE
    request: HookInvocationRequest


HOOK_INVOCATION_INTERRUPT_ADAPTER: TypeAdapter[HookInvocationInterrupt] = TypeAdapter(HookInvocationInterrupt)

HookResumeValue: TypeAlias = dict[str, Any]


def build_hook_interrupt_payload(request: HookInvocationRequest) -> dict[str, Any]:
    """Serialize a hook invocation request for `interrupt()`."""
    return HOOK_INVOCATION_INTERRUPT_ADAPTER.dump_python(
        HookInvocationInterrupt(request=request),
        mode="json",
    )


def parse_hook_interrupt_payload(value: object) -> HookInvocationRequest | None:
    """Parse a hook invocation interrupt payload when present."""
    if (
        not isinstance(value, dict)
        or value.get("type") != HOOK_INVOCATION_INTERRUPT_TYPE
    ):
        return None
    interrupt_data = HOOK_INVOCATION_INTERRUPT_ADAPTER.validate_python(value)
    return interrupt_data.request


def build_hook_resume_value(response: HookInvocationResponse) -> HookResumeValue:
    """Serialize a hook invocation response for `Command(resume=...)`."""
    return HOOK_INVOCATION_RESPONSE_ADAPTER.dump_python(response, mode="json")


def parse_hook_resume_value(
    value: object,
    *,
    invocation_id: UUID,
    snapshot_id: str,
) -> HookInvocationResponse:
    """Validate a resumed hook response against the outstanding request."""
    if not isinstance(value, dict):
        msg = "Hook resume payload must be a JSON object"
        raise ValueError(msg)
    response = HOOK_INVOCATION_RESPONSE_ADAPTER.validate_python(value)
    if response.invocation_id != invocation_id:
        msg = f"Resume invocation id {response.invocation_id} does not match {invocation_id}"
        raise ValueError(msg)
    if response.snapshot_id != snapshot_id:
        msg = f"Resume snapshot id {response.snapshot_id} does not match {snapshot_id}"
        raise ValueError(msg)
    return response
