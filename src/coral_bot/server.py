"""coral-bot — MCP server exposing the Monzo banking API as tools for AI agents."""

import json
import logging
import os
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from coral_bot.client import MonzoClient
from coral_bot.oauth_provider import CoralAuthProvider
from coral_bot.oauth_store import CoralAccessToken, OAuthStore
from coral_bot.tokens import TokenManager
from coral_bot.users import UserStore

# All logging goes to stderr (stdout is reserved for MCP JSON-RPC in stdio mode)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Per-user MonzoClient instances, keyed by user_id ("local" for stdio mode)
_clients: dict[str, MonzoClient] = {}

# Per-user account caches, keyed by user_id
_account_caches: dict[str, list[dict]] = {}

# The default account type to use when no account is specified
_DEFAULT_ACCOUNT_TYPE = "uk_retail"

# Module-level reference so tools can access the store in HTTP mode
_user_store: UserStore | None = None

# Module-level reference so the auth callback can generate codes
_auth_provider: CoralAuthProvider | None = None

# Module-level reference so the auth callback can load pending flows
_oauth_store: OAuthStore | None = None


def _get_current_user_id() -> str:
    """Get the current user ID from the auth context, or 'local' for stdio mode."""
    access_token = get_access_token()
    if access_token is not None and isinstance(access_token, CoralAccessToken):
        return access_token.user_id
    return "local"


def _get_client() -> MonzoClient:
    user_id = _get_current_user_id()

    if user_id not in _clients:
        if user_id == "local":
            token_manager = TokenManager()
        else:
            if _user_store is None:
                raise RuntimeError("UserStore not initialised (HTTP mode misconfigured)")
            token_file = _user_store.get_token_file(user_id)
            if token_file is None:
                raise RuntimeError(f"No token file found for user {user_id}")
            token_manager = TokenManager(token_file=token_file)
        _clients[user_id] = MonzoClient(token_manager=token_manager)

    return _clients[user_id]


async def _get_accounts() -> list[dict]:
    """Fetch and cache the list of accounts for the current user."""
    user_id = _get_current_user_id()
    if user_id not in _account_caches:
        result = await _get_client().list_accounts()
        if "error" in result:
            raise RuntimeError(f"Failed to list accounts: {result['error']}")
        _account_caches[user_id] = result.get("accounts", [])
    return _account_caches[user_id]


async def _resolve_account(
    account_id: str | None = None,
    account_name: str | None = None,
) -> str:
    """Resolve an account ID from an ID, a friendly name, or the default.

    Resolution order:
        1. Explicit account_id (returned as-is)
        2. Fuzzy match account_name against account type and description
        3. Default to the main retail (current) account

    Raises ValueError if account_name is provided but no match is found.
    """
    if account_id:
        return account_id

    accounts = await _get_accounts()

    if account_name:
        query = account_name.lower().strip()
        for acc in accounts:
            if acc.get("closed"):
                continue
            acc_type = acc.get("type", "").lower().replace("_", " ")
            description = acc.get("description", "").lower()
            # Match against type keywords (e.g. "flex" matches "uk_monzo_flex")
            if query in acc_type or query in description:
                return acc["id"]
        # Also try closed accounts if no open match
        for acc in accounts:
            acc_type = acc.get("type", "").lower().replace("_", " ")
            description = acc.get("description", "").lower()
            if query in acc_type or query in description:
                return acc["id"]
        available = [acc.get("type", "unknown").replace("_", " ").title() for acc in accounts]
        raise ValueError(
            f"No account matching '{account_name}'. Available accounts: {', '.join(available)}"
        )

    # Default: first open account matching the default type
    for acc in accounts:
        if acc.get("type") == _DEFAULT_ACCOUNT_TYPE and not acc.get("closed"):
            return acc["id"]

    # Fallback: first open account
    for acc in accounts:
        if not acc.get("closed"):
            return acc["id"]

    raise ValueError("No open accounts found.")


# Allowed metadata keys for annotate_transaction
_ALLOWED_METADATA_KEYS = {"notes", "flagged", "is_subscription"}


def _format_amount(minor_units: int, currency: str = "GBP") -> str:
    """Format an amount from minor units (pence) to a readable string."""
    symbols = {"GBP": "£", "USD": "$", "EUR": "€"}
    symbol = symbols.get(currency, currency + " ")
    if minor_units < 0:
        return f"-{symbol}{abs(minor_units) / 100:,.2f}"
    return f"{symbol}{minor_units / 100:,.2f}"


