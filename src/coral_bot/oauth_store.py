"""Persistent OAuth state store for the coral-bot authorization server.

Manages /data/oauth/ with subdirectories for each entity type.
Token values are stored as SHA-256 hashes; raw values are only returned to clients.
"""

import dataclasses
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger(__name__)

DEFAULT_OAUTH_DIR = "/data/oauth"

# TTLs
AUTH_CODE_TTL = 600  # 10 minutes
ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
PENDING_FLOW_TTL = 600  # 10 minutes


def _hash_token(value: str) -> str:
    """SHA-256 hash a token value for storage."""
    return hashlib.sha256(value.encode()).hexdigest()


class CoralAuthorizationCode(AuthorizationCode):
    """Authorization code with coral-bot user ID."""

    user_id: str


class CoralAccessToken(AccessToken):
    """Access token with coral-bot user ID."""

    user_id: str


class CoralRefreshToken(RefreshToken):
    """Refresh token with coral-bot user ID."""

    user_id: str


@dataclass
class PendingFlow:
    """Data for an in-flight Monzo OAuth flow."""

    mcp_client_id: str
    redirect_uri: str
    code_challenge: str
    state: str | None
    scopes: list[str]
    redirect_uri_provided_explicitly: bool
    resource: str | None = None
    created_at: float = field(default_factory=time.time)


class OAuthStore:
    """File-based persistent store for OAuth entities.

    Directory layout::

        {base_dir}/
        ├── clients/{client_id}.json
        ├── auth_codes/{code_hash}.json
        ├── access_tokens/{hash}.json
        ├── refresh_tokens/{hash}.json
        └── pending_flows/{state}.json
    """

    def __init__(self, base_dir: str | None = None):
        self._base = Path(base_dir or DEFAULT_OAUTH_DIR)

    def _dir(self, kind: str) -> Path:
        d = self._base / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- Clients -------------------------------------------------------------

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        if client.client_id is None:
            raise ValueError("client_id is required")
        client_hash = _hash_token(client.client_id)
        path = self._dir("clients") / f"{client_hash}.json"
        path.write_text(client.model_dump_json(indent=2))
        path.chmod(0o600)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client_hash = _hash_token(client_id)
        path = self._dir("clients") / f"{client_hash}.json"
        if not path.exists():
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    # -- Pending flows -------------------------------------------------------

    async def save_pending_flow(self, monzo_state: str, flow: PendingFlow) -> None:
        state_hash = _hash_token(monzo_state)
        path = self._dir("pending_flows") / f"{state_hash}.json"
        path.write_text(json.dumps(dataclasses.asdict(flow), indent=2))
        path.chmod(0o600)

    async def get_pending_flow(self, monzo_state: str) -> PendingFlow | None:
        state_hash = _hash_token(monzo_state)
        path = self._dir("pending_flows") / f"{state_hash}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            flow = PendingFlow(**data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return None
        if time.time() - flow.created_at > PENDING_FLOW_TTL:
            path.unlink(missing_ok=True)
            return None
        return flow

    async def delete_pending_flow(self, monzo_state: str) -> None:
        state_hash = _hash_token(monzo_state)
        path = self._dir("pending_flows") / f"{state_hash}.json"
        if path.exists():
            path.unlink()

    # -- Authorization codes -------------------------------------------------

    async def save_auth_code(self, raw_code: str, code: CoralAuthorizationCode) -> None:
        code_hash = _hash_token(raw_code)
        path = self._dir("auth_codes") / f"{code_hash}.json"
        path.write_text(code.model_dump_json(indent=2))
        path.chmod(0o600)

    async def load_auth_code(self, raw_code: str) -> CoralAuthorizationCode | None:
        code_hash = _hash_token(raw_code)
        path = self._dir("auth_codes") / f"{code_hash}.json"
        if not path.exists():
            return None
        try:
            code = CoralAuthorizationCode.model_validate_json(path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            return None
        if code.expires_at < time.time():
            path.unlink(missing_ok=True)
            return None
        return code

    async def delete_auth_code(self, raw_code: str) -> None:
        code_hash = _hash_token(raw_code)
        path = self._dir("auth_codes") / f"{code_hash}.json"
        if path.exists():
            path.unlink()

    # -- Access tokens -------------------------------------------------------

    async def save_access_token(self, raw_token: str, token: CoralAccessToken) -> None:
        token_hash = _hash_token(raw_token)
        path = self._dir("access_tokens") / f"{token_hash}.json"
        path.write_text(token.model_dump_json(indent=2))
        path.chmod(0o600)

    async def load_access_token(self, raw_token: str) -> CoralAccessToken | None:
        token_hash = _hash_token(raw_token)
        path = self._dir("access_tokens") / f"{token_hash}.json"
        if not path.exists():
            return None
        try:
            token = CoralAccessToken.model_validate_json(path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            return None
        if token.expires_at is not None and token.expires_at < time.time():
            path.unlink(missing_ok=True)
            return None
        return token

    async def delete_access_token(self, raw_token: str) -> None:
        token_hash = _hash_token(raw_token)
        path = self._dir("access_tokens") / f"{token_hash}.json"
        if path.exists():
            path.unlink()

    # -- Refresh tokens ------------------------------------------------------

    async def save_refresh_token(self, raw_token: str, token: CoralRefreshToken) -> None:
        token_hash = _hash_token(raw_token)
        path = self._dir("refresh_tokens") / f"{token_hash}.json"
        path.write_text(token.model_dump_json(indent=2))
        path.chmod(0o600)

    async def load_refresh_token(self, raw_token: str) -> CoralRefreshToken | None:
        token_hash = _hash_token(raw_token)
        path = self._dir("refresh_tokens") / f"{token_hash}.json"
        if not path.exists():
            return None
        try:
            token = CoralRefreshToken.model_validate_json(path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            return None
        if token.expires_at is not None and token.expires_at < time.time():
            path.unlink(missing_ok=True)
            return None
        return token

    async def delete_refresh_token(self, raw_token: str) -> None:
        token_hash = _hash_token(raw_token)
        path = self._dir("refresh_tokens") / f"{token_hash}.json"
        if path.exists():
            path.unlink()
