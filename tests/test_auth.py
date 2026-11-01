"""Every A2A call needs a registry token: who gets in, how often, and what they see."""

import asyncio
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from almena_agent.a2a.auth import RateLimiter
from almena_agent.a2a.executor import ClaudeAgentExecutor
from almena_agent.accounts import AccountsUnavailable, ApiAccounts
from almena_agent.config import get_settings
from almena_agent.conversations import Conversations
from almena_agent.main import create_app
from tests.conftest import FakeAccounts, FakeModel, FakeRegistry, connect
from tests.test_a2a import send, user_message


def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def send_request(text: str = "Hi") -> dict[str, Any]:
    return rpc("SendMessage", {"message": user_message(text)})


# Authentication.


async def test_without_a_token_nothing_runs(anonymous: AsyncClient, model: FakeModel) -> None:
    response = await anonymous.post("/a2a/jsonrpc", json=send_request())
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    error = response.json()["error"]
    assert error["code"] == -32000
    assert error["data"][0]["reason"] == "UNAUTHENTICATED"

    rest = await anonymous.post("/a2a/rest/message:send", json={"message": user_message("Hi")})
    assert rest.status_code == 401
    assert rest.json()["error"]["status"] == "UNAUTHENTICATED"
    tasks = await anonymous.get("/a2a/rest/tasks")
    assert tasks.status_code == 401
    assert model.calls == []


async def test_a_token_the_registry_refuses_runs_nothing(
    app: Any, model: FakeModel, accounts: FakeAccounts
) -> None:
    async with connect(app, "stolen") as client:
        response = await client.post("/a2a/jsonrpc", json=send_request())
    assert response.status_code == 401
    assert accounts.asked == ["stolen"]
    assert model.calls == []


async def test_an_accepted_token_is_remembered(
    client: AsyncClient, model: FakeModel, accounts: FakeAccounts
) -> None:
    await send(client, user_message("Hi"))
    await send(client, user_message("Again"))
    assert accounts.asked == ["good"]
    assert len(model.calls) == 2


async def test_an_unreachable_registry_lets_nobody_new_in(
    client: AsyncClient, model: FakeModel, accounts: FakeAccounts
) -> None:
    accounts.down = True
    response = await client.post("/a2a/jsonrpc", json=send_request())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == -32603
    assert model.calls == []


async def test_public_routes_need_no_token(anonymous: AsyncClient) -> None:
    assert (await anonymous.get("/health")).status_code == 200
    assert (await anonymous.get("/")).status_code == 200


# Rate limits.


