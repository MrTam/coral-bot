# coral-bot

A Python MCP (Model Context Protocol) server that wraps the Monzo banking API, enabling AI agents (particularly Claude Desktop and Claude Code) to perform analysis on a user's bank account — searching transactions, analysing spending, reviewing savings pots, etc.

coral-bot operates in two modes:
- **Stdio mode** — single-user, no auth, for local use with Claude Code
- **HTTP mode** — multi-user with full OAuth 2.0, for deployment with Claude Desktop and other MCP clients

In HTTP mode, coral-bot is both an **OAuth authorization server** (for MCP clients) and an **OAuth client** (to Monzo), implementing a two-tier OAuth flow.

## Project structure

```
coral-bot/
├── CLAUDE.md              # This file
├── README.md              # User-facing documentation
├── .mcp.json              # MCP server config for Claude Code (stdio mode)
├── .pre-commit-config.yaml # Pre-commit hooks (ruff, ty)
├── .github/workflows/     # CI (lint + test on PRs and main)
├── pyproject.toml         # Python project config (uses uv)
├── Dockerfile             # Container build
├── docker-compose.yml     # Container orchestration for HTTP mode
├── src/
│   └── coral_bot/
│       ├── __init__.py
│       ├── server.py        # FastMCP server entry point + tool definitions + auth callback
│       ├── client.py        # Async Monzo API client (httpx)
│       ├── tokens.py        # Monzo token storage and auto-refresh
│       ├── users.py         # User store (per-user data, Monzo ID deduplication)
│       ├── oauth_store.py   # Persistent OAuth state (clients, codes, tokens, pending flows)
│       └── oauth_provider.py # OAuthAuthorizationServerProvider implementation
├── scripts/
│   └── auth.py            # OAuth helper for manual token bootstrapping
├── tests/
│   ├── conftest.py        # Shared fixtures (mock_client, state clearing)
│   ├── factories.py       # Test data factories (make_transaction, etc.)
│   ├── test_account_resolution.py
│   ├── test_client.py
│   ├── test_helpers.py
│   ├── test_oauth_store.py
│   ├── test_oauth_provider.py
│   ├── test_tokens.py
│   ├── test_tools.py
│   └── test_users.py
```

## Tech stack

- **Python 3.12+**
- **FastMCP** (`mcp[cli]` package) — MCP server framework, auto-generates tool schemas from type hints and docstrings
- **httpx** — async HTTP client for Monzo API calls
- **uv** — package manager and runner

## Key commands

```bash
# Install dependencies (including dev tools)
uv sync --all-extras

# Run the server directly (for testing)
uv run python -m coral_bot.server

# Run auth flow to get an access token
uv run python scripts/auth.py

# Run tests
uv run python -m pytest tests/ -v

# Lint and format
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/

# Type check
uv run ty check
```

## Architecture

### Two-tier OAuth flow (HTTP mode)

coral-bot acts as an OAuth authorization server for MCP clients and an OAuth client to Monzo:

```
MCP Client (Claude Desktop)       coral-bot                  Monzo
─────────────────────────────────────────────────────────────────────
1. GET /authorize              →
   (client_id, redirect_uri,
    code_challenge, state)
                                   Save pending flow
                                ←  302 → auth.monzo.com
                                                          →  Monzo login
                                                          ←  Approve in app
2.                                 GET /auth/callback     ←  (monzo code, state)
                                   Exchange monzo code for tokens
                                   GET /ping/whoami → monzo_user_id
                                   Create/find user (dedup by monzo_user_id)
                                   Generate our auth code
                                →  302 → client redirect_uri
                                        (our code, original state)
3. POST /token                 →
   (our code, code_verifier)
                                   Verify PKCE, exchange code
                                ←  { access_token, refresh_token }
4. MCP requests with Bearer    →   load_access_token → user_id → MonzoClient
```

### Module responsibilities

| Module | Responsibility |
|--------|---------------|
| `server.py` | FastMCP server, tool definitions, auth callback route, user ID resolution |
| `client.py` | Async Monzo API client (httpx), pagination, input validation |
| `tokens.py` | Per-user Monzo token persistence and auto-refresh |
| `users.py` | User store — creates/deduplicates users by Monzo user ID, maps to token files |
| `oauth_store.py` | File-based persistence for OAuth entities (clients, auth codes, access/refresh tokens, pending flows) |
| `oauth_provider.py` | `OAuthAuthorizationServerProvider` implementation — the 9 methods FastMCP needs for a full OAuth server |

### Data layout (HTTP mode)

```
/data/
├── users/{user_id}.json          # User metadata (monzo_user_id, token_file path)
├── tokens/{user_id}.json         # Monzo access/refresh tokens (managed by TokenManager)
└── oauth/
    ├── clients/{client_id}.json  # Registered MCP clients
    ├── auth_codes/{hash}.json    # Authorization codes (10 min TTL)
    ├── access_tokens/{hash}.json # Access tokens (1 hour TTL)
    ├── refresh_tokens/{hash}.json # Refresh tokens (30 day TTL)
    └── pending_flows/{state}.json # In-flight Monzo OAuth flows
```

Token values are stored as SHA-256 hashes; raw values are only returned to clients.

## MCP server configuration

### Stdio mode (Claude Code)

Configured in `.mcp.json`. Set `MONZO_ACCESS_TOKEN` before launching Claude Code.

### HTTP mode (Claude Desktop / multi-user)

