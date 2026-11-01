"""Counts the A2A requests: the bindings' handler, wrapped.

``MeteredRequestHandler`` hands every call on to the a2a-sdk's handler and
counts it in ``agent_a2a_requests`` by the A2A method's name and its
outcome: ``ok``, the ``A2AError``'s class name (``TaskNotFoundError``…),
``error`` for anything else, or ``aborted`` when a stream ends early (the
peer went away). Calls ``A2AAuth`` refuses never get here: the auth metrics
count those.
"""

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from contextlib import asynccontextmanager

from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import Event
from a2a.server.request_handlers import RequestHandler
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    SendMessageRequest,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
)
from a2a.utils.errors import A2AError

from almena_agent.metrics import METRICS


class _Outcome:
    value = "aborted"


@asynccontextmanager
async def _counted(method: str) -> AsyncIterator[_Outcome]:
    outcome = _Outcome()
    try:
        yield outcome
    except A2AError as error:
        outcome.value = type(error).__name__
        raise
    except Exception:
        outcome.value = "error"
        raise
    finally:
        METRICS.request(method, outcome.value)


async def _call[T](method: str, call: Awaitable[T]) -> T:
    async with _counted(method) as outcome:
        result = await call
        outcome.value = "ok"
        return result


async def _stream(method: str, events: AsyncGenerator[Event]) -> AsyncGenerator[Event]:
    async with _counted(method) as outcome:
        async for event in events:
            yield event
        outcome.value = "ok"


class MeteredRequestHandler(RequestHandler):
    def __init__(self, handler: RequestHandler) -> None:
        self._handler = handler

    async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
        return await _call("GetTask", self._handler.on_get_task(params, context))

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        return await _call("ListTasks", self._handler.on_list_tasks(params, context))

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        return await _call("CancelTask", self._handler.on_cancel_task(params, context))

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | Message:
        return await _call("SendMessage", self._handler.on_message_send(params, context))

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        events = self._handler.on_message_send_stream(params, context)
        async for event in _stream("SendStreamingMessage", events):
            yield event

    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        return await _call(
            "CreateTaskPushNotificationConfig",
            self._handler.on_create_task_push_notification_config(params, context),
        )

    async def on_get_task_push_notification_config(
        self, params: GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        return await _call(
            "GetTaskPushNotificationConfig",
            self._handler.on_get_task_push_notification_config(params, context),
        )

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        events = self._handler.on_subscribe_to_task(params, context)
        async for event in _stream("SubscribeToTask", events):
            yield event

    async def on_list_task_push_notification_configs(
        self, params: ListTaskPushNotificationConfigsRequest, context: ServerCallContext
    ) -> ListTaskPushNotificationConfigsResponse:
        return await _call(
            "ListTaskPushNotificationConfigs",
            self._handler.on_list_task_push_notification_configs(params, context),
        )

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        await _call(
            "DeleteTaskPushNotificationConfig",
            self._handler.on_delete_task_push_notification_config(params, context),
        )

    async def on_get_extended_agent_card(
        self, params: GetExtendedAgentCardRequest, context: ServerCallContext
    ) -> AgentCard:
        return await _call(
            "GetExtendedAgentCard", self._handler.on_get_extended_agent_card(params, context)
        )
