"""Tests for MCP tool functions in coral_bot.server."""

import pytest

from coral_bot.server import (
    _fetch_filtered_transactions,
    _filter_by_transaction_type,
    annotate_transaction,
    get_balance,
    get_transaction,
    list_accounts,
    list_pots,
    list_transactions,
    recurring_payments,
    search_transactions,
    spending_summary,
    whoami,
)
from tests.factories import make_account, make_pot, make_transaction

# ---------------------------------------------------------------------------
# _fetch_filtered_transactions
# ---------------------------------------------------------------------------


class TestFetchFilteredTransactions:
    async def test_excludes_pot_transfers(self, mock_client):
        normal_tx = make_transaction(id="tx_normal")
        pot_tx = make_transaction(id="tx_pot", metadata={"pot_id": "pot_xxx"})
        mock_client.list_all_transactions.return_value = {"transactions": [normal_tx, pot_tx]}

        result = await _fetch_filtered_transactions("acc_abc123")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "tx_normal"

    async def test_excludes_zero_amount_by_default(self, mock_client):
        normal_tx = make_transaction(id="tx_normal", amount=-500)
        zero_tx = make_transaction(id="tx_zero", amount=0)
        mock_client.list_all_transactions.return_value = {"transactions": [normal_tx, zero_tx]}

        result = await _fetch_filtered_transactions("acc_abc123")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "tx_normal"

    async def test_includes_zero_when_asked(self, mock_client):
        normal_tx = make_transaction(id="tx_normal", amount=-500)
        zero_tx = make_transaction(id="tx_zero", amount=0)
        mock_client.list_all_transactions.return_value = {"transactions": [normal_tx, zero_tx]}

        result = await _fetch_filtered_transactions("acc_abc123", exclude_zero=False)
        assert len(result) == 2

    async def test_returns_error_string(self, mock_client):
        mock_client.list_all_transactions.return_value = {"error": "Unauthorized"}

        result = await _fetch_filtered_transactions("acc_abc123")
        assert isinstance(result, str)
        assert result.startswith("Error:")

    async def test_normalises_timestamps(self, mock_client):
        mock_client.list_all_transactions.return_value = {"transactions": []}

        await _fetch_filtered_transactions("acc_abc123", since="2026-03", before="2026-04")
        mock_client.list_all_transactions.assert_called_once_with(
            "acc_abc123", since="2026-03-01T00:00:00Z", before="2026-04-01T00:00:00Z"
        )


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


class TestWhoami:
    async def test_success(self, mock_client):
        mock_client.whoami.return_value = {
            "user_id": "user_123",
            "client_id": "oauth_456",
        }

        result = await whoami()
        assert "Authenticated: yes" in result
        assert "user_123" in result
        assert "oauth_456" in result

    async def test_error(self, mock_client):
        mock_client.whoami.return_value = {"error": "Unauthorized"}

        result = await whoami()
        assert result.startswith("Authentication failed:")


# ---------------------------------------------------------------------------
# list_accounts
# ---------------------------------------------------------------------------


class TestListAccounts:
    async def test_success(self, mock_client):
        acc1 = make_account(id="acc_111", type="uk_retail")
        acc2 = make_account(id="acc_222", type="uk_monzo_flex")
        mock_client.list_accounts.return_value = {"accounts": [acc1, acc2]}

        result = await list_accounts()
        assert "2 account(s)" in result
        assert "acc_111" in result
        assert "acc_222" in result
        assert "Uk Retail" in result

    async def test_empty(self, mock_client):
        mock_client.list_accounts.return_value = {"accounts": []}

        result = await list_accounts()
        assert result == "No accounts found."

    async def test_closed_account(self, mock_client):
        acc = make_account(closed=True)
        mock_client.list_accounts.return_value = {"accounts": [acc]}

        result = await list_accounts()
        assert "(CLOSED)" in result

    async def test_error(self, mock_client):
        mock_client.list_accounts.return_value = {"error": "Server error"}

        result = await list_accounts()
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------


