from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest, Task, TaskState, TaskStatus

from almena_agent.a2a.tasks import BoundedTaskStore


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Account(User):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


def as_(account: str) -> ServerCallContext:
    return ServerCallContext(user=Account(account))


def task(task_id: str, state: TaskState) -> Task:
    return Task(id=task_id, context_id="c", status=TaskStatus(state=state))


ALICE = as_("account-1")
BOB = as_("account-2")
DONE = TaskState.TASK_STATE_COMPLETED
WORKING = TaskState.TASK_STATE_WORKING


async def test_finished_tasks_are_dropped_once_idle() -> None:
    clock = Clock()
    store = BoundedTaskStore(idle_seconds=60, max_tasks=100, clock=clock)
    await store.save(task("done", DONE), ALICE)
    await store.save(task("rejected", TaskState.TASK_STATE_REJECTED), ALICE)
    await store.save(task("running", WORKING), ALICE)

    clock.now = 59
    assert await store.get("done", ALICE) is not None  # reading it keeps it

    clock.now = 100
    assert await store.get("rejected", ALICE) is None
    assert await store.get("done", ALICE) is not None
    clock.now = 200
    assert await store.get("done", ALICE) is None
    assert await store.get("running", ALICE) is not None
    assert len(store) == 1


async def test_an_in_flight_task_that_finishes_starts_its_idle_time_then() -> None:
    clock = Clock()
    store = BoundedTaskStore(idle_seconds=60, max_tasks=100, clock=clock)
    await store.save(task("t", WORKING), ALICE)
    clock.now = 500
    await store.save(task("t", DONE), ALICE)
    clock.now = 559
    assert await store.get("t", ALICE) is not None
    clock.now = 700
    assert await store.get("t", ALICE) is None


async def test_beyond_the_cap_the_least_recently_used_finished_go_first() -> None:
    clock = Clock()
    store = BoundedTaskStore(idle_seconds=3600, max_tasks=3, clock=clock)
    await store.save(task("old", DONE), ALICE)
    await store.save(task("read", DONE), BOB)
    await store.save(task("running", WORKING), ALICE)
    await store.get("read", BOB)
    await store.save(task("new", DONE), BOB)

    assert len(store) == 3
    assert await store.get("old", ALICE) is None
    assert await store.get("read", BOB) is not None
    assert await store.get("new", BOB) is not None
    assert await store.get("running", ALICE) is not None


async def test_in_flight_tasks_are_kept_past_the_cap() -> None:
    store = BoundedTaskStore(idle_seconds=3600, max_tasks=2)
    for n in range(4):
        await store.save(task(f"t{n}", WORKING), ALICE)
    await store.save(task("done", DONE), ALICE)
    assert len(store) == 4
    for n in range(4):
        assert await store.get(f"t{n}", ALICE) is not None
    assert await store.get("done", ALICE) is None


async def test_tasks_stay_with_their_owner() -> None:
    store = BoundedTaskStore(idle_seconds=60, max_tasks=10)
    await store.save(task("t", DONE), ALICE)
    assert await store.get("t", BOB) is None
    assert (await store.list(ListTasksRequest(), BOB)).tasks == []
    await store.delete("t", BOB)
    assert await store.get("t", ALICE) is not None
    assert len(store) == 1

    await store.delete("t", ALICE)
    assert await store.get("t", ALICE) is None
    assert len(store) == 0


async def test_list_leaves_out_dropped_tasks() -> None:
    clock = Clock()
    store = BoundedTaskStore(idle_seconds=60, max_tasks=10, clock=clock)
    await store.save(task("done", DONE), ALICE)
    await store.save(task("running", WORKING), ALICE)
    clock.now = 61
    listed = await store.list(ListTasksRequest(), ALICE)
    assert [t.id for t in listed.tasks] == ["running"]
