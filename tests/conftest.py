import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any, ClassVar

import pytest
from anthropic.types.beta import BetaMessageParam, BetaTextBlock, BetaToolParam
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from almena_agent.accounts import AccountsUnavailable
from almena_agent.llm import ModelUnavailable, OnText, Reply
from almena_agent.main import create_app
from almena_agent.registry import RegistryRefused, RegistryUnavailable, Toolbox, ToolOutcome


class FakeModel:
    """Answers in two chunks, and keeps what it was sent and the toolbox it had."""

    def __init__(self) -> None:
        self.calls: list[list[BetaMessageParam]] = []
        self.toolboxes: list[Toolbox | None] = []
        self.refuse = False
        self.unavailable = False
        # Set, the reply waits for it: a task that stays working.
        self.hold: asyncio.Event | None = None

    async def reply(
        self,
        messages: Sequence[BetaMessageParam],
        on_text: OnText,
        toolbox: Toolbox | None = None,
    ) -> Reply:
        self.calls.append(list(messages))
        self.toolboxes.append(toolbox)
        if self.unavailable:
            raise ModelUnavailable("no credentials at /home/someone/.config")
        if self.refuse:
            return Reply(turns=[], text="", refused=True)
        if self.hold is not None:
            await self.hold.wait()
        await on_text("Hello ")
        await on_text("from Almena.")
        text = "Hello from Almena."
        content = [BetaTextBlock(type="text", text=text, citations=None)]
        return Reply(turns=[{"role": "assistant", "content": content}], text=text, refused=False)


class FakeToolbox:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def tools(self) -> Sequence[BetaToolParam]:
        return [{"name": "list_tenants", "input_schema": {"type": "object"}}]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, arguments))
        return ToolOutcome(text='[{"id": "t1"}]', is_error=False)


class FakeAccounts:
    """Knows ``good`` and ``other`` (two accounts) and ``revoked``, which the
    registry's MCP no longer accepts; ``down`` makes it unreachable."""

    TOKENS: ClassVar[dict[str, str]] = {
        "good": "account-1",
        "other": "account-2",
        "revoked": "account-3",
    }

    def __init__(self) -> None:
        self.down = False
        self.asked: list[str] = []

    async def identify(self, token: str) -> str | None:
        self.asked.append(token)
        if self.down:
            raise AccountsUnavailable("connection refused")
        return self.TOKENS.get(token)


class FakeRegistry:
    """Accepts the tokens ``good`` and ``other``; ``down`` makes it unreachable."""

    def __init__(self) -> None:
        self.down = False
        self.opened: list[str] = []

    async def open(self, token: str) -> Toolbox:
        self.opened.append(token)
        if self.down:
            raise RegistryUnavailable("connection refused")
        if token not in ("good", "other"):
            raise RegistryRefused()
        return FakeToolbox(token)


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def accounts() -> FakeAccounts:
    return FakeAccounts()


@pytest.fixture
def app(model: FakeModel, registry: FakeRegistry, accounts: FakeAccounts) -> FastAPI:
    return create_app(model, registry, accounts)


def connect(app: FastAPI, token: str | None = "good") -> AsyncClient:
    """A client of the app sending ``token`` on every request (``None``: none)."""
    headers = {"A2A-Version": "1.0"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """The account behind the token ``good``."""
    async with connect(app) as client:
        yield client


@pytest.fixture
async def other(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Another account (the token ``other``) on the same app."""
    async with connect(app, "other") as client:
        yield client


@pytest.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """No token at all."""
    async with connect(app, None) as client:
        yield client