def _normalise_timestamp(value: str | None) -> str | None:
    """Accept flexible date inputs and normalise to RFC 3339.

    Accepts:
        - Full RFC 3339: 2026-03-01T00:00:00Z (validated via fromisoformat)
        - Date only: 2026-03-01 (becomes 2026-03-01T00:00:00Z)
        - Month only: 2026-03 (becomes 2026-03-01T00:00:00Z)
        - None (passed through)

    Raises ValueError for unrecognised formats.
    """
    if not value:
        return None

    # YYYY-MM -> YYYY-MM-01
    if len(value) == 7:
        value = f"{value}-01"

    # YYYY-MM-DD -> full timestamp
    if len(value) == 10:
        value = f"{value}T00:00:00Z"

    # Validate the full timestamp
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Unrecognised date format: {value!r}. Use YYYY-MM-DD or YYYY-MM.") from e

    return value


def _is_pot_transfer(tx: dict) -> bool:
    """Check if a transaction is an internal pot transfer or round-up."""
    meta = tx.get("metadata", {})
    if meta.get("pot_id"):
        return True
    return tx.get("scheme") == "uk_retail_pot"


def _tx_display_name(tx: dict) -> str:
    """Get the best display name for a transaction."""
    merchant = tx.get("merchant")
    if isinstance(merchant, dict):
        return merchant.get("name", "Unknown")
    # Counterparty (for bank transfers / P2P)
    counterparty = tx.get("counterparty", {})
    if counterparty and counterparty.get("name"):
        return counterparty["name"]
    return tx.get("description", "Unknown")


def _format_tx_line(tx: dict) -> str:
    """Format a single transaction as a display line."""
    currency = tx.get("currency", "GBP")
    amount = _format_amount(tx.get("amount", 0), currency)
    name = _tx_display_name(tx)
    category = tx.get("category", "unknown")
    created = tx.get("created", "")[:10]
    declined = " [DECLINED]" if tx.get("decline_reason") else ""

    line = f"- {created} | {amount:>10} | {name} | {category}{declined}"
    if tx.get("notes"):
        line += f"\n  Note: {tx['notes']}"
    line += f"\n  ID: {tx['id']}"
    return line


def _filter_by_transaction_type(transactions: list[dict], transaction_type: str) -> list[dict]:
    """Filter transactions by direction: income (positive), spending (negative), or all.

    Raises:
        ValueError: If transaction_type is not one of "all", "income", or "spending".
    """
    if transaction_type == "all":
        return transactions
    if transaction_type == "income":
        return [tx for tx in transactions if tx.get("amount", 0) > 0]
    if transaction_type == "spending":
        return [tx for tx in transactions if tx.get("amount", 0) < 0]
    raise ValueError(
        f"transaction_type must be 'all', 'income', or 'spending', got '{transaction_type}'"
    )


async def _fetch_filtered_transactions(
    account_id: str,
    since: str | None = None,
    before: str | None = None,
    exclude_zero: bool = True,
) -> list[dict] | str:
    """Fetch all transactions for a period, excluding pot transfers.

    Returns a list of transactions, or an error string.
    """
    client = _get_client()
    since_ts = _normalise_timestamp(since)
    before_ts = _normalise_timestamp(before)
    result = await client.list_all_transactions(account_id, since=since_ts, before=before_ts)
    if "error" in result:
        return f"Error: {result['error']}"

    transactions = result.get("transactions", [])
    transactions = [
        tx
        for tx in transactions
        if not _is_pot_transfer(tx) and (not exclude_zero or tx.get("amount", 0) != 0)
    ]
    return transactions


# ---------------------------------------------------------------------------
# Tool functions (registered on the FastMCP instance in create_mcp_server)
# ---------------------------------------------------------------------------


async def whoami() -> str:
    """Check authentication status with Monzo. Returns the authenticated user ID and token info."""
    result = await _get_client().whoami()
    if "error" in result:
        return f"Authentication failed: {result['error']}"
    return (
        f"Authenticated: yes\n"
        f"User ID: {result.get('user_id', 'unknown')}\n"
        f"Client ID: {result.get('client_id', 'unknown')}"
    )