Requires three environment variables:
- `MONZO_CLIENT_ID` — Monzo OAuth app client ID
- `MONZO_CLIENT_SECRET` — Monzo OAuth app client secret
- `CORAL_CALLBACK_URL` — public URL to `/auth/callback` (e.g. `https://coral.example.com/auth/callback`)

Also set `MCP_TRANSPORT=streamable-http`.

FastMCP auto-generates these endpoints:
- `/.well-known/oauth-authorization-server` — OAuth metadata
- `/authorize` — authorization endpoint
- `/token` — token endpoint
- `/register` — dynamic client registration
- `/revoke` — token revocation

coral-bot adds:
- `/auth/callback` — Monzo OAuth callback (exchanges Monzo code, creates user, redirects to MCP client)

## Monzo API reference

Base URL: `https://api.monzo.com`

All requests require `Authorization: Bearer {access_token}` header.

### Endpoints exposed as tools

| Tool | Method | Endpoint | Description |
|------|--------|----------|-------------|
| `whoami` | GET | `/ping/whoami` | Check authentication status |
| `list_accounts` | GET | `/accounts` | List all accounts |
| `get_balance` | GET | `/balance` | Get account balance |
| `list_transactions` | GET | `/transactions` | List transactions (with filtering, excludes pot transfers by default) |
| `get_transaction` | GET | `/transactions/{id}` | Get single transaction detail with expanded merchant |
| `list_pots` | GET | `/pots` | List savings pots |
| `spending_summary` | GET | `/transactions` | Aggregated spending by category for a date range |
| `search_transactions` | GET | `/transactions` | Search transactions by merchant/description/notes (with pagination) |
| `recurring_payments` | GET | `/transactions` | Detect recurring merchants and subscriptions |
| `annotate_transaction` | PATCH | `/transactions/{id}` | Add notes/metadata to a transaction |

### Future endpoints (not yet implemented)

- `PUT /pots/{id}/deposit` — Move money into a pot
- `PUT /pots/{id}/withdraw` — Move money out of a pot
- `POST /feed` — Create feed items
- `POST /webhooks` — Register webhooks

## Authentication

### Stdio mode

Monzo uses OAuth 2.0. For local development:

1. Register an app at https://developers.monzo.com
2. Run `scripts/auth.py` which opens a browser for the OAuth redirect flow
3. Approve the login in the Monzo app (strong customer authentication)
4. The script exchanges the auth code for an access token
5. Set `MONZO_ACCESS_TOKEN` in your environment

Access tokens expire. Use the refresh token flow or re-authenticate. Monzo's API has a 5-minute window for full access after SCA approval; after that, some endpoints require re-authentication.

### HTTP mode

MCP clients authenticate via the two-tier OAuth flow described above. No manual token management needed — the flow handles everything automatically:
- Dynamic client registration (MCP client registers itself)
- PKCE-protected authorization code flow
- Token refresh with rotation
- Token revocation
- User deduplication by Monzo user ID (re-authenticating doesn't create duplicate users)

## Design principles

- **Read-only first**: Start with read-only tools for safety. A banking API should not make transfers without explicit human approval.
- **Two-tier auth separation**: MCP clients never see Monzo credentials. coral-bot issues its own tokens; Monzo tokens are managed internally per-user.
- **Hash-based token storage**: All token values are stored as SHA-256 hashes on disk. Raw values are only returned to clients.
- **Clear tool docstrings**: FastMCP uses docstrings as tool descriptions for the AI agent. Make them precise and include parameter descriptions.
- **Never log to stdout**: The MCP stdio transport uses stdout for JSON-RPC. All logging must go to stderr.
- **Graceful error handling**: Return human-readable error messages from tools, never crash the server.
- **Format responses for analysis**: Return structured, readable text that an AI agent can reason about (not raw JSON dumps).

## Code conventions

- Use `async`/`await` throughout — httpx AsyncClient for all API calls
- Type hints on all function signatures
- Docstrings in Google style (FastMCP parses `Args:` sections for parameter descriptions)
- No `print()` statements — use `logging` to stderr
- Keep the client layer (`client.py`) separate from tool definitions (`server.py`)
- Line length limit: 100 characters

## Git and workflow conventions

### Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) syntax:

- `feat:` — new features or tools
- `fix:` — bug fixes
- `refactor:` — code changes that neither fix a bug nor add a feature
- `test:` — adding or updating tests
- `ci:` — CI/CD changes
- `docs:` — documentation only
- `chore:` — dependency updates, config changes, etc.

### Before committing

1. **All tests must pass** — run `uv run python -m pytest tests/ -v` and verify no failures before creating a commit
2. **Lint and type checks must pass** — `ruff check`, `ruff format --check`, and `ty check` must all be clean (pre-commit hooks enforce this)
3. **New code must have test coverage** — any new tool, helper function, or client method must have corresponding tests. Follow existing patterns in `tests/`

### GitHub

- Use the GitHub MCP tools (prefixed `mcp__plugin_github_github__`) for all GitHub operations: creating PRs, reading issues, searching code, etc. Prefer MCP tools over the `gh` CLI
- CI runs lint, type checking, and tests on all PRs and pushes to main

## Testing conventions

- Tests live in `tests/` and follow the `test_<module>.py` naming convention
- Use the `mock_client` fixture from `conftest.py` to mock the Monzo API client
- Use factory helpers from `tests/factories.py` (`make_transaction`, `make_account`, `make_pot`) to create test data
- Group related tests in classes (e.g. `TestSearchTransactions`)
- Reset shared state (like `_account_cache`) using autouse fixtures when needed
- OAuth store and provider tests use `tmp_path` fixture with real file I/O (no mocking)
