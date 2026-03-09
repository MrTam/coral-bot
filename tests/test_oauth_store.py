"""Tests for OAuthStore in coral_bot.oauth_store."""

import dataclasses
import time

import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from coral_bot.oauth_store import (
    PENDING_FLOW_TTL,
    CoralAccessToken,
    CoralAuthorizationCode,
    CoralRefreshToken,
    OAuthStore,
    PendingFlow,
)


@pytest.fixture
def store(tmp_path):
    """Create an OAuthStore using a temporary directory."""
    return OAuthStore(base_dir=str(tmp_path / "oauth"))


def _make_client_info(client_id: str = "test-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="test-secret",
        redirect_uris=[AnyUrl("http://localhost:3000/callback")],
    )


def _make_pending_flow(**overrides) -> PendingFlow:
    defaults = {
        "mcp_client_id": "test-client",
        "redirect_uri": "http://localhost:3000/callback",
        "code_challenge": "challenge123",
        "state": "original-state",
        "scopes": ["monzo"],
        "redirect_uri_provided_explicitly": True,
    }
    defaults.update(overrides)
    return PendingFlow(**defaults)  # type: ignore[arg-type]


class TestClients:
    async def test_save_and_get(self, store):
        client = _make_client_info("my-client")
        await store.save_client(client)
        loaded = await store.get_client("my-client")
        assert loaded is not None
        assert loaded.client_id == "my-client"
        assert loaded.client_secret == "test-secret"

    async def test_get_nonexistent(self, store):
        assert await store.get_client("no-such-client") is None


class TestPendingFlows:
    async def test_save_and_get(self, store):
        flow = _make_pending_flow()
        await store.save_pending_flow("monzo-state-1", flow)
        loaded = await store.get_pending_flow("monzo-state-1")
        assert loaded is not None
        assert loaded.mcp_client_id == "test-client"
        assert loaded.redirect_uri == "http://localhost:3000/callback"
        assert loaded.code_challenge == "challenge123"
        assert loaded.state == "original-state"
        assert loaded.scopes == ["monzo"]

    async def test_get_nonexistent(self, store):
        assert await store.get_pending_flow("no-such-state") is None

    async def test_delete(self, store):
        flow = _make_pending_flow()
        await store.save_pending_flow("state-to-delete", flow)
        await store.delete_pending_flow("state-to-delete")
        assert await store.get_pending_flow("state-to-delete") is None

    async def test_delete_nonexistent(self, store):
        # Should not raise
        await store.delete_pending_flow("no-such-state")

    async def test_expired_flow_returns_none(self, store):
        flow = _make_pending_flow(created_at=time.time() - PENDING_FLOW_TTL - 1)
        await store.save_pending_flow("expired-state", flow)
        assert await store.get_pending_flow("expired-state") is None


class TestAuthCodes:
    async def test_save_and_load(self, store):
        code = CoralAuthorizationCode(
            code="raw-code-123",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=time.time() + 600,
            code_challenge="challenge",
            redirect_uri=AnyUrl("http://localhost:3000/callback"),
            redirect_uri_provided_explicitly=True,
            user_id="user-1",
        )
        await store.save_auth_code("raw-code-123", code)
        loaded = await store.load_auth_code("raw-code-123")
        assert loaded is not None
        assert loaded.client_id == "test-client"
        assert loaded.user_id == "user-1"

    async def test_load_nonexistent(self, store):
        assert await store.load_auth_code("no-such-code") is None

    async def test_expired_code_returns_none(self, store):
        code = CoralAuthorizationCode(
            code="expired-code",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=time.time() - 1,
            code_challenge="challenge",
            redirect_uri=AnyUrl("http://localhost:3000/callback"),
            redirect_uri_provided_explicitly=True,
            user_id="user-1",
        )
        await store.save_auth_code("expired-code", code)
        assert await store.load_auth_code("expired-code") is None

    async def test_delete(self, store):
        code = CoralAuthorizationCode(
            code="to-delete",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=time.time() + 600,
            code_challenge="challenge",
            redirect_uri=AnyUrl("http://localhost:3000/callback"),
            redirect_uri_provided_explicitly=True,
            user_id="user-1",
        )
        await store.save_auth_code("to-delete", code)
        await store.delete_auth_code("to-delete")
        assert await store.load_auth_code("to-delete") is None


