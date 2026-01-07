from k8s_autopilot.utils.logger import AgentLogger
import inspect
import abc
import json
from typing import cast, Any, Dict, List, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    DataPart,
    InvalidParamsError,
    SendStreamingMessageSuccessResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
    UnsupportedOperationError,
    Part,
    Message,
)
from a2a.utils import new_agent_text_message, new_task, new_agent_parts_message
from a2a.utils.errors import ServerError
from k8s_autopilot.core.agents.types import BaseAgent
from langgraph.types import Command

# A2UI Extension imports
from a2ui.a2ui_extension import (
    try_activate_a2ui_extension,
    create_a2ui_part,
    A2UI_EXTENSION_URI,
)


logger = AgentLogger("K8S_AUTO_PILOT_EXECUTOR")


class ExecutorValidationMixin(abc.ABC):
    """
    Mixin to enforce extra validation/status mapping methods for all custom executors.
    """
    @abc.abstractmethod
    def _validate_request(self, context: RequestContext) -> bool:
        """
        Validate the incoming request context. Return True if invalid, False if valid.
        """
        pass

    @abc.abstractmethod
    def _map_status_to_task_state(self, custom_status: str) -> TaskState:
        """
        Map custom status strings to A2A TaskState enum values.
        """
        pass


class A2AAutoPilotExecutor(AgentExecutor, ExecutorValidationMixin):
    """AgentExecutor used by the tragel agents with JSON-RPC 2.0 validation support."""
    
    def _content_to_string(self, content: Any) -> str:
        """
        Convert content to string, handling dicts (e.g., interrupt responses).
        
        Args:
            content: Content that may be a string, dict, or other type
            
        Returns:
            String representation of the content
        """
        if isinstance(content, dict):
            # For interrupt responses, prefer the 'question' field if available
            if 'question' in content:
                return content['question']
            else:
                # Otherwise serialize the entire dict to JSON
                return json.dumps(content, indent=2)
        else:
            # Already a string or other type - convert to string
            return str(content)
    
    def _create_a2ui_parts(
        self,
        content: Any,
        status: str = "working",
        is_task_complete: bool = False,
        require_user_input: bool = False,
        response_type: str = "text",
        metadata: Optional[Dict[str, Any]] = None
    ) -> List[Part]:
        """
        Build A2UI Parts using programmatic builder (not LLM-generated).
        
        Routes to appropriate A2UI template based on response context.
        
        Args:
            content: The agent response content
            status: Current workflow status
            is_task_complete: Whether task is complete
            require_user_input: Whether user input is required
            response_type: Type of response
            metadata: Additional metadata
            
        Returns:
            List of A2A Parts containing A2UI data
        """
        from k8s_autopilot.core.a2ui import build_a2ui_for_response
        
        # Merge status into metadata for builder routing
        meta = metadata or {}
        if 'status' not in meta:
            meta['status'] = status
        
        return build_a2ui_for_response(
            content=content,
            is_task_complete=is_task_complete,
            require_user_input=require_user_input,
            response_type=response_type,
            metadata=meta
        )

    def __init__(self, agent: BaseAgent) -> None:
        self.agent: BaseAgent = agent

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        logger.log_structured(
            level="INFO",
            message=f'Executing agent {self.agent.name}',
            extra={"agent_name": self.agent.name}
        )
        error = self._validate_request(context)
        if error:
            logger.log_structured(
                level="ERROR",
                message='Validation error in request context',
                extra={"agent_name": self.agent.name}
            )
            raise ServerError(error=InvalidParamsError())

        query = context.get_user_input()
        
        # Check if client requested A2UI extension
        use_ui = try_activate_a2ui_extension(context)
        logger.log_structured(
            level="INFO",
            message="A2UI extension check",
            extra={"use_ui": use_ui, "agent_name": self.agent.name}
        )
        
        # Check for A2UI userAction in message parts (client UI interactions)
        user_action = None
        if context.message and context.message.parts:
            for part in context.message.parts:
                if hasattr(part, 'root') and isinstance(part.root, DataPart):
                    if isinstance(part.root.data, dict) and "userAction" in part.root.data:
                        user_action = part.root.data["userAction"]
                        action_name = user_action.get("name", "unknown")
                        action_context = user_action.get("context", {})
                        # Format as natural language query for the agent
                        query = f"USER_ACTION: {action_name}, CONTEXT: {json.dumps(action_context)}"
                        logger.log_structured(
                            level="INFO",
                            message="Received A2UI userAction from client",
                            extra={
                                "action_name": action_name,
                                "agent_name": self.agent.name
                            }
                        )
                        break
        
        logger.log_structured(
            level="DEBUG",
            message=f'User query: {query}',
            extra={"agent_name": self.agent.name}
        )

        task = context.current_task

        # Check if task exists and is in input_required state - convert to resume command
        if task and hasattr(task, 'status') and hasattr(task.status, 'state'):
            if task.status.state == TaskState.input_required:
                logger.log_structured(
                    level="INFO",
                    message='Task is in input_required state, converting message to Command(resume=...)',
                    extra={
                        "agent_name": self.agent.name,
                        "task_id": task.id,
                        "context_id": task.context_id,
                        "user_input_preview": str(query)[:100] if query else "None"
                    }
                )
                # Convert the user input to a Command for resuming the interrupted graph
                query = Command(resume=query)

        if not task:
            logger.log_structured(
                level="INFO",
                message='No current task found, creating new task',
                extra={"agent_name": self.agent.name}
            )
            if context.message is None:
                logger.log_structured(
                    level="ERROR",
                    message='No message provided for new task',
                    extra={"agent_name": self.agent.name}
                )
                raise ServerError(error=InvalidParamsError())
            task = new_task(context.message)
            logger.log_structured(
                level="INFO",
                message=f'Created new task with id: {task.id}, context_id: {task.context_id}',
                extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
            )
            await event_queue.enqueue_event(task)
            logger.log_structured(
                level="DEBUG",
                message=f'Enqueued new task: {task}',
                extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
            )

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        logger.log_structured(
            level="INFO",
            message=f'Starting agent stream',
            extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
        )

        try:
            # Ensure self.agent.stream is an async generator, or await if it's a coroutine returning one
            # Pass use_ui to agent so it can generate A2UI-formatted responses
            agent_stream = self.agent.stream(query, task.context_id, task.id, use_ui=use_ui)
            if not inspect.isasyncgen(agent_stream):
                agent_stream = await agent_stream  # type: ignore
            async for item in agent_stream:  # type: ignore
                    # Forward agent-to-agent events directly to the event queue
                    root = getattr(item, 'root', None)
                    if root is not None and isinstance(root, SendStreamingMessageSuccessResponse):
                        event = root.result
                        if isinstance(
                            event,
                            (TaskStatusUpdateEvent, TaskArtifactUpdateEvent),
                        ):
                            logger.log_structured(
                                level="INFO",
                                message=f'Enqueuing event from agent: {event}',
                                extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
                            )
                            await event_queue.enqueue_event(event)
                        continue
                    
                    # Safely access AgentResponse fields with defaults
                    is_task_complete = getattr(item, 'is_task_complete', False)
                    require_user_input = getattr(item, 'require_user_input', False)
                    
                    logger.log_structured(
                        level="DEBUG",
                        message='Processing agent response item',
                        extra={
                            "agent_name": self.agent.name,
                            "task_id": task.id,
                            "context_id": task.context_id,
                            "is_task_complete": is_task_complete,
                            "require_user_input": require_user_input,
                            "response_type": getattr(item, 'response_type', 'unknown'),
                            "has_content": hasattr(item, 'content')
                        }
                    )
                    
                    # Map custom status to A2A TaskState enum
                    custom_status = getattr(item, 'metadata', {}).get('status', 'working')
                    task_state = self._map_status_to_task_state(custom_status)
                    # logger.debug(f'Mapped custom status "{custom_status}" to task_state {task_state}')

                    if is_task_complete:
                        logger.log_structured(
                            level="INFO",
                            message='Task is marked as complete by agent',
                            extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id, "use_ui": use_ui}
                        )
                        
                        # Handle A2UI response if client supports it
                        if use_ui:
                            # Use programmatic A2UI builder (no LLM parsing needed)
                            final_parts = self._create_a2ui_parts(
                                content=item.content,
                                status="completed",
                                is_task_complete=True,
                                require_user_input=False,
                                response_type=item.response_type,
                                metadata=item.metadata
                            )
                            
                            await updater.add_artifact(
                                final_parts,
                                name=f'{self.agent.name}-result',
                            )
                            await updater.update_status(
                                TaskState.completed,
                                new_agent_parts_message(
                                    final_parts,
                                    task.context_id,
                                    task.id,
                                ),
                                final=True,
                            )
                        else:
                            # Standard text/data response
                            if item.response_type == 'data':
                                data_part: Part = cast(Part, DataPart(data=item.content))
                            else:
                                text_part: Part = cast(Part, TextPart(text=self._content_to_string(item.content)))

                            logger.log_structured(
                                level="INFO",
                                message='Adding artifact to updater',
                                extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
                            )
                            if item.response_type == 'data':
                                await updater.add_artifact(
                                    [data_part],
                                    name=f'{self.agent.name}-result',
                                )
                            else:
                                await updater.add_artifact(
                                    [text_part],
                                    name=f'{self.agent.name}-result',
                                )
                            logger.log_structured(
                                level="INFO",
                                message='Sending final status update: TaskState.completed',
                                extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
                            )
                            await updater.update_status(
                                TaskState.completed,
                                new_agent_text_message(
                                    "Task completed successfully.",
                                    task.context_id,
                                    task.id,
                                ),
                                final=True,
                            )
                        logger.log_structured(
                            level="INFO",
                            message='Calling updater.complete()',
                            extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
                        )
                        try:
                            await updater.complete()
                            logger.log_structured(
                                level="INFO",
                                message='Updater.complete() finished, exiting stream naturally',
                                extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
                            )
                        except RuntimeError as e:
                            # Handle case where task is already in terminal state
                            # This can happen if update_status(..., final=True) already marked it as terminal
                            if "already in a terminal state" in str(e):
                                logger.log_structured(
                                    level="INFO",
                                    message='Task already in terminal state, skipping complete()',
                                    extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
                                )
                            else:
                                # Re-raise if it's a different RuntimeError
                                raise
                        
                        # CRITICAL FIX: Use return instead of break to allow generator to close naturally
                        # Breaking closes the generator prematurely, causing GeneratorExit cascade that
                        # interrupts LangGraph's checkpoint writes. Return allows the generator to exhaust
                        # naturally, ensuring all async operations (including checkpoints) complete.
                        # See: docs/langgraph-comprehensive-solution.md
                        return
                    elif require_user_input:
                        logger.log_structured(
                            level="INFO",
                            message='Agent requires user input, updating status to input_required',
                            extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id, "use_ui": use_ui}
                        )
                        
                        # Handle A2UI response if client supports it
                        if use_ui:
                            # Use programmatic A2UI builder with HITL approval template
                            final_parts = self._create_a2ui_parts(
                                content=item.content,
                                status="input_required",
                                is_task_complete=False,
                                require_user_input=True,
                                response_type=item.response_type,
                                metadata=item.metadata
                            )
                            
                            await updater.update_status(
                                TaskState.input_required,
                                new_agent_parts_message(
                                    final_parts,
                                    task.context_id,
                                    task.id,
                                ),
                                final=True,
                            )
                        else:
                            await updater.update_status(
                                TaskState.input_required,
                                new_agent_text_message(
                                    self._content_to_string(item.content),
                                    task.context_id,
                                    task.id,
                                ),
                                final=True,
                            )
                        
                        logger.log_structured(
                            level="INFO",
                            message='Status updated to input_required, stream will complete naturally',
                            extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
                        )
                        # When LangGraph's interrupt() is called (due to require_user_input), 
                        # the graph stops yielding new items and the async for loop will naturally exhaust.
                        # We must NOT break here - breaking would close the generator prematurely,
                        # interrupting checkpoint writes and causing GeneratorExit cascades.
                        # Instead, let the loop continue until the supervisor's generator naturally exhausts.
                        # This allows LangGraph to complete checkpoint persistence before the generator closes.
                        # On resume, the checkpoint will be found and execution will continue from the saved state.
                    else:
                        # Normal status update (not complete, not requiring user input)
                        # This code runs when neither is_task_complete nor require_user_input is True
                        logger.log_structured(
                            level="INFO",
                            message=f'Updating status to {task_state}',
                            extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id, "use_ui": use_ui}
                        )
                        
                        # Handle A2UI response if client supports it
                        if use_ui:
                            # Use programmatic A2UI builder for working status
                            final_parts = self._create_a2ui_parts(
                                content=item.content,
                                status=custom_status,
                                is_task_complete=False,
                                require_user_input=False,
                                response_type=item.response_type,
                                metadata=item.metadata
                            )
                            
                            await updater.update_status(
                                task_state,
                                new_agent_parts_message(
                                    final_parts,
                                    task.context_id,
                                    task.id,
                                ),
                            )
                        else:
                            await updater.update_status(
                                task_state,
                                new_agent_text_message(
                                    self._content_to_string(item.content),
                                    task.context_id,
                                    task.id,
                                ),
                            )
        except Exception as e:
            logger.log_structured(
                level="ERROR",
                message=f'Exception in agent executor stream: {e}',
                extra={"agent_name": self.agent.name, "task_id": task.id, "context_id": task.context_id}
            )
            raise

    async def cancel(self, request: RequestContext, event_queue: EventQueue) -> Task | None:
        """
        Cancel the current agent execution if possible. Default implementation returns None.
        """
        return None

    def _validate_request(self, context: RequestContext) -> bool:
        """
        Validate the incoming request context. Default implementation returns False (valid).
        """
        return False

    def _map_status_to_task_state(self, custom_status: str) -> TaskState:
        """
        Map custom status strings to A2A TaskState enum values. Can be overridden by subclasses.
        """
        status_mapping: dict[str, TaskState] = {
            'working': TaskState.working,
            'input_required': TaskState.input_required,
            'completed': TaskState.completed,
            'failed': TaskState.failed,
            'error': TaskState.failed,
            'submitted': TaskState.submitted,
        }
        return status_mapping.get(custom_status, TaskState.working) 