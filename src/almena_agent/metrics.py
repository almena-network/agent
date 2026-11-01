"""Operational metrics in the Prometheus text format, served on their own
listener (``AGENT_METRICS_ADDR``) so they never go out through the public
proxy, as the mediator's: the public app has no ``/metrics``. Unset: off.

They only count: no tokens, accounts, contexts or task ids in any label.
``METRICS`` is the process's; ``serve`` starts the listener (a thread of
``prometheus_client``'s), which the app's lifespan opens and closes.
"""

import logging
from collections.abc import Callable
from typing import Literal

from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

logger = logging.getLogger(__name__)

AuthResult = Literal["accepted", "cached", "refused", "missing", "unavailable"]
RateLimit = Literal["account", "ip"]
ClaudeOutcome = Literal["ok", "refused", "error"]
Held = Literal["conversation", "task"]
EvictionReason = Literal["idle", "capacity"]

# A reply round of Claude's can take minutes with thinking and long answers.
CLAUDE_BUCKETS = (0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600)


class Metrics:
    def __init__(self) -> None:
        self.registry = registry = CollectorRegistry()
        # The process's own: memory, CPU, open files, the interpreter, GC.
        for collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
            registry.register(collector)
        self.requests = Counter(
            "agent_a2a_requests",
            "A2A requests that reached the bindings' handler, by method and outcome "
            "(ok, or the A2A error's name).",
            ["method", "outcome"],
            registry=registry,
        )
        self.auth = Counter(
            "agent_auth",
            "Registry tokens checked on A2A calls: accepted (asked the API), cached "
            "(accepted from memory), refused, missing, or unavailable (the API could not answer).",
            ["result"],
            registry=registry,
        )
        self.rate_limited = Counter(
            "agent_rate_limited",
            "A2A calls refused with 429: over the per-account limit or the per-IP "
            "limit on refused tokens.",
            ["limit"],
            registry=registry,
        )
        self.claude_calls = Counter(
            "agent_claude_calls",
            "Requests to Claude (one per round of a reply), by outcome.",
            ["outcome"],
            registry=registry,
        )
        self.claude_seconds = Histogram(
            "agent_claude_call_seconds",
            "How long each request to Claude took, streaming included.",
            buckets=CLAUDE_BUCKETS,
            registry=registry,
        )
        self.claude_tokens = Counter(
            "agent_claude_tokens",
            "Tokens Claude reported: input, output, cache_read and cache_creation.",
            ["kind"],
            registry=registry,
        )
        self.held = Gauge(
            "agent_held",
            "Conversations and tasks currently held in memory.",
            ["kind"],
            registry=registry,
        )
        self.evictions = Counter(
            "agent_evictions",
            "Conversations and finished tasks dropped from memory, idle or over the cap.",
            ["kind", "reason"],
            registry=registry,
        )

    def request(self, method: str, outcome: str) -> None:
        self.requests.labels(method, outcome).inc()

    def authenticated(self, result: AuthResult) -> None:
        self.auth.labels(result).inc()

    def limited(self, limit: RateLimit) -> None:
        self.rate_limited.labels(limit).inc()

    def claude_call(self, outcome: ClaudeOutcome, seconds: float) -> None:
        self.claude_calls.labels(outcome).inc()
        self.claude_seconds.observe(seconds)

    def claude_used(self, kind: str, tokens: int | None) -> None:
        if tokens:
            self.claude_tokens.labels(kind).inc(tokens)

    def holding(self, kind: Held, count: int) -> None:
        self.held.labels(kind).set(count)

    def evicted(self, kind: Held, reason: EvictionReason) -> None:
        self.evictions.labels(kind, reason).inc()


# The process's metrics.
METRICS = Metrics()


def parse_addr(addr: str) -> tuple[str, int]:
    """``host:port`` (``[::1]:9090`` for IPv6) as the listener's host and port."""
    host, sep, port = addr.strip().rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ValueError(f"expected host:port, got {addr!r}")
    return host.removeprefix("[").removesuffix("]"), int(port)


def serve(addr: str, metrics: Metrics = METRICS) -> tuple[int, Callable[[], None]]:
    """Serves ``GET /metrics`` on ``addr`` in a background thread.

    Returns the port it listens on (``addr``'s, or the one picked for port 0)
    and what stops it.
    """
    host, port = parse_addr(addr)
    server, thread = start_http_server(port, host, registry=metrics.registry)
    logger.info("Metrics listening on %s:%d", host, server.server_port)

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return server.server_port, stop
