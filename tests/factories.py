"""Factory helpers for creating test data dicts."""


def make_transaction(**overrides) -> dict:
    """Create a transaction dict with sensible defaults."""
    tx = {
        "id": "tx_0000abc123",
        "amount": -1500,
        "currency": "GBP",
        "description": "TESCO STORES",
        "category": "groceries",
        "created": "2026-03-05T10:30:00Z",
        "settled": "2026-03-06T00:00:00Z",
        "merchant": {"name": "Tesco", "category": "groceries"},
        "counterparty": {},
        "metadata": {},
        "notes": "",
        "decline_reason": None,
        "scheme": "mastercard",
    }
    tx.update(overrides)
    return tx


def make_account(**overrides) -> dict:
    """Create an account dict with sensible defaults."""
    acc = {
        "id": "acc_00009bDXc5T9",
        "type": "uk_retail",
        "description": "user_00009SoSJbW8",
        "created": "2024-01-01T00:00:00Z",
        "closed": False,
    }
    acc.update(overrides)
    return acc


def make_pot(**overrides) -> dict:
    """Create a pot dict with sensible defaults."""
    pot = {
        "id": "pot_0000abc123",
        "name": "Savings",
        "balance": 50000,
        "currency": "GBP",
        "style": "savings",
        "goal_amount": None,
        "deleted": False,
        "locked": False,
        "round_up_multiplier": None,
    }
    pot.update(overrides)
    return pot
