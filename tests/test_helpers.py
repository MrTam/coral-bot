"""Tests for pure helper functions in coral_bot.server."""

import pytest

from coral_bot.server import (
    _format_amount,
    _format_tx_line,
    _is_pot_transfer,
    _normalise_timestamp,
    _tx_display_name,
)
from tests.factories import make_transaction

# ---------------------------------------------------------------------------
# _format_amount
# ---------------------------------------------------------------------------


class TestFormatAmount:
    def test_positive_gbp(self):
        assert _format_amount(1234, "GBP") == "£12.34"

    def test_negative_gbp(self):
        assert _format_amount(-1234, "GBP") == "-£12.34"

    def test_zero(self):
        assert _format_amount(0, "GBP") == "£0.00"

    def test_usd(self):
        assert _format_amount(999, "USD") == "$9.99"

    def test_eur(self):
        assert _format_amount(500, "EUR") == "€5.00"

    def test_unknown_currency(self):
        assert _format_amount(100, "JPY") == "JPY 1.00"

    def test_single_penny(self):
        assert _format_amount(1, "GBP") == "£0.01"

    def test_negative_single_penny(self):
        assert _format_amount(-1, "GBP") == "-£0.01"


# ---------------------------------------------------------------------------
# _normalise_timestamp
# ---------------------------------------------------------------------------


class TestNormaliseTimestamp:
    def test_none(self):
        assert _normalise_timestamp(None) is None

    def test_empty_string(self):
        assert _normalise_timestamp("") is None

    def test_full_rfc3339(self):
        assert _normalise_timestamp("2026-03-01T00:00:00Z") == "2026-03-01T00:00:00Z"

    def test_date_only(self):
        assert _normalise_timestamp("2026-03-01") == "2026-03-01T00:00:00Z"

    def test_month_only(self):
        assert _normalise_timestamp("2026-03") == "2026-03-01T00:00:00Z"

    def test_rfc3339_with_offset(self):
        result = _normalise_timestamp("2026-03-01T00:00:00+01:00")
        assert result == "2026-03-01T00:00:00+01:00"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Unrecognised date format"):
            _normalise_timestamp("not-a-date")

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            _normalise_timestamp("2026-13-01")

    def test_garbage_with_t_raises(self):
        with pytest.raises(ValueError):
            _normalise_timestamp("helloTworld")


# ---------------------------------------------------------------------------
# _is_pot_transfer
# ---------------------------------------------------------------------------


class TestIsPotTransfer:
    def test_pot_transfer_by_metadata(self):
        tx = make_transaction(metadata={"pot_id": "pot_xxx"})
        assert _is_pot_transfer(tx) is True

    def test_pot_transfer_by_scheme(self):
        tx = make_transaction(scheme="uk_retail_pot", metadata={})
        assert _is_pot_transfer(tx) is True

    def test_not_pot_transfer(self):
        tx = make_transaction(scheme="mastercard", metadata={})
        assert _is_pot_transfer(tx) is False

    def test_empty_tx(self):
        assert _is_pot_transfer({}) is False

    def test_no_false_positive_without_scheme(self):
        """Transactions without a scheme field should not be flagged as pot transfers."""
        tx = make_transaction(description="Pottery Barn")
        del tx["scheme"]
        tx["metadata"] = {}
        assert _is_pot_transfer(tx) is False


# ---------------------------------------------------------------------------
# _tx_display_name
# ---------------------------------------------------------------------------


class TestTxDisplayName:
    def test_merchant_dict(self):
        tx = make_transaction(merchant={"name": "Tesco"})
        assert _tx_display_name(tx) == "Tesco"

    def test_merchant_dict_no_name(self):
        tx = make_transaction(merchant={})
        assert _tx_display_name(tx) == "Unknown"

    def test_counterparty(self):
        tx = make_transaction(merchant=None, counterparty={"name": "Alice"})
        assert _tx_display_name(tx) == "Alice"

    def test_description_fallback(self):
        tx = make_transaction(merchant=None, counterparty={}, description="ATM")
        assert _tx_display_name(tx) == "ATM"

    def test_all_missing(self):
        assert _tx_display_name({}) == "Unknown"

    def test_merchant_string_not_dict(self):
        """When merchant is a string ID (not expanded), fall back to description."""
        tx = make_transaction(merchant="merch_xxx", description="Some Shop")
        assert _tx_display_name(tx) == "Some Shop"


# ---------------------------------------------------------------------------
# _format_tx_line
# ---------------------------------------------------------------------------


class TestFormatTxLine:
    def test_basic(self):
        tx = make_transaction()
        line = _format_tx_line(tx)
        assert "2026-03-05" in line
        assert "-£15.00" in line
        assert "Tesco" in line
        assert "groceries" in line
        assert "tx_0000abc123" in line

    def test_with_notes(self):
        tx = make_transaction(notes="Weekly shop")
        line = _format_tx_line(tx)
        assert "Note: Weekly shop" in line

    def test_without_notes(self):
        tx = make_transaction(notes="")
        line = _format_tx_line(tx)
        assert "Note:" not in line

    def test_declined(self):
        tx = make_transaction(decline_reason="INSUFFICIENT_FUNDS")
        line = _format_tx_line(tx)
        assert "[DECLINED]" in line

    def test_positive_amount(self):
        tx = make_transaction(amount=600)
        line = _format_tx_line(tx)
        assert "£6.00" in line
