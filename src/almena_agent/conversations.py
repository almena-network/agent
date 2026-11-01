"""Conversation history per A2A context, in memory.

Each context's turns are what Claude gets back on the next message. History is
append-only: Claude's thinking blocks stay valid only while the turns before
them are sent back unchanged, so old turns are never trimmed; a context that
reaches ``AGENT_HISTORY_TURNS`` is full and the peer starts a new one.
A context belongs to the account that started it (the caller's registry
account): its history can hold what the registry told them, so nobody else
may continue it.
Whole contexts are dropped instead, never their turns: once idle for
``AGENT_CONVERSATION_IDLE_MINUTES``, and the least recently used beyond
``AGENT_MAX_CONVERSATIONS``; a message to a dropped context starts it afresh.
Everything is lost on restart and is not shared between replicas.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from anthropic.types.beta import BetaMessageParam

from almena_agent.metrics import METRICS


@dataclass
class _Context:
    owner: str
    used_at: float
    turns: int = 0
    history: list[BetaMessageParam] = field(default_factory=list)


class Conversations:
    def __init__(
        self,
        max_turns: int,
        idle_seconds: float = 3600,
        max_contexts: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_turns = max_turns
        self._idle_seconds = idle_seconds
        self._max_contexts = max_contexts
        self._clock = clock
        # Least recently used first.
        self._contexts: OrderedDict[str, _Context] = OrderedDict()

    def __len__(self) -> int:
        self._evict()
        return len(self._contexts)

    def belongs_to(self, context_id: str, owner: str) -> bool:
        """Whether ``owner`` may continue the context (anyone may start a new one)."""
        context = self._get(context_id)
        return context is None or context.owner == owner

    def history(self, context_id: str) -> list[BetaMessageParam]:
        context = self._get(context_id)
        return list(context.history) if context else []

    def is_full(self, context_id: str) -> bool:
        context = self._get(context_id)
        return context is not None and context.turns >= self._max_turns

    def record(self, context_id: str, owner: str, turn: Sequence[BetaMessageParam]) -> None:
        """Appends a finished exchange: the user's message and the turns answering it."""
        context = self._get(context_id)
        if context is None:
            context = self._contexts[context_id] = _Context(owner=owner, used_at=self._clock())
        context.turns += 1
        context.history.extend(turn)
        self._evict()
        METRICS.holding("conversation", len(self._contexts))

    def _get(self, context_id: str) -> _Context | None:
        self._evict()
        context = self._contexts.get(context_id)
        if context is not None:
            context.used_at = self._clock()
            self._contexts.move_to_end(context_id)
        return context

    def _evict(self) -> None:
        now = self._clock()
        dropped = False
        while self._contexts:
            context_id, context = next(iter(self._contexts.items()))
            idle = now - context.used_at >= self._idle_seconds
            if not idle and len(self._contexts) <= self._max_contexts:
                break
            del self._contexts[context_id]
            METRICS.evicted("conversation", "idle" if idle else "capacity")
            dropped = True
        if dropped:
            METRICS.holding("conversation", len(self._contexts))
