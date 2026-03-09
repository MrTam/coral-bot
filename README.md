# coral-bot

A Python [MCP](https://modelcontextprotocol.io/) server that wraps the [Monzo](https://monzo.com/) banking API, enabling AI agents to analyse your bank account — search transactions, review spending, check savings pots, and more.

Works with **Claude Code** (stdio, single-user) and **Claude Desktop** (HTTP, multi-user with full OAuth 2.0).

## Features

- **Transaction search** — find transactions by merchant, description, or notes
- **Spending summaries** — aggregated spending by category over any date range
- **Recurring payment detection** — identify subscriptions and regular payments
- **Balance & accounts** — check balances across accounts and pots
- **Transaction annotations** — add notes and metadata to transactions
- **Multi-user OAuth** — full OAuth 2.0 authorization server with PKCE, dynamic client registration, token refresh, and revocation

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- A [Monzo developer account](https://developers.monzo.com/) with a registered OAuth app

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Register a Monzo OAuth app

Register at [developers.monzo.com](https://developers.monzo.com/). Note your client ID and client secret.

### Stdio mode (Claude Code)

For local single-user use:

```bash
# Authenticate with Monzo
export MONZO_CLIENT_ID="your_client_id"
export MONZO_CLIENT_SECRET="your_client_secret"
uv run python scripts/auth.py

# Set the access token
export MONZO_ACCESS_TOKEN="your_token_here"
```

Launch Claude Code from the project directory — the `.mcp.json` config will load the tools automatically.

### HTTP mode (Claude Desktop / multi-user)

For deployment as a remote MCP server with full OAuth 2.0:

```bash
export MONZO_CLIENT_ID="your_client_id"
export MONZO_CLIENT_SECRET="your_client_secret"
export CORAL_CALLBACK_URL="https://your-domain.com/auth/callback"
export MCP_TRANSPORT="streamable-http"

uv run python -m coral_bot.server
```

Or use Docker:

```bash
docker compose up
```

MCP clients (like Claude Desktop) connect and authenticate automatically via the OAuth flow — no manual token management needed.

#### How it works

coral-bot acts as both an **OAuth authorization server** (for MCP clients) and an **OAuth client** (to Monzo):

1. MCP client registers itself via `/register` (dynamic client registration)
2. MCP client redirects user to `/authorize` → coral-bot redirects to Monzo login
3. User approves in Monzo app → Monzo redirects back to `/auth/callback`
4. coral-bot exchanges the Monzo code for tokens, identifies the user, and redirects back to the MCP client with its own authorization code
5. MCP client exchanges the code at `/token` for coral-bot access/refresh tokens
6. All subsequent MCP requests use the coral-bot Bearer token

Re-authenticating the same Monzo account reuses the existing user (deduplication by Monzo user ID).

## Available tools

| Tool | Description |
|------|-------------|
| `whoami` | Check authentication status |
| `list_accounts` | List all accounts |
| `get_balance` | Get account balance |
| `list_transactions` | List transactions with filtering |
| `get_transaction` | Get single transaction with merchant details |
| `list_pots` | List savings pots |
| `spending_summary` | Spending by category for a date range |
| `search_transactions` | Search by merchant, description, or notes |
| `recurring_payments` | Detect recurring merchants and subscriptions |
| `annotate_transaction` | Add notes/metadata to a transaction |

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run tests
uv run python -m pytest tests/ -v

# Lint and format
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/

# Type check
uv run ty check
```

## License

MIT
