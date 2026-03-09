"""OAuth 2.0 Authorization Server provider for coral-bot.

Implements the full OAuthAuthorizationServerProvider protocol so that
MCP clients (e.g. Claude Desktop) can authenticate via a standard
OAuth 2.0 flow that chains through Monzo OAuth.
"""

import logging
import os
import secrets
import time
import urllib.parse

from mcp.server.auth.provider import (
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from coral_bot.oauth_store import (
    ACCESS_TOKEN_TTL,
    AUTH_CODE_TTL,
    REFRESH_TOKEN_TTL,
    CoralAccessToken,
    CoralAuthorizationCode,
    CoralRefreshToken,
    OAuthStore,
    PendingFlow,
)
from coral_bot.users import UserStore

logger = logging.getLogger(__name__)


def _require_client_id(client: OAuthClientInformationFull) -> str:
    """Extract client_id, raising if None (should never happen for registered clients)."""
    cid = client.client_id
    if cid is None:
        raise ValueError("client_id is required")
    return cid


class CoralAuthProvider(
    OAuthAuthorizationServerProvider[CoralAuthorizationCode, CoralRefreshToken, CoralAccessToken]
):
    """Full OAuth authorization server for coral-bot.

    Handles dynamic client registration, authorization code flow with PKCE,
    token refresh, and revocation.  The ``authorize()`` method chains through
    Monzo OAuth in a two-tier flow.
    """

    def __init__(self, oauth_store: OAuthStore, user_store: UserStore):
        self._store = oauth_store
        self._users = user_store

    # -- Client management ---------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._store.save_client(client_info)

    # -- Authorization -------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Start the two-tier OAuth flow.

        Stores the MCP client's original params as a pending flow, then
        returns a redirect URL to Monzo's authorization page.
        """
        monzo_state = secrets.token_urlsafe(32)
        client_id = _require_client_id(client)

        flow = PendingFlow(
            mcp_client_id=client_id,
            redirect_uri=str(params.redirect_uri),
            code_challenge=params.code_challenge,
            state=params.state,
            scopes=params.scopes or [],
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=getattr(params, "resource", None),
        )
        await self._store.save_pending_flow(monzo_state, flow)

        monzo_client_id = os.environ.get("MONZO_CLIENT_ID", "")
        callback_url = os.environ.get("CORAL_CALLBACK_URL", "")

        monzo_params = urllib.parse.urlencode(
            {
                "client_id": monzo_client_id,
                "redirect_uri": callback_url,
                "response_type": "code",
                "state": monzo_state,
            }
        )
        return f"https://auth.monzo.com/?{monzo_params}"

    # -- Authorization code exchange -----------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> CoralAuthorizationCode | None:
        code = await self._store.load_auth_code(authorization_code)
        if code is None:
            return None
        # NOTE: We intentionally do NOT enforce client_id match here.
        # Claude Desktop may re-register between authorize and token exchange
        # (different backend processes), yielding a new client_id. PKCE
        # (code_challenge/code_verifier) already prevents authorization code
        # theft, so the client_id check is redundant for security.
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: CoralAuthorizationCode,
    ) -> OAuthToken:
        await self._store.delete_auth_code(authorization_code.code)

        client_id = _require_client_id(client)
        raw_access = secrets.token_urlsafe(32)
        raw_refresh = secrets.token_urlsafe(32)
        now = int(time.time())

        access_token = CoralAccessToken(
            token=raw_access,
            client_id=client_id,
            scopes=authorization_code.scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            user_id=authorization_code.user_id,
        )
        refresh_token = CoralRefreshToken(
            token=raw_refresh,
            client_id=client_id,
            scopes=authorization_code.scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            user_id=authorization_code.user_id,
        )

        await self._store.save_access_token(raw_access, access_token)
        await self._store.save_refresh_token(raw_refresh, refresh_token)

        return OAuthToken(
            access_token=raw_access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes),
            refresh_token=raw_refresh,
        )

    # -- Refresh token exchange ----------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> CoralRefreshToken | None:
        token = await self._store.load_refresh_token(refresh_token)
        if token is None:
            return None
        if token.client_id != client.client_id:
            logger.warning(
                "Refresh token client_id mismatch: expected %s, got %s",
                client.client_id,
                token.client_id,
            )
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: CoralRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        await self._store.delete_refresh_token(refresh_token.token)

        client_id = _require_client_id(client)
        effective_scopes = scopes if scopes else refresh_token.scopes
        raw_access = secrets.token_urlsafe(32)
        raw_refresh = secrets.token_urlsafe(32)
        now = int(time.time())

        new_access = CoralAccessToken(
            token=raw_access,
            client_id=client_id,
            scopes=effective_scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            user_id=refresh_token.user_id,
        )
        new_refresh = CoralRefreshToken(
            token=raw_refresh,
            client_id=client_id,
            scopes=effective_scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            user_id=refresh_token.user_id,
        )

        await self._store.save_access_token(raw_access, new_access)
        await self._store.save_refresh_token(raw_refresh, new_refresh)

        return OAuthToken(
            access_token=raw_access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(effective_scopes),
            refresh_token=raw_refresh,
        )

    # -- Access token verification -------------------------------------------

    async def load_access_token(self, token: str) -> CoralAccessToken | None:
        return await self._store.load_access_token(token)

    # -- Revocation ----------------------------------------------------------

    async def revoke_token(
        self,
        token: CoralAccessToken | CoralRefreshToken,
    ) -> None:
        if isinstance(token, CoralAccessToken):
            await self._store.delete_access_token(token.token)
        elif isinstance(token, CoralRefreshToken):
            await self._store.delete_refresh_token(token.token)

    # -- Helper for auth callback --------------------------------------------

    async def create_authorization_code(
        self,
        *,
        client_id: str,
        user_id: str,
        scopes: list[str],
        code_challenge: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        resource: str | None = None,
    ) -> str:
        """Create and persist a new authorization code. Returns the raw code."""
        raw_code = secrets.token_urlsafe(32)
        now = time.time()

        code = CoralAuthorizationCode(
            code=raw_code,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + AUTH_CODE_TTL,
            code_challenge=code_challenge,
            redirect_uri=AnyUrl(redirect_uri),
            redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
            user_id=user_id,
            resource=resource,
        )
        await self._store.save_auth_code(raw_code, code)
        return raw_code