class TestGetBalance:
    async def test_success(self, mock_client):
        mock_client.get_balance.return_value = {
            "balance": 27532,
            "total_balance": 1532986,
            "spend_today": -999,
            "currency": "GBP",
        }
        mock_client.list_pots.return_value = {"pots": []}

        result = await get_balance("acc_abc123")
        assert "£275.32" in result
        assert "£15,329.86" in result
        assert "-£9.99" in result

    async def test_includes_pot_breakdown(self, mock_client):
        mock_client.get_balance.return_value = {
            "balance": 27532,
            "total_balance": 1532986,
            "spend_today": -999,
            "currency": "GBP",
        }
        mock_client.list_pots.return_value = {
            "pots": [
                make_pot(name="Holiday", balance=100000),
                make_pot(name="Emergency", balance=50000),
            ]
        }

        result = await get_balance("acc_abc123")
        assert "£275.32" in result
        assert "Pots:" in result
        assert "Holiday: £1,000.00" in result
        assert "Emergency: £500.00" in result

    async def test_no_pots(self, mock_client):
        mock_client.get_balance.return_value = {
            "balance": 27532,
            "total_balance": 27532,
            "spend_today": 0,
            "currency": "GBP",
        }
        mock_client.list_pots.return_value = {"pots": []}

        result = await get_balance("acc_abc123")
        assert "£275.32" in result
        assert "Pots:" not in result

    async def test_pots_error_graceful(self, mock_client):
        mock_client.get_balance.return_value = {
            "balance": 27532,
            "total_balance": 27532,
            "spend_today": 0,
            "currency": "GBP",
        }
        mock_client.list_pots.return_value = {"error": "Forbidden"}

        result = await get_balance("acc_abc123")
        assert "£275.32" in result
        assert "Pots:" not in result

    async def test_excludes_deleted_pots(self, mock_client):
        mock_client.get_balance.return_value = {
            "balance": 27532,
            "total_balance": 1532986,
            "spend_today": 0,
            "currency": "GBP",
        }
        mock_client.list_pots.return_value = {
            "pots": [
                make_pot(name="Active", balance=100000),
                make_pot(name="Gone", balance=50000, deleted=True),
            ]
        }

        result = await get_balance("acc_abc123")
        assert "Active" in result
        assert "Gone" not in result

    async def test_error(self, mock_client):
        mock_client.get_balance.return_value = {"error": "Not found"}

        result = await get_balance("acc_abc123")
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# list_transactions
# ---------------------------------------------------------------------------


class TestListTransactions:
    async def test_success(self, mock_client):
        txs = [
            make_transaction(id="tx_001", amount=-1500),
            make_transaction(id="tx_002", amount=-2000),
            make_transaction(id="tx_003", amount=-300),
        ]
        mock_client.list_transactions.return_value = {"transactions": txs}

        result = await list_transactions("acc_abc123")
        assert "3 transaction(s)" in result
        assert "tx_001" in result
        assert "tx_002" in result
        assert "tx_003" in result

    async def test_empty(self, mock_client):
        mock_client.list_transactions.return_value = {"transactions": []}

        result = await list_transactions("acc_abc123")
        assert "No transactions found" in result

    async def test_excludes_pot_transfers_by_default(self, mock_client):
        normal = make_transaction(id="tx_normal")
        pot = make_transaction(id="tx_pot", metadata={"pot_id": "pot_xxx"})
        mock_client.list_transactions.return_value = {"transactions": [normal, pot]}

        result = await list_transactions("acc_abc123")
        assert "tx_normal" in result
        assert "tx_pot" not in result

    async def test_includes_pot_transfers_when_asked(self, mock_client):
        normal = make_transaction(id="tx_normal")
        pot = make_transaction(id="tx_pot", metadata={"pot_id": "pot_xxx"})
        mock_client.list_transactions.return_value = {"transactions": [normal, pot]}

        result = await list_transactions("acc_abc123", include_pot_transfers=True)
        assert "tx_normal" in result
        assert "tx_pot" in result

    async def test_error(self, mock_client):
        mock_client.list_transactions.return_value = {"error": "Unauthorized"}

        result = await list_transactions("acc_abc123")
        assert result.startswith("Error:")

    async def test_passes_normalised_timestamps(self, mock_client):
        mock_client.list_all_transactions.return_value = {"transactions": []}

        await list_transactions("acc_abc123", since="2026-03", before="2026-04")
        # When since is set, list_transactions auto-paginates via list_all_transactions
        call_kwargs = mock_client.list_all_transactions.call_args.kwargs
        assert call_kwargs["since"] == "2026-03-01T00:00:00Z"
        assert call_kwargs["before"] == "2026-04-01T00:00:00Z"

    async def test_auto_paginates_when_since_provided(self, mock_client):
        txs = [make_transaction(id=f"tx_{i:03d}", amount=-100) for i in range(150)]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await list_transactions("acc_abc123", since="2026-01")
        mock_client.list_all_transactions.assert_called_once()
        mock_client.list_transactions.assert_not_called()
        # Default limit is 100, so output is truncated
        assert "showing 100 of 150 total" in result

    async def test_single_page_when_no_since(self, mock_client):
        mock_client.list_transactions.return_value = {"transactions": [make_transaction()]}

        result = await list_transactions("acc_abc123")
        mock_client.list_transactions.assert_called_once()
        mock_client.list_all_transactions.assert_not_called()
        assert "1 transaction(s)" in result

    async def test_limit_caps_paginated_results(self, mock_client):
        txs = [make_transaction(id=f"tx_{i:03d}", amount=-100) for i in range(150)]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await list_transactions("acc_abc123", since="2026-01", limit=50)
        assert "showing 50 of 150 total" in result
        assert "tx_049" in result
        assert "tx_050" not in result

    async def test_filter_income_only(self, mock_client):
        txs = [
            make_transaction(id="tx_income", amount=5000),
            make_transaction(id="tx_spend", amount=-1500),
        ]
        mock_client.list_transactions.return_value = {"transactions": txs}

        result = await list_transactions("acc_abc123", transaction_type="income")
        assert "tx_income" in result
        assert "tx_spend" not in result

    async def test_filter_spending_only(self, mock_client):
        txs = [
            make_transaction(id="tx_income", amount=5000),
            make_transaction(id="tx_spend", amount=-1500),
        ]
        mock_client.list_transactions.return_value = {"transactions": txs}

        result = await list_transactions("acc_abc123", transaction_type="spending")
        assert "tx_spend" in result
        assert "tx_income" not in result

    async def test_invalid_transaction_type(self, mock_client):
        mock_client.list_transactions.return_value = {"transactions": []}
        result = await list_transactions("acc_abc123", transaction_type="invalid")
        assert "Error:" in result
        assert "transaction_type" in result