async def list_accounts() -> str:
    """List all Monzo accounts. Returns account IDs, types, and descriptions.

    Use the returned account_id values with other tools like get_balance and list_transactions.
    """
    result = await _get_client().list_accounts()
    if "error" in result:
        return f"Error: {result['error']}"

    accounts = result.get("accounts", [])
    if not accounts:
        return "No accounts found."

    lines = []
    for acc in accounts:
        closed = " (CLOSED)" if acc.get("closed") else ""
        lines.append(
            f"- {acc.get('type', 'unknown').replace('_', ' ').title()}{closed}\n"
            f"  Account ID: {acc['id']}\n"
            f"  Description: {acc.get('description', 'N/A')}\n"
            f"  Created: {acc.get('created', 'N/A')}"
        )
    return f"Found {len(accounts)} account(s):\n\n" + "\n\n".join(lines)


async def get_balance(
    account_id: str | None = None,
    account_name: str | None = None,
) -> str:
    """Get the current balance for a Monzo account, including a breakdown of pot balances.

    Args:
        account_id: The account ID (starts with acc_). Optional if account_name is provided.
        account_name: Friendly name, e.g. "flex", "joint", "loan". Defaults to current account.
    """
    try:
        account_id = await _resolve_account(account_id, account_name)
    except ValueError as e:
        return f"Error: {e}"
    client = _get_client()
    result = await client.get_balance(account_id)
    if "error" in result:
        return f"Error: {result['error']}"

    currency = result.get("currency", "GBP")
    lines = [
        f"Balance: {_format_amount(result.get('balance', 0), currency)}",
        f"Total balance (incl. pots): {_format_amount(result.get('total_balance', 0), currency)}",
        f"Spend today: {_format_amount(result.get('spend_today', 0), currency)}",
        f"Currency: {currency}",
    ]

    # Append pot breakdown if available
    pots_result = await client.list_pots(account_id)
    if "error" not in pots_result:
        pots = [p for p in pots_result.get("pots", []) if not p.get("deleted")]
        if pots:
            lines.append("")
            lines.append("Pots:")
            for pot in pots:
                pot_currency = pot.get("currency", currency)
                balance = _format_amount(pot.get("balance", 0), pot_currency)
                lines.append(f"  - {pot.get('name', 'Unnamed')}: {balance}")

    return "\n".join(lines)


async def list_transactions(
    account_id: str | None = None,
    account_name: str | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: int = 100,
    include_pot_transfers: bool = False,
    transaction_type: str = "all",
) -> str:
    """List transactions for a Monzo account, optionally filtered by date range.

    When a date range is specified (since is set), automatically paginates through
    all matching transactions. Otherwise fetches a single page (max 100).

    By default, internal pot transfers (round-ups, savings moves) are excluded
    to focus on real spending. Set include_pot_transfers=True to see everything.

    Args:
        account_id: The account ID (starts with acc_). Optional if account_name is provided.
        account_name: Friendly name, e.g. "flex", "joint", "loan". Defaults to current account.
        since: Start time. Accepts RFC 3339, date (2024-01-01), or month (2024-01).
        before: Only return transactions before this time. Same formats as since.
        limit: Maximum number of transactions to return (default 100).
        include_pot_transfers: Include internal pot transfers and round-ups (default False).
        transaction_type: Filter by direction: "income", "spending", or "all" (default).
    """
    try:
        account_id = await _resolve_account(account_id, account_name)
    except ValueError as e:
        return f"Error: {e}"
    client = _get_client()
    since_ts = _normalise_timestamp(since)
    before_ts = _normalise_timestamp(before)

    # Auto-paginate when a date range is specified; single page otherwise
    if since_ts:
        result = await client.list_all_transactions(account_id, since=since_ts, before=before_ts)
    else:
        result = await client.list_transactions(
            account_id, since=since_ts, before=before_ts, limit=limit
        )
    if "error" in result:
        return f"Error: {result['error']}"

    transactions = result.get("transactions", [])
    if not include_pot_transfers:
        transactions = [tx for tx in transactions if not _is_pot_transfer(tx)]
    try:
        transactions = _filter_by_transaction_type(transactions, transaction_type)
    except ValueError as e:
        return f"Error: {e}"

    if not transactions:
        return "No transactions found for the given criteria."

    total_count = len(transactions)
    truncated = total_count > limit
    transactions = transactions[:limit]

    lines = [_format_tx_line(tx) for tx in transactions]
    header = f"Found {len(transactions)} transaction(s)"
    if truncated:
        header += f" (showing {limit} of {total_count} total)"
    return f"{header}:\n\n" + "\n".join(lines)


