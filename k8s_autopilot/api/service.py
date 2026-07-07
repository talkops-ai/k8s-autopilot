import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from k8s_autopilot.api.models import (
    ThreadCreate,
    ThreadHistoryResponse,
    ThreadResponse,
    ThreadSearch,
    ThreadStateResponse,
    ThreadUpdate,
)

logger = logging.getLogger("ThreadService")


class ThreadService:
    """Orchestrates thread operations natively using LangGraph Checkpointer."""

    def __init__(self, checkpointer: AsyncPostgresSaver) -> None:
        self._checkpointer = checkpointer

    def _extract_thread_response(
        self, thread_id: str, checkpoint_meta: dict[str, Any] | None,
    ) -> ThreadResponse:
        """Helper to build a ThreadResponse from checkpoint metadata."""
        meta = checkpoint_meta or {}
        # Safely extract stored metadata
        title = meta.get("title", "")
        status = meta.get("status", "idle")
        user_id = meta.get("user_id", "default")

        # We don't have created_at/updated_at fields on the CheckpointTuple by default
        # But we can approximate or use current time if missing. LangGraph timestamps
        # are stored natively in the checkpoint row if we query it, but we only have metadata here.
        # Let's add timestamp tracking to the metadata.
        created_at_str = meta.get("created_at")
        updated_at_str = meta.get("updated_at")

        now = datetime.now(UTC)
        created_at = datetime.fromisoformat(created_at_str) if created_at_str else now
        updated_at = datetime.fromisoformat(updated_at_str) if updated_at_str else now

        # Remove our internal fields from the raw metadata payload for the response
        clean_metadata = {
            k: v for k, v in meta.items()
            if k not in ["title", "status", "user_id", "created_at", "updated_at"]
        }

        return ThreadResponse(
            thread_id=uuid.UUID(thread_id),
            title=title,
            status=status,
            user_id=user_id,
            created_at=created_at,
            updated_at=updated_at,
            metadata=clean_metadata,
        )

    async def create_thread(
        self, req: ThreadCreate, user_id: str = "default",
    ) -> ThreadResponse:
        thread_id = req.thread_id or uuid.uuid4()
        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}

        now_iso = datetime.now(UTC).isoformat()

        # Build metadata payload
        metadata = {
            "source": "input",
            "step": -1,
            "run_id": str(uuid.uuid4()),
            **(req.metadata or {}),
            "title": req.title,
            "status": "idle",
            "user_id": user_id,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        # Write an empty checkpoint just to establish the thread in the checkpointer
        checkpoint = empty_checkpoint()
        await self._checkpointer.aput(config, checkpoint, metadata, new_versions={}) # type: ignore[arg-type]

        return self._extract_thread_response(str(thread_id), metadata)

    async def get_thread(self, thread_id: uuid.UUID) -> ThreadResponse | None:
        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
        checkpoint_tuple = await self._checkpointer.aget_tuple(config)  # type: ignore[arg-type]
        if not checkpoint_tuple:
            return None
        return self._extract_thread_response(str(thread_id), checkpoint_tuple.metadata) # type: ignore[arg-type]

    async def search_threads(self, req: ThreadSearch) -> list[ThreadResponse]:
        """Search threads by user_id using the checkpointer's native filtering."""
        # Note: LangGraph's alist filter only allows strict equality matching for now,
        # typically by parsing the JSON payload. We filter by user_id.
        filter_dict: dict[str, Any] = {"user_id": req.user_id}
        if req.agent_id:
            filter_dict["agent_id"] = req.agent_id
        if req.status:
            filter_dict["status"] = req.status

        results = []
        # checkpointer.alist returns an async generator
        async for checkpoint_tuple in self._checkpointer.alist(config=None, filter=filter_dict, limit=req.limit):  # type: ignore[call-arg]
            thread_id_str = checkpoint_tuple.config.get("configurable", {}).get("thread_id")
            if thread_id_str:
                results.append(self._extract_thread_response(thread_id_str, checkpoint_tuple.metadata)) # type: ignore[arg-type]

        # We handle sort_by manually since alist doesn't natively expose sorting parameters easily
        sort_field = req.sort_by if req.sort_by in ["created_at", "updated_at"] else "updated_at"
        reverse = req.sort_order.lower() == "desc"
        results.sort(key=lambda r: getattr(r, sort_field), reverse=reverse)

        return results

    async def update_thread(
        self, thread_id: uuid.UUID, req: ThreadUpdate,
    ) -> ThreadResponse | None:
        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
        checkpoint_tuple = await self._checkpointer.aget_tuple(config)  # type: ignore[arg-type]
        if not checkpoint_tuple:
            return None

        now_iso = datetime.now(UTC).isoformat()
        current_meta = dict(checkpoint_tuple.metadata or {}) # type: ignore[arg-type]

        if "step" not in current_meta:
            current_meta["step"] = -1
        if "run_id" not in current_meta:
            current_meta["run_id"] = str(uuid.uuid4())
        if "source" not in current_meta:
            current_meta["source"] = "update"

        if req.title is not None:
            current_meta["title"] = req.title
        if req.metadata is not None:
            current_meta.update(req.metadata)

        current_meta["updated_at"] = now_iso

        await self._checkpointer.aput(
            config, # type: ignore[arg-type]
            checkpoint_tuple.checkpoint,
            current_meta, # type: ignore[arg-type]
            new_versions={},
        )

        return self._extract_thread_response(str(thread_id), current_meta)

    async def delete_thread(self, thread_id: uuid.UUID) -> bool:
        # Some older checkpointers do not have adelete_thread/delete_thread.
        # But wait, looking at inspect output, AsyncPostgresSaver has adelete_thread and adelete_for_runs.
        # We will try to call adelete_thread if it exists.
        if hasattr(self._checkpointer, "adelete_thread"):
            await self._checkpointer.adelete_thread(str(thread_id))
            return True
        if hasattr(self._checkpointer, "adelete_for_runs"): # fallback maybe?
            return False
        return False

    async def get_thread_state(
        self, thread_id: uuid.UUID,
    ) -> ThreadStateResponse | None:
        """Get thread metadata + messages + structured state from the latest checkpoint.

        Extracts:
        - messages[] from channel_values (conversation ledger)
        - routing_decision, ui_payload, conversation_summary (Phase 1 state keys)
        - interrupts[] from pending_writes (Phase 3 HITL payloads for replay)
        """
        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
        checkpoint_tuple = await self._checkpointer.aget_tuple(config)  # type: ignore[arg-type]
        if not checkpoint_tuple:
            return None

        thread_resp = self._extract_thread_response(str(thread_id), checkpoint_tuple.metadata) # type: ignore[arg-type]

        messages: list[dict[str, Any]] = []
        routing_decision: dict[str, Any] | None = None
        ui_payload: dict[str, Any] | None = None
        conversation_summary: str | None = None

        if checkpoint_tuple.checkpoint:
            channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})

            # ── Messages (conversation ledger) ────────────────────
            state_msgs = channel_values.get("messages", [])
            for msg in state_msgs:
                if hasattr(msg, "dict"):
                    messages.append(msg.dict())
                else:
                    messages.append(msg)

            # ── Phase 1 state keys ────────────────────────────────
            raw_rd = channel_values.get("routing_decision")
            if isinstance(raw_rd, dict) and raw_rd:
                routing_decision = raw_rd

            raw_ui = channel_values.get("ui_payload")
            if isinstance(raw_ui, dict) and raw_ui:
                ui_payload = raw_ui

            raw_summary = channel_values.get("conversation_summary")
            if isinstance(raw_summary, str) and raw_summary:
                conversation_summary = raw_summary

        # ── Phase 3: Extract interrupt payloads for replay ────────
        # Instead of creating synthetic AI messages from interrupt data,
        # we now expose interrupts as structured data. The frontend can
        # render HITL cards, approval forms, etc. directly from this.
        interrupts: list[dict[str, Any]] = []
        pending_writes = getattr(checkpoint_tuple, "pending_writes", None)
        if pending_writes:
            for _task_id, channel, value in pending_writes:
                if channel != "__interrupt__" or not isinstance(value, (list, tuple)):
                    continue
                for interrupt_obj in value:
                    intr_val = getattr(interrupt_obj, "value", interrupt_obj)
                    if isinstance(intr_val, dict):
                        interrupts.append(intr_val)

        return ThreadStateResponse(
            thread_id=thread_resp.thread_id,
            title=thread_resp.title,
            status=thread_resp.status,
            messages=messages,
            created_at=thread_resp.created_at,
            updated_at=thread_resp.updated_at,
            routing_decision=routing_decision,
            ui_payload=ui_payload,
            conversation_summary=conversation_summary,
            interrupts=interrupts,
        )

    async def get_thread_history(
        self, thread_id: uuid.UUID, limit: int = 10,
    ) -> ThreadHistoryResponse | None:
        """Get checkpoint history for time-travel/debugging."""
        config = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}

        states: list[dict[str, Any]] = []
        async for checkpoint_tuple in self._checkpointer.alist(config=config, limit=limit):  # type: ignore[call-arg]
            if checkpoint_tuple and checkpoint_tuple.checkpoint:
                states.append(
                    {
                        "checkpoint_id": checkpoint_tuple.config.get("configurable", {}).get("checkpoint_id"),
                        "checkpoint": checkpoint_tuple.checkpoint,
                        "metadata": checkpoint_tuple.metadata,
                    },
                )

        if not states:
            return None

        return ThreadHistoryResponse(
            thread_id=thread_id,
            states=states,
        )

    async def auto_touch(
        self, thread_id: str, user_id: str = "default",
        agent_id: str = "", user_query: str = "",
    ) -> None:
        """Upsert the conversation record (used by a2a_executor)."""
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return

        config = {"configurable": {"thread_id": str(tid), "checkpoint_ns": ""}}
        checkpoint_tuple = await self._checkpointer.aget_tuple(config)  # type: ignore[arg-type]

        now_iso = datetime.now(UTC).isoformat()

        if checkpoint_tuple:
            current_meta = dict(checkpoint_tuple.metadata or {}) # type: ignore[arg-type]
            
            if "step" not in current_meta:
                current_meta["step"] = -1
            if "run_id" not in current_meta:
                current_meta["run_id"] = str(uuid.uuid4())
            if "source" not in current_meta:
                current_meta["source"] = "update"
                
            current_meta["updated_at"] = now_iso
            # If user_id is missing, inject it
            if "user_id" not in current_meta:
                current_meta["user_id"] = user_id
            if agent_id and ("agent_id" not in current_meta or current_meta["agent_id"] != agent_id):
                current_meta["agent_id"] = agent_id

            # Auto-generate title from user_query or checkpoint if still empty
            if not current_meta.get("title"):
                title = ""
                if user_query and user_query.strip():
                    title = user_query.strip()
                    if len(title) > 60:
                        title = title[:57].rsplit(" ", 1)[0] + "..."
                if not title:
                    title = self._extract_title_from_checkpoint(checkpoint_tuple.checkpoint)
                if title:
                    current_meta["title"] = title

            await self._checkpointer.aput(
                config, # type: ignore[arg-type]
                checkpoint_tuple.checkpoint,
                current_meta, # type: ignore[arg-type]
                new_versions={},
            )
        else:
            # Generate title from user_query for new threads
            title = ""
            if user_query and user_query.strip():
                title = user_query.strip()
                if len(title) > 60:
                    title = title[:57].rsplit(" ", 1)[0] + "..."

            # Create thread metadata
            metadata = {
                "source": "input",
                "step": -1,
                "run_id": str(uuid.uuid4()),
                "title": title,
                "status": "idle",
                "user_id": user_id,
                "agent_id": agent_id,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            checkpoint = empty_checkpoint()
            await self._checkpointer.aput(config, checkpoint, metadata, new_versions={}) # type: ignore[arg-type]

    @staticmethod
    def _extract_title_from_checkpoint(checkpoint: Checkpoint | None) -> str:
        """Extract a short title from the first human message in a checkpoint."""
        if not checkpoint:
            return ""
        channel_values = checkpoint.get("channel_values", {})
        messages = channel_values.get("messages", [])
        for msg in messages:
            # Support both dict and LangChain message objects
            if hasattr(msg, "type"):
                msg_type = msg.type
                msg_content = msg.content if hasattr(msg, "content") else ""
            elif isinstance(msg, dict):
                msg_type = msg.get("type", "")
                msg_content = msg.get("content", "")
            else:
                continue

            if msg_type == "human" and isinstance(msg_content, str) and msg_content.strip():
                title = msg_content.strip()
                # Truncate to ~60 chars at a word boundary
                if len(title) > 60:
                    title = title[:57].rsplit(" ", 1)[0] + "..."
                return title
        return ""


# Singleton service reference for the auto-touch hook
_thread_service: ThreadService | None = None

def get_thread_service() -> ThreadService | None:
    return _thread_service

def set_thread_service(service: ThreadService) -> None:
    global _thread_service
    _thread_service = service