# ---------------------------------------------------------------------------
# get_transaction
# ---------------------------------------------------------------------------


class TestGetTransaction:
    async def test_basic(self, mock_client):
        tx = make_transaction(
            id="tx_detail",
            amount=-4599,
            description="AMAZON EU",
            category="shopping",
            created="2026-03-05T10:30:00Z",
            settled="2026-03-06T00:00:00Z",
        )
        mock_client.get_transaction.return_value = {"transaction": tx}

        result = await get_transaction("tx_detail")
        assert "tx_detail" in result
        assert "-£45.99" in result
        assert "AMAZON EU" in result
        assert "shopping" in result

    async def test_with_merchant(self, mock_client):
        tx = make_transaction(
            merchant={
                "name": "Amazon",
                "category": "shopping",
                "address": {
                    "short_formatted": "London",
                    "city": "London",
                    "postcode": "EC1A 1BB",
                    "country": "GB",
                },
                "online": True,
            }
        )
        mock_client.get_transaction.return_value = {"transaction": tx}

        result = await get_transaction("tx_0000abc123")
        assert "Merchant:" in result
        assert "Amazon" in result
        assert "London" in result
        assert "Online: yes" in result

    async def test_with_notes(self, mock_client):
        tx = make_transaction(notes="Birthday present")
        mock_client.get_transaction.return_value = {"transaction": tx}

        result = await get_transaction("tx_0000abc123")
        assert "Notes: Birthday present" in result

    async def test_declined(self, mock_client):
        tx = make_transaction(decline_reason="INSUFFICIENT_FUNDS")
        mock_client.get_transaction.return_value = {"transaction": tx}

        result = await get_transaction("tx_0000abc123")
        assert "Declined: INSUFFICIENT_FUNDS" in result

    async def test_with_metadata(self, mock_client):
        tx = make_transaction(metadata={"flagged": "true", "notes": "suspicious"})
        mock_client.get_transaction.return_value = {"transaction": tx}

        result = await get_transaction("tx_0000abc123")
        assert "Metadata:" in result
        assert "flagged: true" in result

    async def test_error(self, mock_client):
        mock_client.get_transaction.return_value = {"error": "Not found"}

        result = await get_transaction("tx_0000abc123")
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# list_pots
# ---------------------------------------------------------------------------


