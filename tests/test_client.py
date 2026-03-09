"""Tests for MonzoClient in coral_bot.client."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coral_bot.client import (
    MAX_PAGES,
    MonzoClient,
    _validate_account_id,
    _validate_transaction_id,
)
from tests.factories import make_transaction

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestValidateAccountId:
    def test_valid(self):
        _validate_account_id("acc_abc123")

    def test_missing_prefix(self):
        with pytest.raises(ValueError):
            _validate_account_id("xxx_abc123")

    def test_empty(self):
        with pytest.raises(ValueError):
            _validate_account_id("")

    def test_special_chars(self):
        with pytest.raises(ValueError):
            _validate_account_id("acc_ab-cd")

    def test_injection_attempt(self):
        with pytest.raises(ValueError):
            _validate_account_id("; DROP TABLE--")


class TestValidateTransactionId:
    def test_valid(self):
        _validate_transaction_id("tx_abc123")

    def test_invalid(self):
        with pytest.raises(ValueError):
            _validate_transaction_id("notvalid")

    def test_empty(self):
        with pytest.raises(ValueError):
            _validate_transaction_id("")


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


class TestClientInit:
    def test_with_explicit_token(self):
        client = MonzoClient(access_token="test_token")
        assert client._direct_token == "test_token"

    def test_with_token_manager(self):
        from coral_bot.tokens import TokenManager

        tm = TokenManager.__new__(TokenManager)
        tm._tokens = {"access_token": "managed_token"}
        client = MonzoClient(token_manager=tm)
        assert client._token_manager is tm

    def test_no_token_creates_client(self):
        """Client can be constructed without a token; error occurs on first request."""
        client = MonzoClient()
        assert client._direct_token is None
        assert client._token_manager is None


# ---------------------------------------------------------------------------
# _request error handling
# ---------------------------------------------------------------------------


class TestRequest:
    @pytest.fixture
    def client(self):
        return MonzoClient(access_token="test_token")

    async def test_success(self, client):
        mock_response = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("GET", "https://api.monzo.com/test"),
        )
        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=mock_response)

        result = await client._request("GET", "/test")
        assert result == {"ok": True}

    async def test_http_error_with_json_body(self, client):
        mock_response = httpx.Response(
            401,
            json={"message": "Unauthorized"},
            request=httpx.Request("GET", "https://api.monzo.com/test"),
        )
        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=mock_response)
        # httpx.Response.raise_for_status() needs to actually raise
        # so we use a real response that will raise
        mock_response.request = httpx.Request("GET", "https://api.monzo.com/test")

        result = await client._request("GET", "/test")
        assert "error" in result

    async def test_network_error(self, client):
        client._client = AsyncMock()
        client._client.request = AsyncMock(side_effect=httpx.RequestError("Connection refused"))

        result = await client._request("GET", "/test")
        assert result == {"error": "Request failed — check network connection"}


# ---------------------------------------------------------------------------
# list_transactions parameter building
# ---------------------------------------------------------------------------


class TestListTransactions:
    @pytest.fixture
    def client(self):
        c = MonzoClient(access_token="test_token")
        mock_request = AsyncMock(return_value={"transactions": []})
        with patch.object(c, "_request", mock_request):
            yield c

    async def test_params_forwarded(self, client):
        await client.list_transactions(
            "acc_abc123", since="2026-03-01T00:00:00Z", before="2026-03-31T00:00:00Z", limit=50
        )
        call_args = client._request.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        assert params["account_id"] == "acc_abc123"
        assert params["since"] == "2026-03-01T00:00:00Z"
        assert params["before"] == "2026-03-31T00:00:00Z"
        assert params["limit"] == 50
        assert params["expand[]"] == "merchant"

    async def test_limit_clamped_high(self, client):
        await client.list_transactions("acc_abc123", limit=999)
        call_args = client._request.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        assert params["limit"] == 100

    async def test_limit_clamped_low(self, client):
        await client.list_transactions("acc_abc123", limit=0)
        call_args = client._request.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        assert params["limit"] == 1

    async def test_optional_params_omitted(self, client):
        await client.list_transactions("acc_abc123")
        call_args = client._request.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        assert "since" not in params
        assert "before" not in params


# ---------------------------------------------------------------------------
# list_all_transactions pagination
# ---------------------------------------------------------------------------


class TestPagination:
    @pytest.fixture
    def client(self):
        c = MonzoClient(access_token="test_token")
        return c

    async def test_single_page(self, client):
        txs = [make_transaction(id=f"tx_{i}") for i in range(50)]
        client.list_transactions = AsyncMock(return_value={"transactions": txs})

        result = await client.list_all_transactions("acc_abc123")
        assert len(result["transactions"]) == 50
        assert client.list_transactions.call_count == 1

    async def test_multiple_pages(self, client):
        page1 = [make_transaction(id=f"tx_a{i}") for i in range(100)]
        page2 = [make_transaction(id=f"tx_b{i}") for i in range(50)]
        client.list_transactions = AsyncMock(
            side_effect=[{"transactions": page1}, {"transactions": page2}]
        )

        result = await client.list_all_transactions("acc_abc123")
        assert len(result["transactions"]) == 150
        assert client.list_transactions.call_count == 2
        # Second call should use cursor from last tx in first page
        second_call = client.list_transactions.call_args_list[1]
        assert second_call.kwargs.get("since") == "tx_a99"

    async def test_stops_at_max_pages(self, client):
        full_page = [make_transaction(id=f"tx_{i}") for i in range(100)]
        client.list_transactions = AsyncMock(return_value={"transactions": full_page})

        result = await client.list_all_transactions("acc_abc123")
        assert len(result["transactions"]) == 100 * MAX_PAGES
        assert client.list_transactions.call_count == MAX_PAGES

    async def test_error_on_first_page(self, client):
        client.list_transactions = AsyncMock(return_value={"error": "Unauthorized"})

        result = await client.list_all_transactions("acc_abc123")
        assert result == {"error": "Unauthorized"}

    async def test_error_on_second_page(self, client):
        page1 = [make_transaction(id=f"tx_{i}") for i in range(100)]
        client.list_transactions = AsyncMock(
            side_effect=[{"transactions": page1}, {"error": "Rate limited"}]
        )

        result = await client.list_all_transactions("acc_abc123")
        assert result == {"error": "Rate limited"}

    async def test_empty_first_page(self, client):
        client.list_transactions = AsyncMock(return_value={"transactions": []})

        result = await client.list_all_transactions("acc_abc123")
        assert result == {"transactions": []}


# ---------------------------------------------------------------------------
# annotate_transaction
# ---------------------------------------------------------------------------


class TestAnnotateTransaction:
    async def test_sends_correct_data(self):
        client = MonzoClient(access_token="test_token")
        mock_request = AsyncMock(return_value={"transaction": {}})
        with patch.object(client, "_request", mock_request):
            await client.annotate_transaction("tx_abc123", "notes", "hello")
            mock_request.assert_called_once_with(
                "PATCH", "/transactions/tx_abc123", data={"metadata[notes]": "hello"}
            )

    async def test_validates_transaction_id(self):
        client = MonzoClient(access_token="test_token")
        with pytest.raises(ValueError):
            await client.annotate_transaction("invalid", "notes", "hello")
