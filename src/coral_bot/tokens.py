"""Persistent token storage with automatic refresh for Monzo OAuth tokens."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.monzo.com/oauth2/token"

# Default path for persisted token file (overridable via MONZO_TOKEN_FILE)
DEFAULT_TOKEN_FILE = "/data/monzo_tokens.json"


class TokenManager:
    """Manages Monzo OAuth tokens with persistent storage and auto-refresh.

    Tokens are stored as a JSON file so they survive container restarts.
    On each access, the manager checks if the token has expired and
    refreshes it automatically if possible.

    Args:
        token_file: Path to the JSON file for persisted tokens.
        client_id: Monzo OAuth client ID.
        client_secret: Monzo OAuth client secret.
    """

    def __init__(
        self,
        token_file: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        file_path = token_file or os.environ.get("MONZO_TOKEN_FILE", DEFAULT_TOKEN_FILE)
        self._token_file = Path(file_path)
        self._client_id = client_id or os.environ.get("MONZO_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("MONZO_CLIENT_SECRET", "")
        self._tokens: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load tokens from disk, falling back to environment variables."""
        if self._token_file.exists():
            try:
                self._tokens = json.loads(self._token_file.read_text())
                logger.info("Loaded tokens from %s", self._token_file)
                return
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read token file: %s", e)

        # Fall back to environment variables for initial bootstrap
        access_token = os.environ.get("MONZO_ACCESS_TOKEN", "")
        refresh_token = os.environ.get("MONZO_REFRESH_TOKEN", "")
        if access_token:
            self._tokens = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": 0,  # Unknown expiry — will attempt refresh on 401
            }
            self._save()

    def _save(self) -> None:
        """Persist tokens to disk."""
        try:
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(json.dumps(self._tokens, indent=2))
            # Restrict permissions to owner only
            self._token_file.chmod(0o600)
            logger.info("Saved tokens to %s", self._token_file)
        except OSError as e:
            logger.warning("Failed to save token file: %s", e)

    @property
    def access_token(self) -> str:
        return self._tokens.get("access_token", "")

    @property
    def refresh_token(self) -> str:
        return self._tokens.get("refresh_token", "")

    def is_expired(self) -> bool:
        """Check if the access token has expired (with 60s buffer)."""
        expires_at = self._tokens.get("expires_at", 0)
        if expires_at == 0:
            return False  # Unknown expiry, assume valid until we get a 401
        return time.time() > (expires_at - 60)

    async def refresh(self) -> bool:
        """Attempt to refresh the access token using the refresh token.

        Returns True if refresh succeeded, False otherwise.
        """
        if not self.refresh_token:
            logger.warning("No refresh token available")
            return False

        if not self._client_id or not self._client_secret:
            logger.warning("Cannot refresh: MONZO_CLIENT_ID and MONZO_CLIENT_SECRET required")
            return False

        logger.info("Refreshing access token...")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self.refresh_token,
                    },
                )
                response.raise_for_status()
                data = response.json()

            self._tokens = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", self.refresh_token),
                "expires_at": time.time() + data.get("expires_in", 3600),
            }
            self._save()
            logger.info("Token refreshed successfully")
            return True
        except httpx.HTTPStatusError as e:
            logger.error("Token refresh failed: HTTP %s", e.response.status_code)
            return False
        except httpx.RequestError as e:
            logger.error("Token refresh request failed: %s", e)
            return False

    async def get_valid_token(self) -> str:
        """Return a valid access token, refreshing if expired.

        Raises ValueError if no valid token is available.
        """
        if self.is_expired():
            refreshed = await self.refresh()
            if not refreshed:
                raise ValueError(
                    "Access token expired and refresh failed. Re-authenticate with scripts/auth.py"
                )

        if not self.access_token:
            raise ValueError(
                "No access token available. Set MONZO_ACCESS_TOKEN or run scripts/auth.py"
            )

        return self.access_token

    async def handle_auth_error(self) -> bool:
        """Called when Monzo returns a 401. Attempts a refresh.

        Returns True if a new token was obtained.
        """
        logger.info("Got 401 from Monzo API, attempting token refresh")
        return await self.refresh()

    def update_tokens(self, access_token: str, refresh_token: str, expires_in: int = 3600) -> None:
        """Manually update tokens (e.g. after initial OAuth flow)."""
        self._tokens = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in,
        }
        self._save()
