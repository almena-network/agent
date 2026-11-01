"""Who a registry token belongs to, asked of the registry API.

Every A2A call carries the caller's registry token; the agent asks the API's
signed-in-account endpoint (``AGENT_REGISTRY_ACCOUNT_URL``) whose it is, and
the account's id becomes the caller's identity: its tasks, conversations and
rate limit are that account's.
"""

from typing import Protocol

import httpx


class AccountsUnavailable(Exception):
    """The registry API could not be reached or answered nonsense."""


class Accounts(Protocol):
    async def identify(self, token: str) -> str | None:
        """The id of the account ``token`` signs in, or ``None`` if the API refuses it.

        Raises ``AccountsUnavailable`` when the API cannot be reached.
        """
        ...


class ApiAccounts:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def identify(self, token: str) -> str | None:
        try:
            response = await self._client.get(
                self._url, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as error:
            raise AccountsUnavailable(str(error)) from error
        if response.status_code in (401, 403):
            return None
        if response.status_code != 200:
            raise AccountsUnavailable(f"HTTP {response.status_code}")
        try:
            account = response.json()
        except ValueError as error:
            raise AccountsUnavailable("not JSON") from error
        if not isinstance(account, dict) or not account.get("id"):
            raise AccountsUnavailable("no account id")
        return str(account["id"])
