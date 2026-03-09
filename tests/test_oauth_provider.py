"""Tests for CoralAuthProvider in coral_bot.oauth_provider."""

from unittest.mock import patch

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from coral_bot.oauth_provider import CoralAuthProvider
from coral_bot.oauth_store import (
    OAuthStore,
)
from coral_bot.users import UserStore


@pytest.fixture
def oauth_store(tmp_path):
    return OAuthStore(base_dir=str(tmp_path / "oauth"))


@pytest.fixture
def user_store(tmp_path):
    return UserStore(users_dir=str(tmp_path / "users"))


@pytest.fixture
def provider(oauth_store, user_store):
    return CoralAuthProvider(oauth_store, user_store)


def _make_client(client_id: str = "mcp-client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="secret",
        redirect_uris=[AnyUrl("http://localhost:3000/callback")],
    )


def _make_params(**overrides) -> AuthorizationParams:
    defaults = {
        "state": "abc123",
        "scopes": ["monzo"],
        "code_challenge": "challenge-value",
        "redirect_uri": AnyUrl("http://localhost:3000/callback"),
        "redirect_uri_provided_explicitly": True,
    }
    defaults.update(overrides)
    return AuthorizationParams(**defaults)  # type: ignore[arg-type]


class TestClientManagement:
    async def test_register_and_get_client(self, provider):
        client = _make_client()
        await provider.register_client(client)
        loaded = await provider.get_client("mcp-client-1")
        assert loaded is not None
        assert loaded.client_id == "mcp-client-1"

    async def test_get_nonexistent_client(self, provider):
        assert await provider.get_client("no-such-client") is None


class TestAuthorize:
    @patch.dict(
        "os.environ",
        {
            "MONZO_CLIENT_ID": "monzo-id",
            "CORAL_CALLBACK_URL": "http://coral.example.com/auth/callback",
        },
    )
    async def test_returns_monzo_redirect_url(self, provider, oauth_store):
        client = _make_client()
        params = _make_params()
        url = await provider.authorize(client, params)

        assert url.startswith("https://auth.monzo.com/")
        assert "client_id=monzo-id" in url
        assert "redirect_uri=" in url
        assert "response_type=code" in url
        # A pending flow should be saved
        # Extract state from URL
        import urllib.parse

        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        monzo_state = qs["state"][0]
        flow = await oauth_store.get_pending_flow(monzo_state)
        assert flow is not None
        assert flow.mcp_client_id == "mcp-client-1"
        assert flow.state == "abc123"
        assert flow.code_challenge == "challenge-value"


class TestAuthorizationCodeFlow:
    async def test_create_load_exchange(self, provider, oauth_store):
        client = _make_client()
        await provider.register_client(client)

        # Create an auth code
        raw_code = await provider.create_authorization_code(
            client_id="mcp-client-1",
            user_id="user-42",
            scopes=["monzo"],
            code_challenge="challenge",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        )

        # Load it
        loaded = await provider.load_authorization_code(client, raw_code)
        assert loaded is not None
        assert loaded.user_id == "user-42"
        assert loaded.client_id == "mcp-client-1"

        # Exchange it
        token = await provider.exchange_authorization_code(client, loaded)
        assert isinstance(token, OAuthToken)
        assert token.access_token
        assert token.refresh_token
        assert token.token_type == "Bearer"
        assert token.expires_in == 3600

        # Code should be consumed
        assert await provider.load_authorization_code(client, raw_code) is None

    async def test_load_code_different_client_still_returns(self, provider):
        _make_client("client-1")
        client2 = _make_client("client-2")

        raw_code = await provider.create_authorization_code(
            client_id="client-1",
            user_id="user-1",
            scopes=["monzo"],
            code_challenge="challenge",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        )
        # Client_id check is intentionally relaxed — Claude Desktop may
        # re-register between authorize and token exchange. PKCE protects
        # against authorization code theft instead.
        code = await provider.load_authorization_code(client2, raw_code)
        assert code is not None
        assert code.client_id == "client-1"
        assert code.user_id == "user-1"


class TestRefreshTokenFlow:
    async def test_exchange_refresh_token(self, provider):
        client = _make_client()
        await provider.register_client(client)

        # Create initial tokens via auth code exchange
        raw_code = await provider.create_authorization_code(
            client_id="mcp-client-1",
            user_id="user-42",
            scopes=["monzo"],
            code_challenge="challenge",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        )
        code = await provider.load_authorization_code(client, raw_code)
        assert code is not None
        initial_token = await provider.exchange_authorization_code(client, code)

        # Load refresh token
        assert initial_token.refresh_token is not None
        refresh = await provider.load_refresh_token(client, initial_token.refresh_token)
        assert refresh is not None
        assert refresh.user_id == "user-42"

        # Exchange refresh token
        new_token = await provider.exchange_refresh_token(client, refresh, [])
        assert isinstance(new_token, OAuthToken)
        assert new_token.access_token != initial_token.access_token
        assert new_token.refresh_token != initial_token.refresh_token

        # Old refresh token should be consumed
        assert await provider.load_refresh_token(client, initial_token.refresh_token) is None

    async def test_refresh_wrong_client(self, provider):
        client1 = _make_client("client-1")
        client2 = _make_client("client-2")

        raw_code = await provider.create_authorization_code(
            client_id="client-1",
            user_id="user-1",
            scopes=["monzo"],
            code_challenge="challenge",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        )
        code = await provider.load_authorization_code(client1, raw_code)
        assert code is not None
        token = await provider.exchange_authorization_code(client1, code)
        assert token.refresh_token is not None
        assert await provider.load_refresh_token(client2, token.refresh_token) is None


class TestAccessTokenVerification:
    async def test_load_valid_access_token(self, provider):
        client = _make_client()
        raw_code = await provider.create_authorization_code(
            client_id="mcp-client-1",
            user_id="user-42",
            scopes=["monzo"],
            code_challenge="challenge",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        )
        code = await provider.load_authorization_code(client, raw_code)
        assert code is not None
        token = await provider.exchange_authorization_code(client, code)

        loaded = await provider.load_access_token(token.access_token)
        assert loaded is not None
        assert loaded.user_id == "user-42"
        assert loaded.client_id == "mcp-client-1"

    async def test_load_nonexistent_access_token(self, provider):
        assert await provider.load_access_token("no-such-token") is None


class TestRevocation:
    async def test_revoke_access_token(self, provider):
        client = _make_client()
        raw_code = await provider.create_authorization_code(
            client_id="mcp-client-1",
            user_id="user-42",
            scopes=["monzo"],
            code_challenge="challenge",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        )
        code = await provider.load_authorization_code(client, raw_code)
        assert code is not None
        token = await provider.exchange_authorization_code(client, code)

        access = await provider.load_access_token(token.access_token)
        assert access is not None
        await provider.revoke_token(access)
        assert await provider.load_access_token(token.access_token) is None

    async def test_revoke_refresh_token(self, provider):
        client = _make_client()
        raw_code = await provider.create_authorization_code(
            client_id="mcp-client-1",
            user_id="user-42",
            scopes=["monzo"],
            code_challenge="challenge",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        )
        code = await provider.load_authorization_code(client, raw_code)
        assert code is not None
        token = await provider.exchange_authorization_code(client, code)
        assert token.refresh_token is not None

        refresh = await provider.load_refresh_token(client, token.refresh_token)
        assert refresh is not None
        await provider.revoke_token(refresh)
        assert await provider.load_refresh_token(client, token.refresh_token) is None
