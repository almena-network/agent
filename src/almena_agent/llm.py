"""The model behind the agent: Claude, through the Anthropic SDK.

``Model`` is what the A2A executor needs from it, so tests can put a fake in
its place (``get_model`` is the dependency to override).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from anthropic import AnthropicError, AsyncAnthropic, Omit, omit
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaToolChoiceParam,
    BetaToolResultBlockParam,
    BetaToolUseBlock,
)

from almena_agent.config import Settings, get_settings
from almena_agent.metrics import METRICS
from almena_agent.prompts import SYSTEM_PROMPT
from almena_agent.registry import Toolbox

# Server-side fallback: when the model declines for policy reasons, the API
# retries the same request on its default fallback model within the call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

OnText = Callable[[str], Awaitable[None]]


class ModelUnavailable(Exception):
    """The model could not answer: credentials, network, rate limits, API errors."""


@dataclass(frozen=True)
class Reply:
    """A finished answer to the last user turn."""

    # The turns that answer it, in order: Claude's (every block as returned,
    # thinking included) and, between them, the tool results it was given.
    # They go back unchanged on the next message of the conversation.
    turns: Sequence[BetaMessageParam]
    text: str
    # The model (and its fallback, if any) declined the request.
    refused: bool


class Model(Protocol):
    async def reply(
        self,
        messages: Sequence[BetaMessageParam],
        on_text: OnText,
        toolbox: Toolbox | None = None,
    ) -> Reply:
        """Answers the conversation's last user turn, streaming its text to ``on_text``,
        using the toolbox's tools as it needs them.

        Raises ``ModelUnavailable`` when the model cannot be reached.
        """
        ...


class Claude:
    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        self._settings = settings
        # Made on first use, so that the app starts without credentials.
        self._client = client
        self._system = settings.system_prompt or SYSTEM_PROMPT

    async def reply(
        self,
        messages: Sequence[BetaMessageParam],
        on_text: OnText,
        toolbox: Toolbox | None = None,
    ) -> Reply:
        try:
            return await self._reply(messages, on_text, toolbox)
        except AnthropicError as error:
            # The SDK has already retried what is worth retrying (429, 5xx, network).
            raise ModelUnavailable(str(error)) from error

    async def _reply(
        self, messages: Sequence[BetaMessageParam], on_text: OnText, toolbox: Toolbox | None
    ) -> Reply:
        conversation = list(messages)
        turns: list[BetaMessageParam] = []
        texts: list[str] = []
        rounds = self._settings.max_tool_rounds
        for round_ in range(1, rounds + 1):
            # The last round may not call tools: it has to answer with what it has.
            last = round_ == rounds
            message = await self._stream(
                conversation, on_text, toolbox, separate=bool(texts), last=last
            )
            assistant: BetaMessageParam = {"role": "assistant", "content": message.content}
            turns.append(assistant)
            conversation.append(assistant)
            text = "".join(block.text for block in message.content if block.type == "text")
            if text:
                texts.append(text)
            if message.stop_reason == "refusal":
                return Reply(turns=turns, text="\n\n".join(texts), refused=True)
            uses = [block for block in message.content if block.type == "tool_use"]
            if message.stop_reason != "tool_use" or toolbox is None or not uses:
                break
            results: BetaMessageParam = {
                "role": "user",
                "content": await asyncio.gather(*(_run(toolbox, use) for use in uses)),
            }
            turns.append(results)
            conversation.append(results)
        return Reply(turns=turns, text="\n\n".join(texts), refused=False)

    async def _stream(
        self,
        messages: list[BetaMessageParam],
        on_text: OnText,
        toolbox: Toolbox | None,
        *,
        separate: bool,
        last: bool,
    ) -> BetaMessage:
        started = time.perf_counter()
        try:
            message = await self._request(messages, on_text, toolbox, separate=separate, last=last)
        except AnthropicError:
            METRICS.claude_call("error", time.perf_counter() - started)
            raise
        refused = message.stop_reason == "refusal"
        METRICS.claude_call("refused" if refused else "ok", time.perf_counter() - started)
        usage = message.usage
        METRICS.claude_used("input", usage.input_tokens)
        METRICS.claude_used("output", usage.output_tokens)
        METRICS.claude_used("cache_read", usage.cache_read_input_tokens)
        METRICS.claude_used("cache_creation", usage.cache_creation_input_tokens)
        return message

    async def _request(
        self,
        messages: list[BetaMessageParam],
        on_text: OnText,
        toolbox: Toolbox | None,
        *,
        separate: bool,
        last: bool,
    ) -> BetaMessage:
        if self._client is None:
            self._client = AsyncAnthropic()
        tool_choice: BetaToolChoiceParam | Omit = omit
        if toolbox is not None and last:
            tool_choice = {"type": "none"}
        async with self._client.beta.messages.stream(
            model=self._settings.model,
            max_tokens=self._settings.max_tokens,
            system=self._system,
            messages=messages,
            tools=list(toolbox.tools) if toolbox is not None else omit,
            tool_choice=tool_choice,
            thinking={"type": "adaptive"},
            output_config={"effort": self._settings.effort},
            # Caches the tools, the system prompt and the conversation so far.
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            first = True
            async for text in stream.text_stream:
                # Text from an earlier round of the same reply ends a paragraph.
                if first and separate:
                    text = f"\n\n{text}"
                first = False
                await on_text(text)
            return await stream.get_final_message()


async def _run(toolbox: Toolbox, use: BetaToolUseBlock) -> BetaToolResultBlockParam:
    arguments: Any = use.input
    if not isinstance(arguments, dict):
        outcome_text, is_error = "The tool's input must be a JSON object.", True
    else:
        outcome = await toolbox.call(use.name, arguments)
        outcome_text, is_error = outcome.text, outcome.is_error
    return {
        "type": "tool_result",
        "tool_use_id": use.id,
        "content": outcome_text,
        "is_error": is_error,
    }


@lru_cache
def get_model() -> Model:
    return Claude(get_settings())