async def get_transaction(transaction_id: str) -> str:
    """Get full details for a single transaction, including expanded merchant info.

    Args:
        transaction_id: The transaction ID (starts with tx_).
    """
    result = await _get_client().get_transaction(transaction_id)
    if "error" in result:
        return f"Error: {result['error']}"

    tx = result.get("transaction", result)
    currency = tx.get("currency", "GBP")
    amount = _format_amount(tx.get("amount", 0), currency)

    lines = [
        f"Transaction: {tx.get('id', 'unknown')}",
        f"Amount: {amount}",
        f"Description: {tx.get('description', 'N/A')}",
        f"Category: {tx.get('category', 'unknown')}",
        f"Created: {tx.get('created', 'N/A')}",
        f"Settled: {tx.get('settled', 'N/A')}",
    ]

    if tx.get("notes"):
        lines.append(f"Notes: {tx['notes']}")

    if tx.get("decline_reason"):
        lines.append(f"Declined: {tx['decline_reason']}")

    merchant = tx.get("merchant")
    if isinstance(merchant, dict):
        lines.append("")
        lines.append("Merchant:")
        lines.append(f"  Name: {merchant.get('name', 'N/A')}")
        lines.append(f"  Category: {merchant.get('category', 'N/A')}")
        if merchant.get("address"):
            addr = merchant["address"]
            parts = [
                addr.get("short_formatted", ""),
                addr.get("city", ""),
                addr.get("postcode", ""),
                addr.get("country", ""),
            ]
            lines.append(f"  Address: {', '.join(p for p in parts if p)}")
        if merchant.get("online"):
            lines.append("  Online: yes")

    metadata = tx.get("metadata", {})
    if metadata:
        lines.append("")
        lines.append("Metadata:")
        for k, v in metadata.items():
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


async def list_pots(
    account_id: str | None = None,
    account_name: str | None = None,
) -> str:
    """List all savings pots for a Monzo account.

    Args:
        account_id: The account ID (starts with acc_). Optional if account_name is provided.
        account_name: Friendly name, e.g. "flex", "joint", "loan". Defaults to current account.
    """
    try:
        account_id = await _resolve_account(account_id, account_name)
    except ValueError as e:
        return f"Error: {e}"
    result = await _get_client().list_pots(account_id)
    if "error" in result:
        return f"Error: {result['error']}"

    pots = [p for p in result.get("pots", []) if not p.get("deleted")]
    if not pots:
        return "No active pots found."

    lines = []
    for pot in pots:
        currency = pot.get("currency", "GBP")
        balance = _format_amount(pot.get("balance", 0), currency)
        goal = ""
        if pot.get("goal_amount"):
            goal_amount = _format_amount(pot["goal_amount"], currency)
            goal = f" (goal: {goal_amount})"
        status = ""
        if pot.get("locked"):
            status = " [LOCKED]"
        round_up = ""
        if pot.get("round_up_multiplier"):
            round_up = f" [Round-up: {pot['round_up_multiplier']}x]"

        lines.append(
            f"- {pot.get('name', 'Unnamed')}: {balance}{goal}{status}{round_up}\n"
            f"  Style: {pot.get('style', 'N/A')}\n"
            f"  ID: {pot['id']}"
        )
    return f"Found {len(pots)} active pot(s):\n\n" + "\n\n".join(lines)