class TestAccessTokens:
    async def test_save_and_load(self, store):
        token = CoralAccessToken(
            token="raw-access-tok",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=int(time.time()) + 3600,
            user_id="user-1",
        )
        await store.save_access_token("raw-access-tok", token)
        loaded = await store.load_access_token("raw-access-tok")
        assert loaded is not None
        assert loaded.client_id == "test-client"
        assert loaded.user_id == "user-1"

    async def test_load_nonexistent(self, store):
        assert await store.load_access_token("no-such-token") is None

    async def test_expired_returns_none(self, store):
        token = CoralAccessToken(
            token="expired-tok",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=int(time.time()) - 1,
            user_id="user-1",
        )
        await store.save_access_token("expired-tok", token)
        assert await store.load_access_token("expired-tok") is None

    async def test_delete(self, store):
        token = CoralAccessToken(
            token="to-delete",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=int(time.time()) + 3600,
            user_id="user-1",
        )
        await store.save_access_token("to-delete", token)
        await store.delete_access_token("to-delete")
        assert await store.load_access_token("to-delete") is None

    async def test_no_expiry_never_expires(self, store):
        token = CoralAccessToken(
            token="no-expiry",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=None,
            user_id="user-1",
        )
        await store.save_access_token("no-expiry", token)
        loaded = await store.load_access_token("no-expiry")
        assert loaded is not None


class TestRefreshTokens:
    async def test_save_and_load(self, store):
        token = CoralRefreshToken(
            token="raw-refresh-tok",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=int(time.time()) + 86400,
            user_id="user-1",
        )
        await store.save_refresh_token("raw-refresh-tok", token)
        loaded = await store.load_refresh_token("raw-refresh-tok")
        assert loaded is not None
        assert loaded.user_id == "user-1"

    async def test_load_nonexistent(self, store):
        assert await store.load_refresh_token("no-such-token") is None

    async def test_expired_returns_none(self, store):
        token = CoralRefreshToken(
            token="expired-refresh",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=int(time.time()) - 1,
            user_id="user-1",
        )
        await store.save_refresh_token("expired-refresh", token)
        assert await store.load_refresh_token("expired-refresh") is None

    async def test_delete(self, store):
        token = CoralRefreshToken(
            token="to-delete",
            client_id="test-client",
            scopes=["monzo"],
            expires_at=int(time.time()) + 86400,
            user_id="user-1",
        )
        await store.save_refresh_token("to-delete", token)
        await store.delete_refresh_token("to-delete")
        assert await store.load_refresh_token("to-delete") is None


class TestPendingFlowSerialization:
    def test_round_trip(self):
        flow = _make_pending_flow(resource="https://example.com")
        data = dataclasses.asdict(flow)
        loaded = PendingFlow(**data)
        assert loaded.mcp_client_id == flow.mcp_client_id
        assert loaded.redirect_uri == flow.redirect_uri
        assert loaded.code_challenge == flow.code_challenge
        assert loaded.state == flow.state
        assert loaded.scopes == flow.scopes
        assert loaded.resource == "https://example.com"
        assert loaded.created_at == flow.created_at

    def test_none_state(self):
        flow = _make_pending_flow(state=None)
        data = dataclasses.asdict(flow)
        loaded = PendingFlow(**data)
        assert loaded.state is None

    def test_created_at_defaults_to_now(self):
        before = time.time()
        flow = _make_pending_flow()
        after = time.time()
        assert before <= flow.created_at <= after
