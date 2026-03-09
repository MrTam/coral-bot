"""Shared fixtures for coral-bot tests."""

from unittest.mock import AsyncMock, patch

import pytest

from coral_bot.client import MonzoClient


@pytest.fixture(autouse=True)
def _clear_per_user_state():
    """Reset per-user client and account caches before each test."""
    import coral_bot.server as srv

    srv._clients.clear()
    srv._account_caches.clear()
    yield
    srv._clients.clear()
    srv._account_caches.clear()


@pytest.fixture
def mock_client():
    """Return an AsyncMock spec'd to MonzoClient, patched into server._get_client."""
    mock = AsyncMock(spec=MonzoClient)
    with patch("coral_bot.server._get_client", return_value=mock):
        yield mock