async def spending_summary(
    account_id: str | None = None,
    account_name: str | None = None,
    since: str | None = None,
    before: str | None = None,
) -> str:
    """Get a spending summary grouped by category for a date range.

    Returns total spend per category, transaction count, and overall totals.
    Excludes internal pot transfers and focuses on real spending.

    Args:
        account_id: The account ID (starts with acc_). Optional if account_name is provided.
        account_name: Friendly name, e.g. "flex", "joint", "loan". Defaults to current account.
        since: Start of period. Accepts date (2024-01-01) or month (2024-01).
        before: End of period. Same formats as since.
    """
    try:
        account_id = await _resolve_account(account_id, account_name)
    except ValueError as e:
        return f"Error: {e}"
    transactions = await _fetch_filtered_transactions(account_id, since, before)
    if isinstance(transactions, str):
        return transactions

    if not transactions:
        return "No transactions found for the given criteria."

    # Group by category — only track top 3 per category
    by_category: dict[str, dict] = defaultdict(lambda: {"total": 0, "count": 0, "top": []})
    total_income = 0
    total_spend = 0

    for tx in transactions:
        category = tx.get("category", "unknown")
        amount = tx.get("amount", 0)
        name = _tx_display_name(tx)

        by_category[category]["total"] += amount
        by_category[category]["count"] += 1
        # Maintain only the top 3 largest spends per category
        top = by_category[category]["top"]
        top.append((name, amount))
        if len(top) > 3:
            top.sort(key=lambda x: x[1])
            by_category[category]["top"] = top[:3]

        if amount > 0:
            total_income += amount
        else:
            total_spend += amount

    currency = transactions[0].get("currency", "GBP")

    # Sort categories by total spend (most spent first)
    sorted_cats = sorted(by_category.items(), key=lambda x: x[1]["total"])

    lines = ["Spending Summary", "=" * 40]

    for cat, data in sorted_cats:
        cat_label = cat.replace("_", " ").title()
        cat_amount = _format_amount(data["total"], currency)
        lines.append(f"\n{cat_label}: {cat_amount} ({data['count']} transaction(s))")
        top_items = sorted(data["top"], key=lambda x: x[1])
        for name, amt in top_items:
            lines.append(f"  - {name}: {_format_amount(amt, currency)}")

    lines.append(f"\n{'=' * 40}")
    lines.append(f"Total spend: {_format_amount(total_spend, currency)}")
    lines.append(f"Total income: {_format_amount(total_income, currency)}")
    lines.append(f"Net: {_format_amount(total_spend + total_income, currency)}")
    lines.append(f"Transactions: {len(transactions)}")

    return "\n".join(lines)


async def search_transactions(
    query: str,
    account_id: str | None = None,
    account_name: str | None = None,
    since: str | None = None,
    before: str | None = None,
    transaction_type: str = "all",
) -> str:
    """Search transactions by merchant name, description, or notes.

    Paginates through all transactions in the date range and returns matches.
    The search is case-insensitive and matches partial strings.

    Args:
        query: Search term to match against merchant name, description, or notes.
        account_id: The account ID (starts with acc_). Optional if account_name is provided.
        account_name: Friendly name, e.g. "flex", "joint", "loan". Defaults to current account.
        since: Start of search period. Accepts RFC 3339, date (2024-01-01), or month (2024-01).
        before: End of search period. Same formats as since.
        transaction_type: Filter by direction: "income", "spending", or "all" (default).
    """
    try:
        account_id = await _resolve_account(account_id, account_name)
    except ValueError as e:
        return f"Error: {e}"
    transactions = await _fetch_filtered_transactions(account_id, since, before, exclude_zero=False)
    if isinstance(transactions, str):
        return transactions

    query_lower = query.lower()

    matches = []
    for tx in transactions:
        name = _tx_display_name(tx).lower()
        description = tx.get("description", "").lower()
        notes = tx.get("notes", "").lower()
        if query_lower in name or query_lower in description or query_lower in notes:
            matches.append(tx)

    try:
        matches = _filter_by_transaction_type(matches, transaction_type)
    except ValueError as e:
        return f"Error: {e}"

    if not matches:
        return f"No transactions matching '{query}' found."

    total = sum(tx.get("amount", 0) for tx in matches)
    currency = matches[0].get("currency", "GBP")
    lines = [_format_tx_line(tx) for tx in matches]

    header = (
        f"Found {len(matches)} transaction(s) matching '{query}':\n"
        f"Total: {_format_amount(total, currency)}\n"
    )
    return header + "\n" + "\n".join(lines)


