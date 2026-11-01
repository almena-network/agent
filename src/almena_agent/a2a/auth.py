"""Who may call the A2A bindings: every call carries a registry token the API accepts.

An ASGI middleware in front of both bindings (the agent card stays public).
It reads ``Authorization: Bearer``, asks the registry API whose account the
token signs in (remembering the answer for ``AGENT_TOKEN_CACHE_SECONDS``),
and hands the account on as the request's user: the a2a-sdk scopes tasks by
it, and the executor its conversations. Without an accepted token nothing
runs, Claude least of all.

It also limits the rate: ``AGENT_RATE_LIMIT`` calls per account and
``AGENT_AUTH_FAILURE_LIMIT`` refused tokens per client IP, each per
``AGENT_RATE_LIMIT_WINDOW_SECONDS``.

A2A leaves authentication to HTTP: the refusals are ``401`` with a
``WWW-Authenticate`` challenge, ``429`` with ``Retry-After``, or ``503`` when
the API cannot answer, each with the body of its binding's errors.
"""

import hashlib
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from starlette.authentication import BaseUser
from starlette.types import ASGIApp, Receive, Scope, Send

from almena_agent.a2a.card import JSONRPC_PATH, REST_PATH
from almena_agent.accounts import Accounts, AccountsUnavailable
from almena_agent.config import Settings
from almena_agent.metrics import METRICS

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

# Accepted tokens remembered (oldest dropped first), and keys a limiter counts
# before it drops those whose window is over.
MAX_TRACKED = 10_000

_ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"


class Caller(BaseUser):
    """The account behind an accepted token; its id is the a2a-sdk's owner scope."""

    def __init__(self, account: str) -> None:
        self.account = account

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.account

    @property
    def identity(self) -> str:
        return self.account


class RateLimiter:
    """At most ``limit`` hits per key in each fixed window of ``window`` seconds."""

    def __init__(self, limit: int, window: float, clock: Clock = time.monotonic) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        # key -> (window start, hits in it)
        self._hits: dict[str, tuple[float, int]] = {}

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may try again; 0 while it is under the limit."""
        now = self._clock()
        start, hits = self._hits.get(key, (now, 0))
        if now - start >= self._window or hits < self._limit:
            return 0
        return max(1, math.ceil(start + self._window - now))

    def hit(self, key: str) -> None:
        now = self._clock()
        start, hits = self._hits.get(key, (now, 0))
        if now - start >= self._window:
            start, hits = now, 0
        self._hits[key] = (start, hits + 1)
        # Keys whose window is over hold nothing; dropping them bounds the memory.
        if len(self._hits) > MAX_TRACKED:
            self._hits = {k: v for k, v in self._hits.items() if now - v[0] < self._window}


class A2AAuth:
    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        accounts: Accounts,
        clock: Clock = time.monotonic,
    ) -> None:
        self._app = app
        self._accounts = accounts
        self._clock = clock
        self._cache_seconds = settings.token_cache_seconds
        # sha256(token) -> (account, when it was accepted)
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        window = settings.rate_limit_window_seconds
        self._calls = RateLimiter(settings.rate_limit, window, clock)
        self._failures = RateLimiter(settings.auth_failure_limit, window, clock)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _guarded(scope["path"]):
            await self._app(scope, receive, send)
            return
        rest = scope["path"] != JSONRPC_PATH
        client = scope.get("client")
        ip = client[0] if client else "unknown"

        wait = self._failures.retry_after(ip)
        if wait:
            METRICS.limited("ip")
            await _refuse(send, rest, 429, "Too many refused tokens; try again later.", wait)
            return
        token = _bearer(scope)
        if token is None:
            METRICS.authenticated("missing")
            self._failures.hit(ip)
            await _refuse(send, rest, 401, "Send a registry token: Authorization: Bearer.")
            return
        try:
            account, cached = await self._identify(token)
        except AccountsUnavailable:
            METRICS.authenticated("unavailable")
            logger.exception("The registry could not check a token")
            await _refuse(send, rest, 503, "The registry cannot check the token; try again.", 5)
            return
        if account is None:
            METRICS.authenticated("refused")
            logger.info("Refused a registry token from %s", ip)
            self._failures.hit(ip)
            await _refuse(send, rest, 401, "The registry does not accept this token.")
            return
        METRICS.authenticated("cached" if cached else "accepted")
        wait = self._calls.retry_after(account)
        if wait:
            METRICS.limited("account")
            await _refuse(send, rest, 429, "Too many requests; try again later.", wait)
            return
        self._calls.hit(account)
        scope["user"] = Caller(account)
        await self._app(scope, receive, send)

    async def _identify(self, token: str) -> tuple[str | None, bool]:
        """The token's account (``None``: refused) and whether it came from the cache."""
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[1] < self._cache_seconds:
            return cached[0], True
        account = await self._accounts.identify(token)
        if account is None:
            self._cache.pop(key, None)
            return None, False
        self._cache[key] = (account, now)
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_TRACKED:
            self._cache.popitem(last=False)
        return account, False


def _guarded(path: str) -> bool:
    return path in (JSONRPC_PATH, REST_PATH) or path.startswith(f"{REST_PATH}/")


def _bearer(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return token.strip() or None if scheme.lower() == "bearer" else None
    return None


# HTTP status -> (gRPC-style status for REST, JSON-RPC code, ErrorInfo reason).
# -32000 is JSON-RPC's implementation-defined range, outside A2A's own codes;
# A2A files rate limits and unavailability under -32603 (internal error).
_ERRORS: dict[int, tuple[str, int, str]] = {
    401: ("UNAUTHENTICATED", -32000, "UNAUTHENTICATED"),
    429: ("RESOURCE_EXHAUSTED", -32603, "RATE_LIMIT_EXCEEDED"),
    503: ("UNAVAILABLE", -32603, "REGISTRY_UNAVAILABLE"),
}


async def _refuse(send: Send, rest: bool, status: int, message: str, retry_after: int = 0) -> None:
    grpc_status, code, reason = _ERRORS[status]
    details = [{"@type": _ERROR_INFO, "reason": reason, "domain": "a2a-protocol.org"}]
    body: dict[str, Any]
    if rest:
        body = {
            "error": {"code": status, "status": grpc_status, "message": message, "details": details}
        }
    else:
        # The request's id is unread here; JSON-RPC answers ``null`` then.
        body = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": code, "message": message, "data": details},
        }
    content = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(content)).encode()),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="almena-registry"'))
    if retry_after:
        headers.append((b"retry-after", str(retry_after).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": content})
