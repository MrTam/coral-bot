"""Tests for token management in coral_bot.tokens."""

import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from coral_bot.tokens import TokenManager


@pytest.fixture
def token_file(tmp_path):
    """Return a path for a temporary token file."""
    return tmp_path / "tokens.json"


@pytest.fixture
def token_manager(token_file):
    """Create a TokenManager with a temporary file and no env vars."""
    with patch.dict("os.environ", {}, clear=True):
        tm = TokenManager(
            token_file=str(token_file),
            client_id="test_client_id",
            client_secret="test_client_secret",
        )
    return tm


class TestTokenStorage:
    def test_load_from_file(self, token_file):
        tokens = {
            "access_token": "file_token",
            "refresh_token": "file_refresh",
            "expires_at": time.time() + 3600,
        }
        token_file.write_text(json.dumps(tokens))

        tm = TokenManager(token_file=str(token_file))
        assert tm.access_token == "file_token"
        assert tm.refresh_token == "file_refresh"

    def test_load_from_env(self, token_file):
        with patch.dict(
            "os.environ",
            {"MONZO_ACCESS_TOKEN": "env_token", "MONZO_REFRESH_TOKEN": "env_refresh"},
        ):
            tm = TokenManager(token_file=str(token_file))
        assert tm.access_token == "env_token"
        assert tm.refresh_token == "env_refresh"

    def test_file_takes_precedence_over_env(self, token_file):
        tokens = {"access_token": "file_token", "refresh_token": "", "expires_at": 0}
        token_file.write_text(json.dumps(tokens))

        with patch.dict("os.environ", {"MONZO_ACCESS_TOKEN": "env_token"}):
            tm = TokenManager(token_file=str(token_file))
        assert tm.access_token == "file_token"

    def test_save_creates_file(self, token_file):
        with patch.dict("os.environ", {"MONZO_ACCESS_TOKEN": "new_token"}):
            TokenManager(token_file=str(token_file))
        assert token_file.exists()
        data = json.loads(token_file.read_text())
        assert data["access_token"] == "new_token"

    def test_no_tokens_available(self, token_file):
        with patch.dict("os.environ", {}, clear=True):
            tm = TokenManager(token_file=str(token_file))
        assert tm.access_token == ""
        assert tm.refresh_token == ""


class TestTokenExpiry:
    def test_not_expired(self, token_manager):
        token_manager._tokens = {"expires_at": time.time() + 3600}
        assert token_manager.is_expired() is False

    def test_expired(self, token_manager):
        token_manager._tokens = {"expires_at": time.time() - 100}
        assert token_manager.is_expired() is True

    def test_unknown_expiry_not_expired(self, token_manager):
        token_manager._tokens = {"expires_at": 0}
        assert token_manager.is_expired() is False

    def test_expired_within_buffer(self, token_manager):
        """Token expiring within 60s buffer is considered expired."""
        token_manager._tokens = {"expires_at": time.time() + 30}
        assert token_manager.is_expired() is True


class TestTokenRefresh:
    async def test_successful_refresh(self, token_manager):
        token_manager._tokens = {
            "access_token": "old_token",
            "refresh_token": "valid_refresh",
            "expires_at": 0,
        }

        request = httpx.Request("POST", "https://api.monzo.com/oauth2/token")
        response = httpx.Response(
            200,
            json={
                "access_token": "new_token",
                "refresh_token": "new_refresh",
                "expires_in": 3600,
            },
            request=request,
        )

        with patch("coral_bot.tokens.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await token_manager.refresh()

        assert result is True
        assert token_manager.access_token == "new_token"
        assert token_manager.refresh_token == "new_refresh"

    async def test_refresh_without_refresh_token(self, token_manager):
        token_manager._tokens = {"access_token": "old", "refresh_token": ""}
        result = await token_manager.refresh()
        assert result is False

    async def test_refresh_without_client_credentials(self, token_file):
        tm = TokenManager(token_file=str(token_file), client_id="", client_secret="")
        tm._tokens = {"refresh_token": "some_refresh"}
        result = await tm.refresh()
        assert result is False

    async def test_refresh_http_error(self, token_manager):
        token_manager._tokens = {"refresh_token": "valid_refresh", "expires_at": 0}

        request = httpx.Request("POST", "https://api.monzo.com/oauth2/token")
        response = httpx.Response(401, request=request)

        with patch("coral_bot.tokens.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await token_manager.refresh()

        assert result is False


class TestGetValidToken:
    async def test_returns_valid_token(self, token_manager):
        token_manager._tokens = {
            "access_token": "valid_token",
            "expires_at": time.time() + 3600,
        }
        token = await token_manager.get_valid_token()
        assert token == "valid_token"

    async def test_raises_when_no_token(self, token_manager):
        token_manager._tokens = {"access_token": "", "expires_at": 0}
        with pytest.raises(ValueError, match="No access token"):
            await token_manager.get_valid_token()

    async def test_refreshes_expired_token(self, token_manager):
        token_manager._tokens = {
            "access_token": "old",
            "refresh_token": "valid_refresh",
            "expires_at": time.time() - 100,
        }

        async def mock_refresh():
            token_manager._tokens["access_token"] = "refreshed"
            token_manager._tokens["expires_at"] = time.time() + 3600
            return True

        with patch.object(token_manager, "refresh", side_effect=mock_refresh):
            token = await token_manager.get_valid_token()

        assert token == "refreshed"


class TestUpdateTokens:
    def test_update_and_persist(self, token_manager, token_file):
        token_manager.update_tokens("new_access", "new_refresh", expires_in=7200)

        assert token_manager.access_token == "new_access"
        assert token_manager.refresh_token == "new_refresh"

        # Verify persisted to file
        data = json.loads(token_file.read_text())
        assert data["access_token"] == "new_access"
        assert data["refresh_token"] == "new_refresh"