async def recurring_payments(
    account_id: str | None = None,
    account_name: str | None = None,
    since: str | None = None,
    before: str | None = None,
) -> str:
    """Detect recurring payments and subscriptions by finding merchants that appear multiple times.

    Analyses transactions in the date range and groups by merchant, showing
    merchants with 2+ transactions. Useful for identifying subscriptions,
    direct debits, and regular spending habits.

    Args:
        account_id: The account ID (starts with acc_). Optional if account_name is provided.
        account_name: Friendly name, e.g. "flex", "joint", "loan". Defaults to current account.
        since: Start of analysis period. Accepts RFC 3339, date (2024-01-01), or month (2024-01).
        before: End of analysis period. Same formats as since.
    """
    try:
        account_id = await _resolve_account(account_id, account_name)
    except ValueError as e:
        return f"Error: {e}"
    transactions = await _fetch_filtered_transactions(account_id, since, before)
    if isinstance(transactions, str):
        return transactions

    if not transactions:
        return "No transactions found for the given criteria."

    # Group by merchant name
    by_merchant: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "count": 0, "dates": [], "category": ""}
    )

    for tx in transactions:
        name = _tx_display_name(tx)
        amount = tx.get("amount", 0)
        created = tx.get("created", "")[:10]

        by_merchant[name]["total"] += amount
        by_merchant[name]["count"] += 1
        by_merchant[name]["dates"].append(created)
        by_merchant[name]["category"] = tx.get("category", "unknown")

    # Filter to merchants with 2+ transactions (recurring)
    recurring = {k: v for k, v in by_merchant.items() if v["count"] >= 2}

    if not recurring:
        return "No recurring payments detected in the given period."

    currency = transactions[0].get("currency", "GBP")

    # Sort by total spend (most spent first)
    sorted_merchants = sorted(recurring.items(), key=lambda x: x[1]["total"])

    lines = ["Recurring Payments", "=" * 40]

    for name, data in sorted_merchants:
        cat_label = data["category"].replace("_", " ").title()
        total = _format_amount(data["total"], currency)
        avg_amount = round(data["total"] / data["count"])
        avg = _format_amount(avg_amount, currency)
        dates = ", ".join(data["dates"][:5])
        if len(data["dates"]) > 5:
            dates += f" (+{len(data['dates']) - 5} more)"

        lines.append(
            f"\n{name} ({cat_label})\n"
            f"  Transactions: {data['count']} | Total: {total} | Avg: {avg}\n"
            f"  Dates: {dates}"
        )

    lines.append(f"\n{'=' * 40}")
    lines.append(f"Found {len(recurring)} recurring merchant(s)")

    return "\n".join(lines)


async def annotate_transaction(
    transaction_id: str,
    key: str,
    value: str,
) -> str:
    """Add a metadata annotation to a transaction. Useful for tagging or adding notes.

    Args:
        transaction_id: The transaction ID (starts with tx_).
        key: The metadata key. Allowed keys: "notes", "flagged", "is_subscription".
        value: The metadata value to set. Use empty string to clear.
    """
    if key not in _ALLOWED_METADATA_KEYS:
        allowed = ", ".join(sorted(_ALLOWED_METADATA_KEYS))
        return f"Error: key '{key}' is not allowed. Use one of: {allowed}"

    result = await _get_client().annotate_transaction(transaction_id, key, value)
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Successfully annotated transaction {transaction_id}: {key}={value}"


# ---------------------------------------------------------------------------
# All tool functions to register on the FastMCP instance
# ---------------------------------------------------------------------------

_TOOL_FUNCTIONS = [
    whoami,
    list_accounts,
    get_balance,
    list_transactions,
    get_transaction,
    list_pots,
    spending_summary,
    search_transactions,
    recurring_payments,
    annotate_transaction,
]


def _is_http_mode() -> bool:
    """Check if we have the env vars needed for multi-user HTTP mode."""
    return bool(
        os.environ.get("MONZO_CLIENT_ID")
        and os.environ.get("MONZO_CLIENT_SECRET")
        and os.environ.get("CORAL_CALLBACK_URL")
    )


def create_mcp_server() -> FastMCP:
    """Create and configure the FastMCP server instance.

    In HTTP mode (MONZO_CLIENT_ID, MONZO_CLIENT_SECRET, and CORAL_CALLBACK_URL set):
        - Enables full OAuth 2.0 authorization server via CoralAuthProvider
        - Adds Monzo OAuth callback route (/auth/callback)

    In stdio mode (default):
        - No authentication, single-user (existing behaviour)
    """
    global _user_store, _auth_provider, _oauth_store

    if _is_http_mode():
        _user_store = UserStore()
        _oauth_store = OAuthStore()
        _auth_provider = CoralAuthProvider(_oauth_store, _user_store)

        callback_url = os.environ["CORAL_CALLBACK_URL"]
        # Derive the server's base URL from the callback URL
        parsed = urllib.parse.urlparse(callback_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        # resource_server_url must include the MCP endpoint path (/mcp)
        resource_url = f"{base_url}/mcp"

        mcp = FastMCP(
            "coral",
            host="0.0.0.0",
            port=8080,
            auth_server_provider=_auth_provider,
            auth=AuthSettings(
                issuer_url=base_url,  # type: ignore[arg-type]
                resource_server_url=resource_url,  # type: ignore[arg-type]
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=["monzo"],
                    default_scopes=["monzo"],
                ),
                revocation_options=RevocationOptions(enabled=True),
            ),
        )
        _register_auth_routes(mcp)
    else:
        mcp = FastMCP("coral", host="0.0.0.0", port=8080)

    # Register all tools
    for fn in _TOOL_FUNCTIONS:
        mcp.tool()(fn)

    return mcp


