"""The registry's tools: who gets them, the loop that uses them, the MCP client."""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaTextBlock,
    BetaToolUseBlock,
    BetaUsage,
)
from httpx import AsyncClient

from almena_agent.config import get_settings
from almena_agent.llm import Claude
from almena_agent.main import create_app
from almena_agent.registry import McpRegistry, RegistryRefused
from tests.conftest import FakeAccounts, FakeModel, FakeRegistry, FakeToolbox, connect
from tests.test_a2a import user_message

GOOD = {"Authorization": "Bearer good"}


async def _send(
    client: AsyncClient, message: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}
    response = await client.post("/a2a/jsonrpc", json=request, headers=headers)
    assert response.status_code == 200
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


async def test_a_token_brings_the_registry_tools(
    client: AsyncClient, model: FakeModel, registry: FakeRegistry
) -> None:
    task = await _send(client, user_message("My tenants?"), GOOD)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert registry.opened == ["good"]
    (toolbox,) = model.toolboxes
    assert isinstance(toolbox, FakeToolbox)
    assert toolbox.token == "good"


async def test_without_the_mcp_endpoint_the_agent_only_converses(
    model: FakeModel, accounts: FakeAccounts
) -> None:
    async with connect(create_app(model, None, accounts)) as client:
        task = await _send(client, user_message("Hi"))
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert model.toolboxes == [None]


async def test_a_token_the_registry_mcp_refuses_rejects_the_task(
    client: AsyncClient, model: FakeModel
) -> None:
    task = await _send(client, user_message("Hi"), {"Authorization": "Bearer revoked"})
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert model.calls == []


async def test_an_unreachable_registry_leaves_the_conversation(
    client: AsyncClient, model: FakeModel, registry: FakeRegistry
) -> None:
    registry.down = True
    task = await _send(client, user_message("Hi"), GOOD)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert model.toolboxes == [None]


async def test_a_context_stays_with_whoever_started_it(
    client: AsyncClient, model: FakeModel
) -> None:
    first = await _send(client, user_message("My tenants?"), GOOD)
    context = first["contextId"]
    theirs = await _send(
        client, user_message("And theirs?", context), {"Authorization": "Bearer other"}
    )
    assert theirs["status"]["state"] == "TASK_STATE_REJECTED"
    again = await _send(client, user_message("Thanks", context), GOOD)
    assert again["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len(model.calls) == 2


# The loop: Claude asks for tools until it answers.


def _message(content: list[Any], stop: str) -> BetaMessage:
    return BetaMessage(
        id="msg",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        content=content,
        stop_reason=stop,
        stop_sequence=None,
        usage=BetaUsage(input_tokens=1, output_tokens=1),
    )


class _Stream:
    def __init__(self, message: BetaMessage) -> None:
        self._message = message

    @property
    async def text_stream(self) -> AsyncIterator[str]:
        for block in self._message.content:
            if block.type == "text":
                yield block.text

    async def get_final_message(self) -> BetaMessage:
        return self._message


class _Messages:
    def __init__(self, answers: Sequence[BetaMessage]) -> None:
        self._answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    @asynccontextmanager
    async def stream(self, **request: Any) -> AsyncIterator[_Stream]:
        self.requests.append({**request, "messages": list(request["messages"])})
        yield _Stream(self._answers.pop(0))


class _Client:
    def __init__(self, answers: Sequence[BetaMessage]) -> None:
        self.messages = _Messages(answers)
        self.beta = self


async def test_claude_calls_tools_until_it_answers() -> None:
    use = BetaToolUseBlock(type="tool_use", id="tu1", name="list_tenants", input={})
    client = _Client(
        [
            _message(
                [BetaTextBlock(type="text", text="Looking.", citations=None), use], "tool_use"
            ),
            _message(
                [BetaTextBlock(type="text", text="You have one.", citations=None)], "end_turn"
            ),
        ]
    )
    claude = Claude(get_settings(), client)  # type: ignore[arg-type]
    toolbox = FakeToolbox("good")
    streamed: list[str] = []

    async def on_text(chunk: str) -> None:
        streamed.append(chunk)

    user: BetaMessageParam = {"role": "user", "content": "My tenants?"}
    reply = await claude.reply([user], on_text, toolbox)

    assert toolbox.calls == [("list_tenants", {})]
    assert "".join(streamed) == reply.text == "Looking.\n\nYou have one."
    assert [turn["role"] for turn in reply.turns] == ["assistant", "user", "assistant"]
    results = reply.turns[1]["content"]
    assert isinstance(results, list)
    (result,) = results
    assert result == {
        "type": "tool_result",
        "tool_use_id": "tu1",
        "content": '[{"id": "t1"}]',
        "is_error": False,
    }
    # The second request carries the first exchange unchanged, and the tools both times.
    first, second = client.messages.requests
    assert second["messages"] == [user, *reply.turns[:2]]
    assert first["tools"] == second["tools"] == list(toolbox.tools)


async def test_the_last_round_may_not_call_tools(monkeypatch: Any) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "max_tool_rounds", 2)
    use = BetaToolUseBlock(type="tool_use", id="tu1", name="list_tenants", input={})
    client = _Client(
        [
            _message([use], "tool_use"),
            _message([BetaTextBlock(type="text", text="Enough.", citations=None)], "end_turn"),
        ]
    )
    claude = Claude(settings, client)  # type: ignore[arg-type]

    async def on_text(chunk: str) -> None:
        pass

    reply = await claude.reply([{"role": "user", "content": "Go"}], on_text, FakeToolbox("good"))
    assert reply.text == "Enough."
    last = client.messages.requests[-1]
    assert last["tool_choice"] == {"type": "none"}


