"""The A2A executor: turns each incoming message into a task Claude answers.

A message opens (or continues) a task; Claude's text streams into the task as
one artifact, and the task ends completed, rejected when Claude declines or
the conversation is full, or failed when Claude cannot be reached. A2A
contexts are conversations: messages sharing a ``contextId`` see the turns
before them. The caller is the registry account ``auth.A2AAuth`` let in; a
call without one never gets here, and is rejected if it does.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message
from anthropic.types.beta import BetaMessageParam

from almena_agent.conversations import Conversations
from almena_agent.llm import Model, ModelUnavailable
from almena_agent.registry import Registry, RegistryRefused, RegistryUnavailable, Toolbox

logger = logging.getLogger(__name__)

RESPONSE_ARTIFACT = "response"


class ClaudeAgentExecutor(AgentExecutor):
    def __init__(
        self, model: Model, conversations: Conversations, registry: Registry | None = None
    ) -> None:
        self._model = model
        self._conversations = conversations
        self._registry = registry
        # One message at a time per conversation, so turns stay in order: each
        # lock with the messages holding or awaiting it, dropped when none is.
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None:
            raise ValueError("the request carries no message")
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        text = context.get_user_input().strip()
        if not text:
            await updater.reject(self._say(updater, "Send the request as text."))
            return

        caller = context.call_context.user
        token = _bearer(context)
        if not caller.is_authenticated or not caller.user_name or token is None:
            await updater.reject(self._say(updater, "Send a registry token."))
            return
        owner = caller.user_name

        async with self._turn(task.context_id):
            if not self._conversations.belongs_to(task.context_id, owner):
                await updater.reject(
                    self._say(updater, "This conversation belongs to another caller.")
                )
                return
            if self._conversations.is_full(task.context_id):
                await updater.reject(
                    self._say(updater, "This conversation is full: start a new context.")
                )
                return
            toolbox: Toolbox | None = None
            if self._registry is not None:
                try:
                    toolbox = await self._registry.open(token)
                except RegistryRefused:
                    await updater.reject(
                        self._say(updater, "The registry does not accept this token.")
                    )
                    return
                except RegistryUnavailable:
                    # The agent can still converse; it says so if it needs the registry.
                    logger.exception("The registry could not be reached")
            await updater.start_work()
            user: BetaMessageParam = {"role": "user", "content": text}
            history = [*self._conversations.history(task.context_id), user]
            artifact_id = str(uuid.uuid4())
            first = True

            async def on_text(chunk: str) -> None:
                nonlocal first
                await updater.add_artifact(
                    [new_text_part(chunk)],
                    artifact_id=artifact_id,
                    name=RESPONSE_ARTIFACT,
                    append=not first,
                )
                first = False

            try:
                reply = await self._model.reply(history, on_text, toolbox)
            except ModelUnavailable:
                # The cause is logged, never sent: it can hold internal details.
                logger.exception("Claude could not answer in context %s", task.context_id)
                await updater.failed(self._say(updater, "The agent is unavailable; try again."))
                return
            if reply.refused:
                logger.info("Claude declined a request in context %s", task.context_id)
                await updater.reject(self._say(updater, "The agent cannot help with this request."))
                return
            self._conversations.record(task.context_id, owner, [user, *reply.turns])
            await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The framework has already cancelled execute(); record the new state.
        task = context.current_task
        if task is None:
            return
        await TaskUpdater(event_queue, task.id, task.context_id).cancel()

    @asynccontextmanager
    async def _turn(self, context_id: str) -> AsyncIterator[None]:
        lock, waiting = self._locks.get(context_id, (asyncio.Lock(), 0))
        self._locks[context_id] = (lock, waiting + 1)
        try:
            async with lock:
                yield
        finally:
            lock, waiting = self._locks[context_id]
            if waiting == 1:
                del self._locks[context_id]
            else:
                self._locks[context_id] = (lock, waiting - 1)

    @staticmethod
    def _say(updater: TaskUpdater, text: str) -> Message:
        return updater.new_agent_message([new_text_part(text)])


def _bearer(context: RequestContext) -> str | None:
    """The registry token the request carries (``Authorization: Bearer``), if any."""
    headers = context.call_context.state.get("headers", {})
    scheme, _, token = str(headers.get("authorization", "")).partition(" ")
    return token.strip() or None if scheme.lower() == "bearer" else None