def _register_auth_routes(mcp: FastMCP) -> None:
    """Register the Monzo OAuth callback route on the FastMCP server."""

    @mcp.custom_route("/auth/callback", methods=["GET"])
    async def auth_callback(request: Request) -> Response:
        """Handle Monzo OAuth callback: exchange code, find/create user, redirect to MCP client."""
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")

        assert _oauth_store is not None
        assert _auth_provider is not None
        assert _user_store is not None

        flow = await _oauth_store.get_pending_flow(state)
        if flow is None:
            return HTMLResponse(
                "<h1>Error</h1><p>Invalid or expired OAuth state.</p>",
                status_code=400,
            )
        await _oauth_store.delete_pending_flow(state)

        if not code:
            return HTMLResponse(
                "<h1>Error</h1><p>No authorization code received from Monzo.</p>",
                status_code=400,
            )

        # Exchange Monzo code for tokens
        monzo_client_id = os.environ.get("MONZO_CLIENT_ID", "")
        monzo_client_secret = os.environ.get("MONZO_CLIENT_SECRET", "")
        callback_url = os.environ.get("CORAL_CALLBACK_URL", "")

        try:
            async with httpx.AsyncClient() as http_client:
                resp = await http_client.post(
                    "https://api.monzo.com/oauth2/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": monzo_client_id,
                        "client_secret": monzo_client_secret,
                        "redirect_uri": callback_url,
                        "code": code,
                    },
                )
                resp.raise_for_status()
                token_data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("Monzo token exchange failed: HTTP %s", e.response.status_code)
            return HTMLResponse(
                "<h1>Error</h1><p>Failed to exchange Monzo authorization code.</p>",
                status_code=502,
            )
        except httpx.RequestError as e:
            logger.error("Monzo token exchange request failed: %s", e)
            return HTMLResponse(
                "<h1>Error</h1><p>Failed to connect to Monzo API.</p>",
                status_code=502,
            )

        monzo_access = token_data.get("access_token", "")
        monzo_refresh = token_data.get("refresh_token", "")

        if not monzo_access:
            return HTMLResponse(
                "<h1>Error</h1><p>No access token in Monzo response.</p>",
                status_code=502,
            )

        # Get the Monzo user ID via /ping/whoami
        try:
            async with httpx.AsyncClient() as http_client:
                whoami_resp = await http_client.get(
                    "https://api.monzo.com/ping/whoami",
                    headers={"Authorization": f"Bearer {monzo_access}"},
                )
                whoami_resp.raise_for_status()
                monzo_user_id = whoami_resp.json().get("user_id", "")
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("Monzo whoami failed: %s", e)
            return HTMLResponse(
                "<h1>Error</h1><p>Failed to identify Monzo user.</p>",
                status_code=502,
            )

        if not monzo_user_id:
            return HTMLResponse(
                "<h1>Error</h1><p>Could not determine Monzo user ID.</p>",
                status_code=502,
            )

        # Create or find the coral-bot user
        user_id = _user_store.create_or_find_user(monzo_access, monzo_refresh, monzo_user_id)

        # Evict any cached MonzoClient so the next request uses fresh tokens
        _clients.pop(user_id, None)
        _account_caches.pop(user_id, None)

        # Generate our authorization code for the MCP client
        our_code = await _auth_provider.create_authorization_code(
            client_id=flow.mcp_client_id,
            user_id=user_id,
            scopes=flow.scopes,
            code_challenge=flow.code_challenge,
            redirect_uri=flow.redirect_uri,
            redirect_uri_provided_explicitly=flow.redirect_uri_provided_explicitly,
            resource=flow.resource,
        )

        # Redirect back to the MCP client's redirect_uri with our code
        params = {"code": our_code}
        if flow.state:
            params["state"] = flow.state
        redirect_url = flow.redirect_uri
        separator = "&" if "?" in redirect_url else "?"
        redirect_url += separator + urllib.parse.urlencode(params)

        return RedirectResponse(redirect_url)