# The MCP client, against a stand-in for the registry's endpoint.


def _endpoint(seen: list[dict[str, Any]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer good":
            return httpx.Response(401)
        message = json.loads(request.content)
        seen.append({"message": message, "headers": dict(request.headers)})
        if "id" not in message:
            return httpx.Response(202)
        method = message["method"]
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-11-25", "capabilities": {}}
        elif method == "tools/list":
            schema = {"type": "object", "properties": {"tenant_id": {"type": "string"}}}
            result = {"tools": [{"name": "get_tenant", "title": "A tenant", "inputSchema": schema}]}
        elif message["params"]["name"] == "get_tenant":
            result = {"content": [{"type": "text", "text": "HTTP 404: tenant_not_found"}]}
            result["isError"] = True
        else:
            error = {"code": -32602, "message": "unknown tool"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "error": error})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    return httpx.MockTransport(handle)


async def test_the_mcp_client_lists_and_calls_tools() -> None:
    seen: list[dict[str, Any]] = []
    http = httpx.AsyncClient(transport=_endpoint(seen))
    toolbox = await McpRegistry("http://registry/mcp", http).open("good")
    assert [m["message"]["method"] for m in seen] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert list(toolbox.tools) == [
        {
            "name": "get_tenant",
            "description": "A tenant",
            "input_schema": {"type": "object", "properties": {"tenant_id": {"type": "string"}}},
        }
    ]

    missing = await toolbox.call("get_tenant", {"tenant_id": "x"})
    assert missing.is_error and missing.text == "HTTP 404: tenant_not_found"
    assert seen[-1]["headers"]["mcp-protocol-version"] == "2025-11-25"
    unknown = await toolbox.call("drop_everything", {})
    assert unknown.is_error and unknown.text == "unknown tool"


async def test_the_mcp_client_reports_a_refused_token() -> None:
    http = httpx.AsyncClient(transport=_endpoint([]))
    try:
        await McpRegistry("http://registry/mcp", http).open("bad")
    except RegistryRefused:
        return
    raise AssertionError("the token was not refused")
