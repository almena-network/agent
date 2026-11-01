"""Prometheus metrics: what they count, and that only their own listener serves them."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from anthropic import AnthropicError
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaUsage
from httpx import AsyncClient
from pydantic import ValidationError

from almena_agent.a2a.tasks import BoundedTaskStore
from almena_agent.config import Settings, get_settings
from almena_agent.conversations import Conversations
from almena_agent.llm import Claude, ModelUnavailable
from almena_agent.main import create_app
from almena_agent.metrics import METRICS, Metrics, parse_addr, serve
from tests.conftest import FakeAccounts, FakeModel, FakeRegistry, connect
from tests.test_a2a import send, user_message
from tests.test_task_store import ALICE, DONE, WORKING, Clock, task


def value(name: str, **labels: str) -> float:
    return METRICS.registry.get_sample_value(name, labels) or 0.0


def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


SEND = rpc("SendMessage", {"message": user_message("Hi")})


# Not public.


async def test_the_public_app_has_no_metrics(client: AsyncClient, anonymous: AsyncClient) -> None:
    for caller in (client, anonymous):
        for path in ("/metrics", "/metrics/"):
            response = await caller.get(path, follow_redirects=True)
            assert response.status_code == 404
            assert "agent_" not in response.text
    openapi = (await anonymous.get("/openapi.json")).json()
    assert not any("metrics" in path for path in openapi["paths"])


def test_the_metrics_listener_serves_them() -> None:
    metrics = Metrics()
    metrics.authenticated("accepted")
    port, stop = serve("127.0.0.1:0", metrics)
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
    finally:
        stop()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'agent_auth_total{result="accepted"} 1.0' in response.text
    assert "agent_claude_call_seconds_bucket" in response.text
    assert "process_cpu_seconds_total" in response.text or "python_info" in response.text


def test_the_metrics_address_is_host_and_port() -> None:
    assert parse_addr("127.0.0.1:9090") == ("127.0.0.1", 9090)
    assert parse_addr("[::1]:9090") == ("::1", 9090)
    assert Settings(metrics_addr="").metrics_addr == ""
    for wrong in ("9090", "localhost", "host:port", ":9090", "host:70000"):
        with pytest.raises(ValidationError):
            Settings(metrics_addr=wrong)


# A2A requests and authentication.


async def test_requests_are_counted_by_method_and_outcome(client: AsyncClient) -> None:
    sent = value("agent_a2a_requests_total", method="SendMessage", outcome="ok")
    missing = value("agent_a2a_requests_total", method="GetTask", outcome="TaskNotFoundError")
    await send(client, user_message("Hi"))
    await client.post("/a2a/jsonrpc", json=rpc("GetTask", {"id": "nope"}))
    assert value("agent_a2a_requests_total", method="SendMessage", outcome="ok") == sent + 1
    assert (
        value("agent_a2a_requests_total", method="GetTask", outcome="TaskNotFoundError")
        == missing + 1
    )


async def test_auth_results_are_counted(app: Any, accounts: FakeAccounts) -> None:
    before = {
        result: value("agent_auth_total", result=result)
        for result in ("accepted", "cached", "refused", "missing", "unavailable")
    }
    async with connect(app) as good, connect(app, "wrong") as wrong, connect(app, None) as none:
        await good.post("/a2a/jsonrpc", json=SEND)
        await good.post("/a2a/jsonrpc", json=SEND)
        await wrong.post("/a2a/jsonrpc", json=SEND)
        await none.post("/a2a/jsonrpc", json=SEND)
        accounts.down = True
        async with connect(app, "other") as other:
            assert (await other.post("/a2a/jsonrpc", json=SEND)).status_code == 503
    after = {result: value("agent_auth_total", result=result) for result in before}
    assert {result: after[result] - before[result] for result in before} == {
        "accepted": 1,
        "cached": 1,
        "refused": 1,
        "missing": 1,
        "unavailable": 1,
    }


async def test_rate_limit_refusals_are_counted(
    monkeypatch: pytest.MonkeyPatch,
    model: FakeModel,
    registry: FakeRegistry,
    accounts: FakeAccounts,
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit", 1)
    monkeypatch.setattr(get_settings(), "auth_failure_limit", 1)
    account = value("agent_rate_limited_total", limit="account")
    ip = value("agent_rate_limited_total", limit="ip")
    app = create_app(model, registry, accounts)
    async with connect(app) as good:
        await good.post("/a2a/jsonrpc", json=SEND)
        assert (await good.post("/a2a/jsonrpc", json=SEND)).status_code == 429
    async with connect(app, "wrong") as wrong:
        await wrong.post("/a2a/jsonrpc", json=SEND)
        assert (await wrong.post("/a2a/jsonrpc", json=SEND)).status_code == 429
    assert value("agent_rate_limited_total", limit="account") == account + 1
    assert value("agent_rate_limited_total", limit="ip") == ip + 1


# Claude.


def _message(stop: str) -> BetaMessage:
    return BetaMessage(
        id="msg",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        content=[BetaTextBlock(type="text", text="Hi.", citations=None)],
        stop_reason=stop,
        stop_sequence=None,
        usage=BetaUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=None,
        ),
    )


class _Stream:
    def __init__(self, message: BetaMessage) -> None:
        self._message = message

    @property
    async def text_stream(self) -> AsyncIterator[str]:
        yield "Hi."

    async def get_final_message(self) -> BetaMessage:
        return self._message


class _Client:
    def __init__(self, answer: BetaMessage | None) -> None:
        self._answer = answer
        self.beta = self
        self.messages = self

    @asynccontextmanager
    async def stream(self, **request: Any) -> AsyncIterator[_Stream]:
        if self._answer is None:
            raise AnthropicError("connection refused")
        yield _Stream(self._answer)


async def _ignore(chunk: str) -> None:
    pass


async def test_claude_calls_latency_and_tokens_are_counted() -> None:
    names = [("agent_claude_calls_total", {"outcome": o}) for o in ("ok", "refused", "error")]
    names += [("agent_claude_tokens_total", {"kind": k}) for k in ("input", "output", "cache_read")]
    names += [("agent_claude_call_seconds_count", {})]
    before = [value(name, **labels) for name, labels in names]

    user: Any = [{"role": "user", "content": "Hi"}]
    await Claude(get_settings(), _Client(_message("end_turn"))).reply(user, _ignore)  # type: ignore[arg-type]
    refused = await Claude(get_settings(), _Client(_message("refusal"))).reply(user, _ignore)  # type: ignore[arg-type]
    assert refused.refused
    with pytest.raises(ModelUnavailable):
        await Claude(get_settings(), _Client(None)).reply(user, _ignore)  # type: ignore[arg-type]

    after = [value(name, **labels) for name, labels in names]
    assert [a - b for a, b in zip(after, before, strict=True)] == [1, 1, 1, 20, 10, 200, 3]
    assert value("agent_claude_tokens_total", kind="cache_creation") == 0


# What memory holds, and what it drops.


def test_conversations_held_and_evicted_are_counted() -> None:
    idle = value("agent_evictions_total", kind="conversation", reason="idle")
    capacity = value("agent_evictions_total", kind="conversation", reason="capacity")
    now = [0.0]
    conversations = Conversations(10, idle_seconds=60, max_contexts=2, clock=lambda: now[0])
    for context in ("c1", "c2", "c3"):
        conversations.record(context, "a", [{"role": "user", "content": "Hi"}])
    assert value("agent_held", kind="conversation") == 2
    now[0] = 100
    assert len(conversations) == 0
    assert value("agent_held", kind="conversation") == 0
    assert value("agent_evictions_total", kind="conversation", reason="capacity") == capacity + 1
    assert value("agent_evictions_total", kind="conversation", reason="idle") == idle + 2


async def test_tasks_held_and_evicted_are_counted() -> None:
    idle = value("agent_evictions_total", kind="task", reason="idle")
    capacity = value("agent_evictions_total", kind="task", reason="capacity")
    clock = Clock()
    store = BoundedTaskStore(idle_seconds=60, max_tasks=2, clock=clock)
    await store.save(task("t1", DONE), ALICE)
    await store.save(task("t2", WORKING), ALICE)
    await store.save(task("t3", DONE), ALICE)
    assert value("agent_held", kind="task") == 2
    clock.now = 100
    assert await store.get("t3", ALICE) is None
    assert value("agent_held", kind="task") == 1
    assert value("agent_evictions_total", kind="task", reason="capacity") == capacity + 1
    assert value("agent_evictions_total", kind="task", reason="idle") == idle + 1
