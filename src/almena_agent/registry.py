"""The Almena registry's tools, reached through the API's MCP endpoint.

The agent is an MCP client of the registry API (``AGENT_REGISTRY_MCP_URL``):
for each message it opens a toolbox with the caller's own registry token, so
Claude acts as that caller's account and can do only what that account may do.
Every call carries a token (``a2a/auth.py`` turns away the rest); with
``AGENT_REGISTRY_MCP_URL`` empty there is no toolbox, and the agent only converses.

The endpoint is stateless and answers each JSON-RPC message as JSON, so every
message is one plain POST; ``initialize`` is still sent first, as MCP asks.
"""

import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from anthropic.types.beta import BetaToolParam

from almena_agent import __version__

PROTOCOL_VERSION = "2025-11-25"


class RegistryUnavailable(Exception):
    """The registry's MCP endpoint could not be reached or answered nonsense."""


class RegistryRefused(Exception):
    """The registry did not accept the caller's token (HTTP 401)."""


@dataclass(frozen=True)
class ToolOutcome:
    text: str
    is_error: bool


class Toolbox(Protocol):
    """The tools Claude may use for one message, already bound to its caller."""

    @property
    def tools(self) -> Sequence[BetaToolParam]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Runs a tool. Its failures, the registry's errors included, are outcomes."""
        ...


class Registry(Protocol):
    async def open(self, token: str) -> Toolbox:
        """A toolbox acting with ``token``.

        Raises ``RegistryRefused`` for a token the registry does not accept,
        ``RegistryUnavailable`` when it cannot be reached.
        """
        ...


class McpRegistry:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def open(self, token: str) -> Toolbox:
        session = _Session(self._client, self._url, token)
        await session.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "almena-agent", "version": __version__},
            },
        )
        await session.notify("notifications/initialized")
        listed = await session.request("tools/list", {})
        tools: list[BetaToolParam] = [
            {
                "name": tool["name"],
                "description": tool.get("description") or tool.get("title") or tool["name"],
                "input_schema": tool["inputSchema"],
            }
            for tool in listed.get("tools", [])
        ]
        return _McpToolbox(session, tools)


class _Session:
    def __init__(self, client: httpx.AsyncClient, url: str, token: str) -> None:
        self._client = client
        self._url = url
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        self._ids = itertools.count(1)

    async def _post(self, message: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._client.post(self._url, json=message, headers=self._headers)
        except httpx.HTTPError as error:
            raise RegistryUnavailable(str(error)) from error
        if response.status_code == 401:
            raise RegistryRefused()
        return response

    async def notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        response = await self._post(message)
        try:
            body = response.json()
        except ValueError as error:
            raise RegistryUnavailable(f"HTTP {response.status_code}, not JSON") from error
        if not isinstance(body, dict):
            raise RegistryUnavailable("not a JSON-RPC answer")
        if "error" in body:
            raise _RpcError(str(body["error"].get("message", body["error"])))
        result = body.get("result")
        if not isinstance(result, dict):
            raise RegistryUnavailable("not a JSON-RPC answer")
        if method == "initialize":
            self._headers["MCP-Protocol-Version"] = str(result.get("protocolVersion"))
        return result


class _RpcError(RegistryUnavailable):
    """The endpoint answered with a JSON-RPC error (bad arguments, unknown tool)."""


class _McpToolbox:
    def __init__(self, session: _Session, tools: Sequence[BetaToolParam]) -> None:
        self._session = session
        self._tools = tools

    @property
    def tools(self) -> Sequence[BetaToolParam]:
        return self._tools

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            result = await self._session.request(
                "tools/call", {"name": name, "arguments": arguments}
            )
        except _RpcError as error:
            return ToolOutcome(text=str(error), is_error=True)
        except RegistryUnavailable:
            return ToolOutcome(text="The registry is unavailable; try again.", is_error=True)
        except RegistryRefused:
            return ToolOutcome(text="The registry no longer accepts the token.", is_error=True)
        texts = [
            block["text"] for block in result.get("content", []) if block.get("type") == "text"
        ]
        text = "\n".join(texts) if texts else json.dumps(result.get("structuredContent", {}))
        return ToolOutcome(text=text, is_error=bool(result.get("isError")))