class TestListPots:
    async def test_success(self, mock_client):
        pot1 = make_pot(id="pot_001", name="Holiday", balance=100000)
        pot2 = make_pot(id="pot_002", name="Emergency", balance=50000)
        mock_client.list_pots.return_value = {"pots": [pot1, pot2]}

        result = await list_pots("acc_abc123")
        assert "2 active pot(s)" in result
        assert "Holiday" in result
        assert "£1,000.00" in result
        assert "pot_001" in result

    async def test_filters_deleted(self, mock_client):
        active = make_pot(id="pot_active", name="Active")
        deleted = make_pot(id="pot_deleted", name="Deleted", deleted=True)
        mock_client.list_pots.return_value = {"pots": [active, deleted]}

        result = await list_pots("acc_abc123")
        assert "Active" in result
        assert "Deleted" not in result

    async def test_with_goal(self, mock_client):
        pot = make_pot(goal_amount=100000)
        mock_client.list_pots.return_value = {"pots": [pot]}

        result = await list_pots("acc_abc123")
        assert "(goal: £1,000.00)" in result

    async def test_locked(self, mock_client):
        pot = make_pot(locked=True)
        mock_client.list_pots.return_value = {"pots": [pot]}

        result = await list_pots("acc_abc123")
        assert "[LOCKED]" in result

    async def test_round_up(self, mock_client):
        pot = make_pot(round_up_multiplier=2)
        mock_client.list_pots.return_value = {"pots": [pot]}

        result = await list_pots("acc_abc123")
        assert "[Round-up: 2x]" in result

    async def test_empty(self, mock_client):
        mock_client.list_pots.return_value = {"pots": []}

        result = await list_pots("acc_abc123")
        assert result == "No active pots found."

    async def test_error(self, mock_client):
        mock_client.list_pots.return_value = {"error": "Unauthorized"}

        result = await list_pots("acc_abc123")
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# spending_summary
# ---------------------------------------------------------------------------


