"""Tests for account resolution logic in coral_bot.server."""

from unittest.mock import AsyncMock, patch

import pytest

from coral_bot.server import _get_accounts, _resolve_account
from tests.factories import make_account

# Sample accounts matching the real Monzo account types
ACCOUNTS = [
    make_account(id="acc_retail", type="uk_retail", description="user_00009SoS"),
    make_account(id="acc_flex", type="uk_monzo_flex", description="monzoflex_0000AJWn"),
    make_account(id="acc_rewards", type="uk_rewards", description="rewardsoptin_0000"),
    make_account(id="acc_loan", type="uk_loan", description="loan_0000AVqS"),
    make_account(id="acc_joint", type="uk_retail_joint", description="Joint account", closed=True),
    make_account(id="acc_young", type="uk_retail_young", description=""),
]


@pytest.fixture(autouse=True)
def _clear_account_cache():
    """Reset the per-user account cache before each test."""
    import coral_bot.server as srv

    srv._account_caches.clear()
    yield
    srv._account_caches.clear()


@pytest.fixture
def mock_accounts():
    """Patch _get_client so list_accounts returns our test accounts."""
    mock = AsyncMock()
    mock.list_accounts.return_value = {"accounts": ACCOUNTS}
    with patch("coral_bot.server._get_client", return_value=mock):
        yield mock


class TestResolveAccountById:
    async def test_explicit_id_returned_as_is(self, mock_accounts):
        result = await _resolve_account(account_id="acc_whatever")
        assert result == "acc_whatever"

    async def test_explicit_id_skips_api_call(self, mock_accounts):
        await _resolve_account(account_id="acc_whatever")
        mock_accounts.list_accounts.assert_not_called()


class TestResolveAccountByName:
    async def test_flex(self, mock_accounts):
        result = await _resolve_account(account_name="flex")
        assert result == "acc_flex"

    async def test_loan(self, mock_accounts):
        result = await _resolve_account(account_name="loan")
        assert result == "acc_loan"

    async def test_rewards(self, mock_accounts):
        result = await _resolve_account(account_name="rewards")
        assert result == "acc_rewards"

    async def test_retail(self, mock_accounts):
        result = await _resolve_account(account_name="retail")
        assert result == "acc_retail"

    async def test_young(self, mock_accounts):
        result = await _resolve_account(account_name="young")
        assert result == "acc_young"

    async def test_case_insensitive(self, mock_accounts):
        result = await _resolve_account(account_name="FLEX")
        assert result == "acc_flex"

    async def test_whitespace_stripped(self, mock_accounts):
        result = await _resolve_account(account_name="  flex  ")
        assert result == "acc_flex"

    async def test_closed_account_matched_when_no_open_match(self, mock_accounts):
        result = await _resolve_account(account_name="joint")
        assert result == "acc_joint"

    async def test_no_match_raises(self, mock_accounts):
        with pytest.raises(ValueError, match="No account matching 'savings'"):
            await _resolve_account(account_name="savings")

    async def test_error_lists_available_accounts(self, mock_accounts):
        with pytest.raises(ValueError, match="Available accounts:"):
            await _resolve_account(account_name="nonexistent")


class TestResolveAccountDefault:
    async def test_defaults_to_uk_retail(self, mock_accounts):
        result = await _resolve_account()
        assert result == "acc_retail"

    async def test_defaults_to_first_open_if_no_retail(self):
        accounts = [
            make_account(id="acc_flex", type="uk_monzo_flex"),
            make_account(id="acc_loan", type="uk_loan"),
        ]
        mock = AsyncMock()
        mock.list_accounts.return_value = {"accounts": accounts}
        with patch("coral_bot.server._get_client", return_value=mock):
            result = await _resolve_account()
            assert result == "acc_flex"

    async def test_raises_if_no_open_accounts(self):
        accounts = [
            make_account(id="acc_closed", type="uk_retail", closed=True),
        ]
        mock = AsyncMock()
        mock.list_accounts.return_value = {"accounts": accounts}
        with (
            patch("coral_bot.server._get_client", return_value=mock),
            pytest.raises(ValueError, match="No open accounts found"),
        ):
            await _resolve_account()


class TestAccountCaching:
    async def test_accounts_fetched_once(self, mock_accounts):
        await _resolve_account(account_name="flex")
        await _resolve_account(account_name="loan")
        mock_accounts.list_accounts.assert_called_once()

    async def test_api_error_raises_runtime_error(self):
        mock = AsyncMock()
        mock.list_accounts.return_value = {"error": "unauthorized"}
        with (
            patch("coral_bot.server._get_client", return_value=mock),
            pytest.raises(RuntimeError, match="Failed to list accounts"),
        ):
            await _get_accounts()


class TestOpenAccountPreferredOverClosed:
    async def test_open_preferred_over_closed(self):
        """When both open and closed accounts match, prefer the open one."""
        accounts = [
            make_account(id="acc_joint_closed", type="uk_retail_joint", closed=True),
            make_account(id="acc_joint_open", type="uk_retail_joint", closed=False),
        ]
        mock = AsyncMock()
        mock.list_accounts.return_value = {"accounts": accounts}
        with patch("coral_bot.server._get_client", return_value=mock):
            result = await _resolve_account(account_name="joint")
            assert result == "acc_joint_open"