async def test_each_account_has_its_rate(
    monkeypatch: pytest.MonkeyPatch,
    model: FakeModel,
    registry: FakeRegistry,
    accounts: FakeAccounts,
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit", 2)
    app = create_app(model, registry, accounts)
    async with connect(app) as client, connect(app, "other") as other:
        for _ in range(2):
            assert (await client.post("/a2a/jsonrpc", json=send_request())).status_code == 200
        limited = await client.post("/a2a/jsonrpc", json=send_request())
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0
        assert limited.json()["error"]["data"][0]["reason"] == "RATE_LIMIT_EXCEEDED"
        assert (await other.post("/a2a/jsonrpc", json=send_request())).status_code == 200
    assert len(model.calls) == 3


async def test_refused_tokens_are_limited_per_address(
    monkeypatch: pytest.MonkeyPatch,
    model: FakeModel,
    registry: FakeRegistry,
    accounts: FakeAccounts,
) -> None:
    monkeypatch.setattr(get_settings(), "auth_failure_limit", 2)
    app = create_app(model, registry, accounts)
    async with connect(app, "wrong") as wrong, connect(app) as good:
        for _ in range(2):
            assert (await wrong.post("/a2a/jsonrpc", json=send_request())).status_code == 401
        assert (await wrong.post("/a2a/jsonrpc", json=send_request())).status_code == 429
        # The same address is held back whatever it sends, until the window ends.
        assert (await good.post("/a2a/jsonrpc", json=send_request())).status_code == 429
    assert accounts.asked == ["wrong", "wrong"]


def test_a_limit_lifts_when_its_window_ends() -> None:
    now = [0.0]
    limiter = RateLimiter(limit=1, window=60, clock=lambda: now[0])
    limiter.hit("a")
    assert limiter.retry_after("a") == 60
    assert limiter.retry_after("b") == 0
    now[0] = 59.5
    assert limiter.retry_after("a") == 1
    now[0] = 60
    assert limiter.retry_after("a") == 0


# Tasks belong to the account that sent them.


async def test_another_account_cannot_see_a_task(client: AsyncClient, other: AsyncClient) -> None:
    task = await send(client, user_message("Hi"))

    mine = (await client.post("/a2a/jsonrpc", json=rpc("GetTask", {"id": task["id"]}))).json()
    assert mine["result"]["id"] == task["id"]
    theirs = (await other.post("/a2a/jsonrpc", json=rpc("GetTask", {"id": task["id"]}))).json()
    assert theirs["error"]["code"] == -32001
    assert (await other.get(f"/a2a/rest/tasks/{task['id']}")).status_code == 404

    listed = (await client.post("/a2a/jsonrpc", json=rpc("ListTasks", {}))).json()["result"]
    assert [t["id"] for t in listed["tasks"]] == [task["id"]]
    listed = (await other.post("/a2a/jsonrpc", json=rpc("ListTasks", {}))).json()["result"]
    assert listed.get("tasks", []) == []


async def test_only_its_account_cancels_a_working_task(
    client: AsyncClient, other: AsyncClient, model: FakeModel
) -> None:
    model.hold = asyncio.Event()
    params = {
        "message": user_message("Take your time"),
        "configuration": {"returnImmediately": True},
    }
    response = await client.post("/a2a/jsonrpc", json=rpc("SendMessage", params))
    task = response.json()["result"]["task"]
    assert task["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")

    theirs = (await other.post("/a2a/jsonrpc", json=rpc("CancelTask", {"id": task["id"]}))).json()
    assert theirs["error"]["code"] == -32001
    mine = (await client.post("/a2a/jsonrpc", json=rpc("CancelTask", {"id": task["id"]}))).json()
    assert mine["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    model.hold.set()


# Memory: conversations are dropped, never trimmed.


def test_idle_conversations_are_dropped() -> None:
    now = [0.0]
    conversations = Conversations(10, idle_seconds=60, max_contexts=10, clock=lambda: now[0])
    conversations.record("c1", "a", [{"role": "user", "content": "Hi"}])
    now[0] = 30
    conversations.record("c2", "a", [{"role": "user", "content": "Hi"}])
    now[0] = 70
    assert len(conversations) == 1
    assert conversations.history("c1") == []
    # Dropped, the context is anyone's to start again.
    assert conversations.belongs_to("c1", "b")
    assert conversations.history("c2") != []


def test_the_least_recently_used_conversation_goes_first() -> None:
    conversations = Conversations(10, idle_seconds=3600, max_contexts=2)
    for context in ("c1", "c2"):
        conversations.record(context, "a", [{"role": "user", "content": "Hi"}])
    conversations.history("c1")
    conversations.record("c3", "a", [{"role": "user", "content": "Hi"}])
    assert len(conversations) == 2
    assert conversations.history("c2") == []
    assert conversations.history("c1") != []


async def test_no_lock_outlives_its_messages() -> None:
    executor = ClaudeAgentExecutor(FakeModel(), Conversations(10))
    async with executor._turn("c1"):
        async with asyncio.timeout(1):
            waiting = asyncio.create_task(_take(executor, "c1"))
            await asyncio.sleep(0)
            assert executor._locks["c1"][1] == 2
    await waiting
    assert executor._locks == {}


async def _take(executor: ClaudeAgentExecutor, context_id: str) -> None:
    async with executor._turn(context_id):
        pass


# The client of the API's account endpoint.


def _account_endpoint(request: httpx.Request) -> httpx.Response:
    token = request.headers.get("authorization")
    if token == "Bearer good":
        return httpx.Response(200, json={"id": "4f1c", "email": None, "alias": None})
    if token == "Bearer broken":
        return httpx.Response(500)
    return httpx.Response(401, json={"detail": "not_authenticated"})


async def test_the_account_client_asks_the_api() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_account_endpoint))
    accounts = ApiAccounts("http://registry/api/v1/auth/me", http)
    assert await accounts.identify("good") == "4f1c"
    assert await accounts.identify("bad") is None
    with pytest.raises(AccountsUnavailable):
        await accounts.identify("broken")