class TestSpendingSummary:
    async def test_success(self, mock_client):
        txs = [
            make_transaction(amount=-1500, category="groceries"),
            make_transaction(amount=-2000, category="groceries"),
            make_transaction(amount=-999, category="eating_out"),
            make_transaction(amount=300000, category="income"),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await spending_summary("acc_abc123")
        assert "Spending Summary" in result
        assert "Groceries" in result
        assert "Eating Out" in result
        assert "Income" in result
        assert "Total spend:" in result
        assert "Total income:" in result
        assert "Net:" in result

    async def test_empty(self, mock_client):
        mock_client.list_all_transactions.return_value = {"transactions": []}

        result = await spending_summary("acc_abc123")
        assert "No transactions found" in result

    async def test_error(self, mock_client):
        mock_client.list_all_transactions.return_value = {"error": "Unauthorized"}

        result = await spending_summary("acc_abc123")
        assert result.startswith("Error:")

    async def test_single_category(self, mock_client):
        txs = [
            make_transaction(amount=-500, category="groceries"),
            make_transaction(amount=-700, category="groceries"),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await spending_summary("acc_abc123")
        assert "Groceries" in result
        assert "2 transaction(s)" in result

    async def test_top3_limit(self, mock_client):
        """Categories with >3 transactions should only show top 3 merchants."""
        txs = [
            make_transaction(amount=-100, category="groceries", merchant={"name": f"Shop {i}"})
            for i in range(5)
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await spending_summary("acc_abc123")
        assert "5 transaction(s)" in result
        # Count merchant lines under Groceries (lines starting with "  - ")
        lines = result.split("\n")
        groceries_idx = next(i for i, ln in enumerate(lines) if "Groceries" in ln)
        merchant_lines = []
        for line in lines[groceries_idx + 1 :]:
            if line.startswith("  - "):
                merchant_lines.append(line)
            else:
                break
        assert len(merchant_lines) <= 3

    async def test_income_and_spend(self, mock_client):
        txs = [
            make_transaction(amount=-5000, category="groceries"),
            make_transaction(amount=300000, category="income"),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await spending_summary("acc_abc123")
        assert "Total spend: -£50.00" in result
        assert "Total income: £3,000.00" in result


# ---------------------------------------------------------------------------
# search_transactions
# ---------------------------------------------------------------------------


class TestSearchTransactions:
    async def test_by_merchant_name(self, mock_client):
        tesco = make_transaction(
            id="tx_tesco", merchant={"name": "Tesco"}, description="TESCO STORES"
        )
        asda = make_transaction(id="tx_asda", merchant={"name": "Asda"}, description="ASDA STORES")
        mock_client.list_all_transactions.return_value = {"transactions": [tesco, asda]}

        result = await search_transactions(account_id="acc_abc123", query="Tesco")
        assert "tx_tesco" in result
        assert "tx_asda" not in result

    async def test_by_description(self, mock_client):
        tx = make_transaction(id="tx_desc", merchant=None, description="PAYPAL *SPOTIFY")
        mock_client.list_all_transactions.return_value = {"transactions": [tx]}

        result = await search_transactions(account_id="acc_abc123", query="spotify")
        assert "tx_desc" in result

    async def test_by_notes(self, mock_client):
        tx = make_transaction(id="tx_notes", notes="Birthday present for Alice")
        mock_client.list_all_transactions.return_value = {"transactions": [tx]}

        result = await search_transactions(account_id="acc_abc123", query="alice")
        assert "tx_notes" in result

    async def test_case_insensitive(self, mock_client):
        tx = make_transaction(id="tx_ci", merchant={"name": "Tesco"})
        mock_client.list_all_transactions.return_value = {"transactions": [tx]}

        result = await search_transactions(account_id="acc_abc123", query="tESCO")
        assert "tx_ci" in result

    async def test_partial_match(self, mock_client):
        tx = make_transaction(id="tx_partial", merchant={"name": "Tesco Express"})
        mock_client.list_all_transactions.return_value = {"transactions": [tx]}

        result = await search_transactions(account_id="acc_abc123", query="tes")
        assert "tx_partial" in result

    async def test_no_results(self, mock_client):
        tx = make_transaction(merchant={"name": "Tesco"})
        mock_client.list_all_transactions.return_value = {"transactions": [tx]}

        result = await search_transactions(account_id="acc_abc123", query="walmart")
        assert "No transactions matching" in result

    async def test_shows_total(self, mock_client):
        txs = [
            make_transaction(id="tx_1", amount=-1500, merchant={"name": "Tesco"}),
            make_transaction(id="tx_2", amount=-2000, merchant={"name": "Tesco Extra"}),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await search_transactions(account_id="acc_abc123", query="tesco")
        assert "2 transaction(s)" in result
        assert "-£35.00" in result

    async def test_filter_income_only(self, mock_client):
        txs = [
            make_transaction(id="tx_salary", amount=300000, merchant={"name": "Employer"}),
            make_transaction(id="tx_coffee", amount=-350, merchant={"name": "Costa"}),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await search_transactions(
            account_id="acc_abc123", query="e", transaction_type="income"
        )
        assert "tx_salary" in result
        assert "tx_coffee" not in result

    async def test_filter_spending_only(self, mock_client):
        txs = [
            make_transaction(
                id="tx_salary",
                amount=300000,
                merchant={"name": "Employer"},
                description="EMPLOYER LTD",
            ),
            make_transaction(
                id="tx_coffee",
                amount=-350,
                merchant={"name": "Costa"},
                description="COSTA COFFEE",
            ),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await search_transactions(
            account_id="acc_abc123", query="costa", transaction_type="spending"
        )
        assert "tx_coffee" in result
        assert "tx_salary" not in result

    async def test_invalid_transaction_type(self, mock_client):
        mock_client.list_all_transactions.return_value = {"transactions": []}
        result = await search_transactions(
            account_id="acc_abc123", query="test", transaction_type="bad"
        )
        assert "Error:" in result
        assert "transaction_type" in result

    async def test_error(self, mock_client):
        mock_client.list_all_transactions.return_value = {"error": "Unauthorized"}

        result = await search_transactions(account_id="acc_abc123", query="test")
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# _filter_by_transaction_type
# ---------------------------------------------------------------------------


class TestFilterByTransactionType:
    def test_income(self):
        txs = [{"amount": 5000}, {"amount": -1500}, {"amount": 0}]
        result = _filter_by_transaction_type(txs, "income")
        assert len(result) == 1
        assert result[0]["amount"] == 5000

    def test_spending(self):
        txs = [{"amount": 5000}, {"amount": -1500}, {"amount": 0}]
        result = _filter_by_transaction_type(txs, "spending")
        assert len(result) == 1
        assert result[0]["amount"] == -1500

    def test_all(self):
        txs = [{"amount": 5000}, {"amount": -1500}, {"amount": 0}]
        result = _filter_by_transaction_type(txs, "all")
        assert len(result) == 3

    def test_invalid_raises_value_error(self):
        txs = [{"amount": 5000}]
        with pytest.raises(ValueError, match="transaction_type"):
            _filter_by_transaction_type(txs, "invalid")


# ---------------------------------------------------------------------------
# recurring_payments
# ---------------------------------------------------------------------------


class TestRecurringPayments:
    async def test_detects_repeat_merchants(self, mock_client):
        txs = [
            make_transaction(id="tx_1", merchant={"name": "Netflix"}, amount=-999),
            make_transaction(id="tx_2", merchant={"name": "Netflix"}, amount=-999),
            make_transaction(id="tx_3", merchant={"name": "Netflix"}, amount=-999),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await recurring_payments("acc_abc123")
        assert "Netflix" in result
        assert "3" in result  # transaction count

    async def test_excludes_single_occurrence(self, mock_client):
        txs = [
            make_transaction(merchant={"name": "Netflix"}, amount=-999),
            make_transaction(merchant={"name": "Netflix"}, amount=-999),
            make_transaction(merchant={"name": "One-off Shop"}, amount=-500),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await recurring_payments("acc_abc123")
        assert "Netflix" in result
        assert "One-off Shop" not in result

    async def test_no_recurring(self, mock_client):
        txs = [
            make_transaction(merchant={"name": "Shop A"}, amount=-500),
            make_transaction(merchant={"name": "Shop B"}, amount=-700),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await recurring_payments("acc_abc123")
        assert "No recurring payments detected" in result

    async def test_shows_avg_and_total(self, mock_client):
        txs = [
            make_transaction(
                merchant={"name": "Netflix"}, amount=-999, created="2026-03-01T00:00:00Z"
            ),
            make_transaction(
                merchant={"name": "Netflix"}, amount=-999, created="2026-03-15T00:00:00Z"
            ),
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await recurring_payments("acc_abc123")
        assert "Total: -£19.98" in result
        assert "Avg: -£9.99" in result

    async def test_truncates_dates(self, mock_client):
        txs = [
            make_transaction(
                merchant={"name": "Daily"},
                amount=-100,
                created=f"2026-03-{i + 1:02d}T00:00:00Z",
            )
            for i in range(7)
        ]
        mock_client.list_all_transactions.return_value = {"transactions": txs}

        result = await recurring_payments("acc_abc123")
        assert "(+2 more)" in result

    async def test_empty(self, mock_client):
        mock_client.list_all_transactions.return_value = {"transactions": []}

        result = await recurring_payments("acc_abc123")
        assert "No transactions found" in result

    async def test_error(self, mock_client):
        mock_client.list_all_transactions.return_value = {"error": "Unauthorized"}

        result = await recurring_payments("acc_abc123")
        assert result.startswith("Error:")


# ---------------------------------------------------------------------------
# annotate_transaction
# ---------------------------------------------------------------------------


class TestAnnotateTransaction:
    async def test_success(self, mock_client):
        mock_client.annotate_transaction.return_value = {"transaction": {}}

        result = await annotate_transaction("tx_abc123", "notes", "test note")
        assert "Successfully annotated" in result
        assert "notes=test note" in result

    async def test_disallowed_key(self, mock_client):
        result = await annotate_transaction("tx_abc123", "dangerous_key", "value")
        assert "not allowed" in result
        assert "notes" in result  # lists allowed keys

    async def test_notes_key_allowed(self, mock_client):
        mock_client.annotate_transaction.return_value = {"transaction": {}}

        result = await annotate_transaction("tx_abc123", "notes", "hello")
        assert "Successfully" in result

    async def test_flagged_key_allowed(self, mock_client):
        mock_client.annotate_transaction.return_value = {"transaction": {}}

        result = await annotate_transaction("tx_abc123", "flagged", "true")
        assert "Successfully" in result

    async def test_is_subscription_key_allowed(self, mock_client):
        mock_client.annotate_transaction.return_value = {"transaction": {}}

        result = await annotate_transaction("tx_abc123", "is_subscription", "true")
        assert "Successfully" in result

    async def test_error(self, mock_client):
        mock_client.annotate_transaction.return_value = {"error": "Forbidden"}

        result = await annotate_transaction("tx_abc123", "notes", "test")
        assert result.startswith("Error:")
