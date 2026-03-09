"""Async client for the Monzo banking API."""

import logging
import re
from typing import Any

import httpx

from coral_bot.tokens import TokenManager

logger = logging.getLogger(__name__)

BASE_URL = "https://api.monzo.com"

# Max pages to fetch during pagination (5000 transactions)
MAX_PAGES = 50

_ACCOUNT_ID_RE = re.compile(r"^acc_[a-zA-Z0-9]+$")
_TRANSACTION_ID_RE = re.compile(r"^tx_[a-zA-Z0-9]+$")


def _validate_account_id(account_id: str) -> None:
    if not _ACCOUNT_ID_RE.match(account_id):
        raise ValueError(f"Invalid account_id format: {account_id!r}")


def _validate_transaction_id(transaction_id: str) -> None:
    if not _TRANSACTION_ID_RE.match(transaction_id):
        raise ValueError(f"Invalid transaction_id format: {transaction_id!r}")


class MonzoClient:
    """Async wrapper around the Monzo REST API.

    Supports two modes:
        - Direct token: pass access_token for simple usage (tests, CLI)
        - TokenManager: pass token_manager for auto-refresh and persistence
    """

    def __init__(
        self,
        access_token: str | None = None,
        token_manager: TokenManager | None = None,
    ):
        self._token_manager = token_manager
        self._direct_token = access_token
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=30.0,
        )

    async def _get_headers(self) -> dict[str, str]:
        """Get authorization headers, using token manager if available."""
        if self._token_manager:
            token = await self._token_manager.get_valid_token()
        elif self._direct_token:
            token = self._direct_token
        else:
            raise ValueError(
                "No access token provided. Set MONZO_ACCESS_TOKEN or pass access_token."
            )
        return {"Authorization": f"Bearer {token}"}

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the Monzo API.

        Returns the parsed JSON response, or a dict with an "error" key on failure.
        Automatically retries once on 401 if a token manager is available.
        """
        try:
            headers = await self._get_headers()
            response = await self._client.request(
                method,
                endpoint,
                params=params,
                data=data,
                headers=headers,
            )

            # Auto-retry on 401/403 with refreshed token.
            # Monzo returns 403 (not 401) when SCA has expired.
            if response.status_code in (401, 403) and self._token_manager:
                refreshed = await self._token_manager.handle_auth_error()
                if refreshed:
                    headers = await self._get_headers()
                    response = await self._client.request(
                        method,
                        endpoint,
                        params=params,
                        data=data,
                        headers=headers,
                    )

            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("Monzo API error: %s %s -> %s", method, endpoint, e.response.status_code)
            if e.response.status_code == 403:
                return {
                    "error": (
                        "Monzo denied access (403 Forbidden). This usually means the "
                        "5-minute Strong Customer Authentication window has expired. "
                        "Please re-authenticate to regain access."
                    )
                }
            try:
                body = e.response.json()
                return {"error": body.get("message", f"HTTP {e.response.status_code}")}
            except Exception:
                return {"error": f"HTTP {e.response.status_code}"}
        except httpx.RequestError as e:
            logger.error("Request failed: %s %s -> %s", method, endpoint, e)
            return {"error": "Request failed — check network connection"}

    async def whoami(self) -> dict[str, Any]:
        return await self._request("GET", "/ping/whoami")

    async def list_accounts(self) -> dict[str, Any]:
        return await self._request("GET", "/accounts")

    async def get_balance(self, account_id: str) -> dict[str, Any]:
        _validate_account_id(account_id)
        return await self._request("GET", "/balance", params={"account_id": account_id})

    async def list_transactions(
        self,
        account_id: str,
        since: str | None = None,
        before: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        _validate_account_id(account_id)
        limit = max(1, min(limit, 100))
        params: dict[str, Any] = {
            "account_id": account_id,
            "limit": limit,
            "expand[]": "merchant",
        }
        if since:
            params["since"] = since
        if before:
            params["before"] = before
        return await self._request("GET", "/transactions", params=params)

    async def list_all_transactions(
        self,
        account_id: str,
        since: str | None = None,
        before: str | None = None,
    ) -> dict[str, Any]:
        """Fetch all transactions for a period, paginating through 100 at a time.

        Stops after MAX_PAGES pages to prevent runaway requests.
        """
        _validate_account_id(account_id)
        all_transactions: list[dict[str, Any]] = []
        cursor = since

        for _ in range(MAX_PAGES):
            result = await self.list_transactions(
                account_id, since=cursor, before=before, limit=100
            )
            if "error" in result:
                return result

            batch = result.get("transactions", [])
            if not batch:
                break

            all_transactions.extend(batch)

            # Use the last transaction's ID as cursor for the next page
            cursor = batch[-1]["id"]

            # If we got fewer than 100, we've reached the end
            if len(batch) < 100:
                break

        return {"transactions": all_transactions}

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        _validate_transaction_id(transaction_id)
        return await self._request(
            "GET",
            f"/transactions/{transaction_id}",
            params={"expand[]": "merchant"},
        )

    async def list_pots(self, account_id: str) -> dict[str, Any]:
        _validate_account_id(account_id)
        return await self._request("GET", "/pots", params={"current_account_id": account_id})

    async def annotate_transaction(
        self,
        transaction_id: str,
        key: str,
        value: str,
    ) -> dict[str, Any]:
        _validate_transaction_id(transaction_id)
        data = {f"metadata[{key}]": value}
        return await self._request("PATCH", f"/transactions/{transaction_id}", data=data)
