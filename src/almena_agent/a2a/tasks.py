"""The A2A task store: the a2a-sdk's in-memory one, bounded.

``InMemoryTaskStore`` keeps every task forever. ``BoundedTaskStore`` wraps it
and drops finished tasks (completed, failed, canceled or rejected) once nobody
has saved or read them for ``AGENT_TASK_IDLE_MINUTES``, and the least recently
used finished ones beyond ``AGENT_MAX_TASKS`` in all. Tasks still in flight
(submitted, working, or waiting on input or auth) are never dropped: while
more than that many run, the store holds them all. A dropped task is not
found, as another account's is.

Tasks stay scoped by their owner, the request's user (the caller's account,
set by ``A2AAuth``): the wrapped store keeps them per owner, and so does this
bookkeeping.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task, TaskState

from almena_agent.metrics import METRICS, EvictionReason

FINISHED: frozenset[TaskState] = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)


@dataclass
class _Entry:
    finished: bool
    used_at: float


class _Owner(User):
    """The owner of a task being dropped, as the wrapped store resolves it."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


class BoundedTaskStore(TaskStore):
    def __init__(
        self,
        idle_seconds: float = 3600,
        max_tasks: int = 5000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_seconds = idle_seconds
        self._max_tasks = max_tasks
        self._clock = clock
        # Owner-scoped like the SDK's default; deleting builds a context whose
        # user resolves to the same owner.
        self._store = InMemoryTaskStore(owner_resolver=resolve_user_scope)
        # (owner, task id), least recently used first.
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    async def save(self, task: Task, context: ServerCallContext) -> None:
        key = (resolve_user_scope(context), task.id)
        await self._store.save(task, context)
        self._entries[key] = _Entry(finished=task.status.state in FINISHED, used_at=self._clock())
        self._entries.move_to_end(key)
        await self._evict()
        METRICS.holding("task", len(self._entries))

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        await self._evict()
        task = await self._store.get(task_id, context)
        entry = self._entries.get(key := (resolve_user_scope(context), task_id))
        if task is not None and entry is not None:
            entry.used_at = self._clock()
            self._entries.move_to_end(key)
        return task

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        await self._evict()
        return await self._store.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        self._entries.pop((resolve_user_scope(context), task_id), None)
        await self._store.delete(task_id, context)
        METRICS.holding("task", len(self._entries))

    async def _evict(self) -> None:
        # Chosen in one pass, with no await in between: the bookkeeping never
        # sees a half-finished eviction.
        now = self._clock()
        excess = len(self._entries) - self._max_tasks
        victims: list[tuple[str, str]] = []
        reasons: list[EvictionReason] = []
        for key, entry in self._entries.items():
            if not entry.finished:
                continue
            idle = now - entry.used_at >= self._idle_seconds
            if idle or excess > 0:
                victims.append(key)
                reasons.append("idle" if idle else "capacity")
                excess -= 1
        for key, reason in zip(victims, reasons, strict=True):
            del self._entries[key]
            METRICS.evicted("task", reason)
        if victims:
            METRICS.holding("task", len(self._entries))
        for owner, task_id in victims:
            await self._store.delete(task_id, ServerCallContext(user=_Owner(owner)))
