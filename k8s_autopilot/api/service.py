import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import Checkpoint, empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from k8s_autopilot.api.models import (
    GoalTelemetry,
    ModelTelemetry,
    SubagentTelemetry,
    ThreadCreate,
    ThreadHistoryResponse,
    ThreadResponse,
    ThreadSearch,
    ThreadStateResponse,
    ThreadTelemetryResponse,
    ThreadUpdate,
    UsageTelemetry,
)

from k8s_autopilot.utils.logger import get_logger

logger = get_logger(__name__)


class ThreadService:
    """Orchestrates thread operations natively using LangGraph Checkpointer."""

    def __init__(self, checkpointer: Any, executor: Any = None) -> None:
        self._checkpointer = checkpointer
        self._executor = executor
        if isinstance(checkpointer, AsyncPostgresSaver):
            self._backend = "postgres"
        elif isinstance(checkpointer, AsyncSqliteSaver):
            self._backend = "sqlite"
        else:
            self._backend = "auto"

    @classmethod
    def _format_message(cls, msg: Any) -> dict[str, Any]:
        """Format LangChain or dict message to frontend standard dict."""
        if hasattr(msg, "type"):
            msg_dict: dict[str, Any] = {
                "type": getattr(msg, "type", "ai"),
                "content": getattr(msg, "content", ""),
                "id": getattr(msg, "id", None),
                "name": getattr(msg, "name", None),
                "tool_call_id": getattr(msg, "tool_call_id", None),
                "additional_kwargs": getattr(msg, "additional_kwargs", {}) or {},
                "response_metadata": getattr(msg, "response_metadata", {}) or {},
            }
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                msg_dict["tool_calls"] = [
                    tc.dict() if hasattr(tc, "dict") else dict(tc) for tc in msg.tool_calls
                ]
            return msg_dict
        elif isinstance(msg, dict):
            return dict(msg)
        return {"type": "ai", "content": str(msg)}

    def _extract_thread_response(
        self, thread_id: str, checkpoint_meta: dict[str, Any] | None,
    ) -> ThreadResponse:
        """Helper to build a ThreadResponse from checkpoint metadata."""
        meta = checkpoint_meta or {}
        # Safely extract stored metadata
        title = meta.get("title", "")
        status = meta.get("status", "idle")
        user_id = meta.get("user_id", "default")

        created_at_str = meta.get("created_at")
        updated_at_str = meta.get("updated_at")

        now = datetime.now(UTC)
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00")) if created_at_str else now
        except Exception:
            created_at = now
        try:
            updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00")) if updated_at_str else now
        except Exception:
            updated_at = now

        # Remove internal fields from raw metadata payload for the response
        clean_metadata = {
            k: v for k, v in meta.items()
            if k not in ["title", "status", "user_id", "created_at", "updated_at"]
        }

        effective_title = (
            title
            or meta.get("initial_prompt")
            or meta.get("user_query")
            or f"Conversation {thread_id[:8]}"
        )
        if not effective_title.startswith("Conversation "):
            clean_metadata.setdefault("initial_prompt", effective_title)
            clean_metadata.setdefault("user_query", effective_title)

        tid_out: uuid.UUID | str = thread_id
        try:
            tid_out = uuid.UUID(thread_id)
        except (ValueError, TypeError, AttributeError):
            tid_out = thread_id

        return ThreadResponse(
            thread_id=tid_out,
            title=effective_title,
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
        str_tid = str(thread_id)
        config = {"configurable": {"thread_id": str_tid, "checkpoint_ns": ""}}

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

        return self._extract_thread_response(str_tid, metadata)

    async def get_thread(self, thread_id: uuid.UUID | str) -> ThreadResponse | None:
        str_tid = str(thread_id)
        config = {"configurable": {"thread_id": str_tid, "checkpoint_ns": ""}}
        checkpoint_tuple = await self._checkpointer.aget_tuple(config)  # type: ignore[arg-type]
        if not checkpoint_tuple:
            return None
        cp_meta = dict(checkpoint_tuple.metadata or {})
        if not cp_meta.get("title") and getattr(checkpoint_tuple, "checkpoint", None):
            from k8s_autopilot.state.session import _initial_prompt_from_messages

            cp_dict = checkpoint_tuple.checkpoint if isinstance(checkpoint_tuple.checkpoint, dict) else {}
            msgs = cp_dict.get("channel_values", {}).get("messages", [])
            prompt = _initial_prompt_from_messages(msgs)
            if prompt:
                cp_meta["title"] = prompt
                cp_meta["initial_prompt"] = prompt
                cp_meta["user_query"] = prompt
        return self._extract_thread_response(str_tid, cp_meta)

    async def search_threads(self, req: ThreadSearch) -> list[ThreadResponse]:
        """Search threads across persistent database / checkpointer."""
        from k8s_autopilot.state.session import list_threads

        results: list[ThreadResponse] = []
        seen_ids: set[str] = set()

        # 1. Primary: Use high-performance list_threads database query
        try:
            db_threads = await list_threads(
                limit=req.limit,
                offset=req.offset,
                include_checkpoint_fields=True,
                include_message_count=True,
                sort_by="created" if req.sort_by == "created_at" else "updated",
                backend=self._backend,
            )
            for t in db_threads:
                tid_str = t["thread_id"]
                if tid_str in seen_ids or ":" in tid_str:
                    continue
                seen_ids.add(tid_str)
                now = datetime.now(UTC)
                updated_at_str = t.get("updated_at")
                created_at_str = t.get("created_at")
                updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00")) if updated_at_str else now
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00")) if created_at_str else now
                title = t.get("initial_prompt") or f"Conversation {tid_str[:8]}"

                meta = {
                    "agent_name": t.get("agent_name"),
                    "message_count": t.get("message_count", 0),
                    "git_branch": t.get("git_branch"),
                    "cwd": t.get("cwd"),
                    "initial_prompt": t.get("initial_prompt") or (title if not title.startswith("Conversation ") else None),
                    "user_query": t.get("initial_prompt") or (title if not title.startswith("Conversation ") else None),
                }

                tid_out: uuid.UUID | str = tid_str
                try:
                    tid_out = uuid.UUID(tid_str)
                except Exception:
                    tid_out = tid_str

                results.append(
                    ThreadResponse(
                        thread_id=tid_out,
                        title=title,
                        status="idle",
                        user_id=req.user_id,
                        created_at=created_at,
                        updated_at=updated_at,
                        metadata=meta,
                    )
                )
        except Exception as exc:
            logger.debug(f"list_threads direct query fallback to alist: {exc}")

        # 2. If list_threads is empty or checkpointer has in-memory / custom saver
        if not results and hasattr(self._checkpointer, "alist"):
            try:
                async for checkpoint_tuple in self._checkpointer.alist(config=None, limit=req.limit * 3):
                    cfg = checkpoint_tuple.config.get("configurable", {})
                    tid_str = cfg.get("thread_id")
                    cp_ns = cfg.get("checkpoint_ns", "")
                    if not tid_str or tid_str in seen_ids:
                        continue
                    # Skip internal subagent threads and nested subgraphs
                    if ":" in tid_str or cp_ns:
                        continue
                    seen_ids.add(tid_str)

                    cp_meta = dict(checkpoint_tuple.metadata or {})
                    if not cp_meta.get("title") and getattr(checkpoint_tuple, "checkpoint", None):
                        from k8s_autopilot.state.session import _initial_prompt_from_messages

                        cp_dict = checkpoint_tuple.checkpoint if isinstance(checkpoint_tuple.checkpoint, dict) else {}
                        msgs = cp_dict.get("channel_values", {}).get("messages", [])
                        prompt = _initial_prompt_from_messages(msgs)
                        if prompt:
                            cp_meta["title"] = prompt
                            cp_meta["initial_prompt"] = prompt
                            cp_meta["user_query"] = prompt

                    results.append(self._extract_thread_response(tid_str, cp_meta))
            except Exception as e:
                logger.debug(f"alist fallback failed: {e}")

        sort_field = req.sort_by if req.sort_by in ["created_at", "updated_at"] else "updated_at"
        reverse = req.sort_order.lower() == "desc"
        results.sort(key=lambda r: getattr(r, sort_field), reverse=reverse)

        return results[:req.limit]

    async def update_thread(
        self, thread_id: uuid.UUID | str, req: ThreadUpdate,
    ) -> ThreadResponse | None:
        str_tid = str(thread_id)
        config = {"configurable": {"thread_id": str_tid, "checkpoint_ns": ""}}
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

        current_cid = checkpoint_tuple.config.get("configurable", {}).get("checkpoint_id")
        put_config = {
            "configurable": {
                "thread_id": str_tid,
                "checkpoint_ns": "",
                **({"checkpoint_id": current_cid} if current_cid else {}),
            }
        }

        await self._checkpointer.aput(
            put_config, # type: ignore[arg-type]
            checkpoint_tuple.checkpoint,
            current_meta, # type: ignore[arg-type]
            new_versions={},
        )

        return self._extract_thread_response(str_tid, current_meta)

    async def delete_thread(self, thread_id: uuid.UUID | str) -> bool:
        from k8s_autopilot.state.session import delete_thread as session_delete_thread

        str_tid = str(thread_id)
        deleted = False
        try:
            deleted = await session_delete_thread(str_tid, backend=self._backend)
        except Exception:
            pass

        if hasattr(self._checkpointer, "adelete_thread"):
            try:
                await self._checkpointer.adelete_thread(str_tid)
                deleted = True
            except Exception:
                pass
        return deleted

    async def set_thread_approval_mode(
        self, thread_id: uuid.UUID | str, mode: Any, auto_approve: bool | None = None
    ) -> bool:
        """Store approval mode for a thread in the attached store."""
        from k8s_autopilot.security.approval_mode import (
            APPROVAL_MODE_NAMESPACE,
            approval_mode_key,
            approval_mode_payload,
            coerce_approval_mode,
        )

        resolved_mode = coerce_approval_mode(mode)
        if not resolved_mode:
            return False

        key = approval_mode_key(str(thread_id))
        payload = approval_mode_payload(mode=resolved_mode, auto_approve=auto_approve)

        store_obj = getattr(self, "_checkpointer", None)
        if store_obj and hasattr(store_obj, "store"):
            store_obj = store_obj.store
        if store_obj and hasattr(store_obj, "aput"):
            await store_obj.aput(APPROVAL_MODE_NAMESPACE, key, payload)
            return True
        return False

    async def get_thread_state(
        self, thread_id: uuid.UUID | str,
    ) -> ThreadStateResponse | None:
        """Get thread metadata + messages + structured state from the latest checkpoint.

        Extracts:
        - messages[] from LangGraph Pregel aget_state or writes/checkpoints
        - routing_decision, ui_payload, conversation_summary (Phase 1 state keys)
        - interrupts[] from pending_writes / tasks (Phase 3 HITL payloads for replay)
        """
        str_tid = str(thread_id)
        config = {"configurable": {"thread_id": str_tid, "checkpoint_ns": ""}}

        messages: list[dict[str, Any]] = []
        routing_decision: dict[str, Any] | None = None
        ui_payload: dict[str, Any] | None = None
        conversation_summary: str | None = None
        interrupts: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}

        # 1. Primary: Reconstruct full multi-turn conversation messages via SessionManager
        try:
            from k8s_autopilot.middleware.goal_state_notice import is_conversation_control_message
            from k8s_autopilot.state.session import SessionManager

            raw_msgs = await SessionManager().get_thread_messages(str_tid)
            for msg in raw_msgs:
                if not is_conversation_control_message(msg):
                    messages.append(self._format_message(msg))
        except Exception as err:
            logger.debug("SessionManager get_thread_messages failed: %s", err)

        # 2. Query Pregel aget_state for structured state keys (routing_decision, ui_payload, interrupts)
        # and as fallback for messages if SessionManager didn't return any
        if self._executor and hasattr(self._executor, "_ensure_agent"):
            try:
                agent = self._executor._ensure_agent()
                if agent and hasattr(agent, "aget_state"):
                    state_snapshot = await agent.aget_state(config)
                    if state_snapshot and state_snapshot.values:
                        values = state_snapshot.values
                        if not messages:
                            from k8s_autopilot.middleware.goal_state_notice import (
                                is_conversation_control_message,
                            )

                            for msg in values.get("messages", []):
                                if not is_conversation_control_message(msg):
                                    messages.append(self._format_message(msg))

                        raw_rd = values.get("routing_decision")
                        if isinstance(raw_rd, dict) and raw_rd:
                            routing_decision = raw_rd

                        raw_ui = values.get("ui_payload")
                        if isinstance(raw_ui, dict) and raw_ui:
                            ui_payload = raw_ui

                        raw_summary = values.get("conversation_summary")
                        if isinstance(raw_summary, str) and raw_summary:
                            conversation_summary = raw_summary

                    if state_snapshot and hasattr(state_snapshot, "tasks"):
                        for t in getattr(state_snapshot, "tasks", ()):
                            for intr in getattr(t, "interrupts", ()):
                                intr_val = getattr(intr, "value", intr)
                                if isinstance(intr_val, dict):
                                    interrupts.append(intr_val)

                    if state_snapshot and hasattr(state_snapshot, "metadata") and state_snapshot.metadata:
                        metadata = dict(state_snapshot.metadata)
            except Exception as exc:
                logger.debug("aget_state via agent failed: %s", exc)

        # 3. Checkpointer fallback: Fetch checkpoint tuple
        checkpoint_tuple = None
        try:
            checkpoint_tuple = await self._checkpointer.aget_tuple(config)
            if checkpoint_tuple and checkpoint_tuple.metadata:
                metadata = {**metadata, **dict(checkpoint_tuple.metadata)}
        except Exception as exc:
            logger.debug("aget_tuple failed: %s", exc)

        if checkpoint_tuple is None and ":" in str_tid:
            try:
                sub_config = {"configurable": {"thread_id": str_tid}}
                checkpoint_tuple = await self._checkpointer.aget_tuple(sub_config)
                if checkpoint_tuple and checkpoint_tuple.metadata:
                    metadata = {**metadata, **dict(checkpoint_tuple.metadata)}
            except Exception as exc:
                logger.debug("aget_tuple subagent fallback failed: %s", exc)

        if not messages and checkpoint_tuple and getattr(checkpoint_tuple, "checkpoint", None):
            cp_dict = checkpoint_tuple.checkpoint if isinstance(checkpoint_tuple.checkpoint, dict) else {}
            for msg in cp_dict.get("channel_values", {}).get("messages", []):
                messages.append(self._format_message(msg))

        if not messages and not checkpoint_tuple and not metadata:
            return None

        step_count = len(messages) if messages else (metadata.get("step", 0) if metadata else 0)
        logger.debug(
            "Checkpointer state retrieved for thread '%s'",
            str_tid,
            context_id=str_tid,
            extra={"context_id": str_tid, "step_count": step_count},
        )

        if not metadata.get("title") and messages:
            from k8s_autopilot.state.session import _initial_prompt_from_messages

            prompt = _initial_prompt_from_messages(messages)
            if prompt:
                metadata["title"] = prompt
                metadata["initial_prompt"] = prompt
                metadata["user_query"] = prompt

        thread_resp = self._extract_thread_response(str_tid, metadata)

        # 4. Extract interrupt payloads from checkpoint_tuple if not already extracted
        if not interrupts and checkpoint_tuple:
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
        self, thread_id: uuid.UUID | str, limit: int = 10,
    ) -> ThreadHistoryResponse | None:
        """Get checkpoint history for time-travel/debugging."""
        str_tid = str(thread_id)
        config = {"configurable": {"thread_id": str_tid, "checkpoint_ns": ""}}

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

        tid_out: uuid.UUID | str = thread_id
        try:
            tid_out = uuid.UUID(str_tid)
        except Exception:
            tid_out = str_tid

        return ThreadHistoryResponse(
            thread_id=tid_out,
            states=states,
        )

    async def get_thread_telemetry(
        self, thread_id: uuid.UUID | str,
    ) -> ThreadTelemetryResponse | None:
        """Get authoritative status bar telemetry for a thread.

        Authoritative channels:
        - approval_mode: Live LangGraph store / ConfigStore / Settings
        - goal: objective, status, rubric_label, rubric
        - usage: input_tokens, output_tokens, total_tokens, cost_usd
        - model: spec, provider, name, reasoning_effort
        - subagents: registered subagents with status
        """
        str_tid = str(thread_id)
        config = {"configurable": {"thread_id": str_tid, "checkpoint_ns": ""}}

        from k8s_autopilot.config.settings import get_settings
        from k8s_autopilot.model.config import resolve_model_spec
        from k8s_autopilot.security.approval_mode import (
            approval_mode_key,
            aread_approval_mode_from_store,
        )
        from k8s_autopilot.subagents.loader import list_subagents

        settings = get_settings()

        # 1. Defaults
        approval_mode = getattr(settings, "approval_mode", "manual") or "manual"
        goal_obj: str | None = None
        goal_status: str | None = None
        rubric_text: str | None = None
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0

        # 2. Query state from agent graph if executor available
        agent = None
        if self._executor and hasattr(self._executor, "_ensure_agent"):
            try:
                agent = self._executor._ensure_agent()
                if agent and hasattr(agent, "aget_state"):
                    state_snapshot = await agent.aget_state(config)
                    if state_snapshot and state_snapshot.values:
                        values = state_snapshot.values
                        if "approval_mode" in values and values["approval_mode"]:
                            approval_mode = str(values["approval_mode"])
                        if "_goal_status" in values:
                            goal_status = values["_goal_status"]
                        if "_rubric_status" in values:
                            r_stat = str(values["_rubric_status"]).lower()
                            if r_stat in ("satisfied", "passed", "complete"):
                                goal_status = "complete"
                            elif r_stat in ("max_iterations_reached", "failed", "blocked") and goal_status != "complete":
                                goal_status = "blocked"
                        if "_goal_objective" in values:
                            goal_obj = values["_goal_objective"]
                        elif "goal" in values:
                            g = values["goal"]
                            goal_obj = g.get("objective") if isinstance(g, dict) else str(g)
                        elif "objective" in values:
                            goal_obj = str(values["objective"])

                        raw_rubric = values.get("rubric") or values.get("_goal_rubric") or values.get("criteria")
                        if isinstance(raw_rubric, list):
                            rubric_text = "\n".join(f"- {c}" for c in raw_rubric if c)
                        elif isinstance(raw_rubric, str):
                            rubric_text = raw_rubric

                        if "_session_cost_usd" in values:
                            try:
                                cost_usd = float(values["_session_cost_usd"])
                            except Exception:
                                pass
                        elif "cost_usd" in values:
                            try:
                                cost_usd = float(values["cost_usd"])
                            except Exception:
                                pass
            except Exception as exc:
                logger.debug(f"Telemetry state snapshot retrieval: {exc}")

        # 3. Read live approval mode from LangGraph Store if available
        try:
            store = getattr(self._checkpointer, "store", None) or (getattr(agent, "store", None) if agent else None)
            if store:
                stored_mode = await aread_approval_mode_from_store(store, approval_mode_key(str_tid))
                if stored_mode:
                    approval_mode = stored_mode.value
        except Exception as exc:
            logger.debug(f"Telemetry store approval mode retrieval: {exc}")

        # 4. Extract token counts and cost from checkpoints/writes via SessionManager
        try:
            from k8s_autopilot.state.session import SessionManager

            sm_in, sm_out, sm_cost, _ = await SessionManager().get_thread_token_usage_and_cost(str_tid)
            if sm_in or sm_out:
                input_tokens = sm_in
                output_tokens = sm_out
            if sm_cost > 0.0:
                cost_usd = max(cost_usd, sm_cost)
        except Exception as exc:
            logger.debug(f"Telemetry token extraction: {exc}")

        # 5. Derive Rubric Label
        rubric_label: str | None = None
        if goal_status == "complete":
            rubric_label = "✓ Goal complete"
        elif goal_status == "blocked":
            rubric_label = "⚠ Goal blocked"
        elif goal_status == "paused":
            rubric_label = "⏸ Goal paused"
        elif rubric_text and (goal_status == "active" or not goal_status):
            rubric_label = "✓ Rubric set"
        elif goal_status == "active":
            rubric_label = "Active"

        # 6. Model metadata
        model_spec = getattr(settings, "model", None) or getattr(settings, "model_name", "google_genai:gemini-3.7-flash") or "google_genai:gemini-3.7-flash"
        provider, model_name = resolve_model_spec(model_spec)
        reasoning_effort = getattr(settings, "reasoning_effort", "medium") or "medium"

        # 7. Subagents
        subagents: list[SubagentTelemetry] = []
        try:
            discovered = list_subagents(include_builtin=True)
            for sa in discovered:
                sa_name = sa.get("name") if isinstance(sa, dict) else getattr(sa, "name", "")
                if sa_name:
                    subagents.append(SubagentTelemetry(name=str(sa_name), status="idle"))

            from k8s_autopilot.subagents.loader import load_async_subagents
            async_subs = load_async_subagents()
            for asa in async_subs:
                asa_name = asa.get("name") if isinstance(asa, dict) else getattr(asa, "name", "")
                if asa_name and not any(s.name == str(asa_name) for s in subagents):
                    subagents.append(SubagentTelemetry(name=str(asa_name), status="idle"))
        except Exception as exc:
            logger.debug(f"Telemetry subagents list: {exc}")

        tid_out: uuid.UUID | str = thread_id
        try:
            tid_out = uuid.UUID(str_tid)
        except Exception:
            tid_out = str_tid

        return ThreadTelemetryResponse(
            thread_id=tid_out,
            approval_mode=approval_mode,
            goal=GoalTelemetry(
                objective=goal_obj,
                status=goal_status,
                rubric_label=rubric_label,
                rubric=rubric_text,
            ),
            usage=UsageTelemetry(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cost_usd=round(cost_usd, 4),
            ),
            model=ModelTelemetry(
                spec=model_spec,
                provider=provider,
                name=model_name,
                reasoning_effort=reasoning_effort,
            ),
            subagents=subagents,
        )

    async def auto_touch(
        self, thread_id: str, user_id: str = "default",
        agent_id: str = "", user_query: str = "",
    ) -> None:
        """Upsert the conversation record (used by a2a_executor)."""
        if not thread_id:
            return

        str_tid = thread_id
        config = {"configurable": {"thread_id": str_tid, "checkpoint_ns": ""}}
        checkpoint_tuple = await self._checkpointer.aget_tuple(config)  # type: ignore[arg-type]

        now_iso = datetime.now(UTC).isoformat()

        if checkpoint_tuple:
            # Thread is already initialized and tracked in the checkpointer DAG;
            # LangGraph Pregel execution will advance steps and history cleanly.
            return

        # Generate title from user_query for new threads
        title = ""
        if user_query and user_query.strip():
            title = user_query.strip()
            if len(title) > 60:
                title = title[:57].rsplit(" ", 1)[0] + "..."

        # Create root thread metadata
        metadata = {
            "source": "input",
            "step": -1,
            "run_id": str(uuid.uuid4()),
            "title": title,
            "agent_name": agent_id or "k8s-autopilot",
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
    global _thread_service  # noqa: PLW0603
    _thread_service = service