# ---------------------------------------------------------------------------
# ASGI middleware to fix trailing-slash issuer URLs (RFC 8414 compliance)
# ---------------------------------------------------------------------------

# Pydantic's AnyHttpUrl normalises bare hostnames by appending "/", but
# RFC 8414 §2 says issuers "MUST NOT" use URLs with a trailing "/".
# The MCP SDK passes the issuer through AnyHttpUrl, so the well-known
# metadata responses contain e.g. "https://host/" instead of "https://host".
# This middleware rewrites those responses on the fly.


class _FixIssuerSlash:
    """Strip trailing slashes from ``issuer`` and ``authorization_servers``."""

    def __init__(self, app, paths: set[str]) -> None:
        self._app = app
        self._paths = paths

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] not in self._paths:
            return await self._app(scope, receive, send)

        status: int = 200
        headers: list[tuple[bytes, bytes]] = []
        body_parts: list[bytes] = []

        async def _capture(message: dict) -> None:
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    body = b"".join(body_parts)
                    if status == 200:
                        body = _patch_oauth_metadata(body)
                    hdrs = [
                        (k, str(len(body)).encode()) if k == b"content-length" else (k, v)
                        for k, v in headers
                    ]
                    await send({"type": "http.response.start", "status": status, "headers": hdrs})
                    await send({"type": "http.response.body", "body": body})

        await self._app(scope, receive, _capture)


def _patch_oauth_metadata(body: bytes) -> bytes:
    try:
        data = json.loads(body)
        changed = False

        # Fix trailing slash on issuer (RFC 8414 compliance)
        if isinstance(data.get("issuer"), str) and data["issuer"].endswith("/"):
            data["issuer"] = data["issuer"].rstrip("/")
            changed = True

        # Fix trailing slash on authorization_servers (PRM)
        servers = data.get("authorization_servers")
        if isinstance(servers, list):
            data["authorization_servers"] = [
                s.rstrip("/") if isinstance(s, str) and s.endswith("/") else s for s in servers
            ]
            changed = True

        # Advertise "none" token auth method for public clients (e.g. Claude Desktop).
        # The MCP SDK hardcodes ["client_secret_post", "client_secret_basic"] but the
        # runtime *does* support "none" — we just need to advertise it in metadata.
        for key in (
            "token_endpoint_auth_methods_supported",
            "revocation_endpoint_auth_methods_supported",
        ):
            methods = data.get(key)
            if isinstance(methods, list) and "none" not in methods:
                data[key] = [*methods, "none"]
                changed = True

        return json.dumps(data).encode() if changed else body
    except (json.JSONDecodeError, TypeError):
        return body


# Create the module-level mcp instance for backwards compatibility with tests
# that import `mcp` directly. In production, main() creates a fresh instance.
mcp = create_mcp_server()


def main():
    """Run the Monzo MCP server.

    Transport is configured via the MCP_TRANSPORT environment variable:
        - "stdio" (default) — for local use with Claude Code
        - "streamable-http" — for remote/container deployment
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "sse", "streamable-http"):
        logger.error("Invalid MCP_TRANSPORT: %s", transport)
        sys.exit(1)
    logger.info("Starting coral-bot with %s transport", transport)

    if transport == "streamable-http":
        import uvicorn

        app = mcp.streamable_http_app()

        # Determine which paths need the issuer fix
        paths = {"/.well-known/oauth-authorization-server"}
        if _is_http_mode():
            callback_url = os.environ["CORAL_CALLBACK_URL"]
            parsed = urllib.parse.urlparse(callback_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            resource_url = f"{base_url}/mcp"
            # PRM path is /.well-known/oauth-protected-resource + resource path
            prm_parsed = urllib.parse.urlparse(resource_url)
            prm_path = prm_parsed.path if prm_parsed.path != "/" else ""
            paths.add(f"/.well-known/oauth-protected-resource{prm_path}")

        app = _FixIssuerSlash(app, paths)
        config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
        server = uvicorn.Server(config)
        import anyio

        anyio.run(server.serve)
    else:
        mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
