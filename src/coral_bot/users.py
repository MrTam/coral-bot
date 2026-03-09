"""User store for multi-user management.

Each user is stored as a JSON file in the data directory with:
- A UUID user ID
- Their Monzo user ID (for deduplication)
- Their Monzo token file path
"""

import json
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_USERS_DIR = "/data/users"


class UserStore:
    """Manages per-user data stored as JSON files on disk.

    Each user file contains:
        - user_id: UUID string
        - monzo_user_id: Monzo account user ID (for deduplication)
        - token_file: path to the user's Monzo token file
    """

    def __init__(self, users_dir: str | None = None):
        self._dir = Path(users_dir or DEFAULT_USERS_DIR)

    def _user_file(self, user_id: str) -> Path:
        return self._dir / f"{user_id}.json"

    def _token_file_for(self, user_id: str) -> str:
        return str(self._dir.parent / "tokens" / f"{user_id}.json")

    def find_user_by_monzo_user_id(self, monzo_user_id: str) -> str | None:
        """Find an existing user by their Monzo user ID.

        Returns the coral-bot user_id if found, None otherwise.
        """
        if not self._dir.exists():
            return None

        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("monzo_user_id") == monzo_user_id:
                return data.get("user_id")

        return None

    def create_or_find_user(
        self,
        monzo_access_token: str,
        monzo_refresh_token: str,
        monzo_user_id: str,
    ) -> str:
        """Create a new user or find an existing one by Monzo user ID.

        If a user with the same monzo_user_id already exists, their tokens
        are updated and the existing user_id is returned.

        Args:
            monzo_access_token: The Monzo access token from OAuth.
            monzo_refresh_token: The Monzo refresh token from OAuth.
            monzo_user_id: The Monzo user ID (from /ping/whoami).

        Returns:
            The coral-bot user_id.
        """
        existing_id = self.find_user_by_monzo_user_id(monzo_user_id)
        if existing_id is not None:
            # Update tokens for existing user
            from coral_bot.tokens import TokenManager

            token_file = self.get_token_file(existing_id)
            if token_file:
                tm = TokenManager(token_file=token_file)
                tm.update_tokens(monzo_access_token, monzo_refresh_token)
                logger.info("Updated tokens for existing user %s", existing_id)
            return existing_id

        # Create new user
        user_id = str(uuid.uuid4())

        self._dir.mkdir(parents=True, exist_ok=True)
        user_data = {
            "user_id": user_id,
            "monzo_user_id": monzo_user_id,
            "token_file": self._token_file_for(user_id),
        }
        user_file = self._user_file(user_id)
        user_file.write_text(json.dumps(user_data, indent=2))
        user_file.chmod(0o600)

        # Persist Monzo tokens in a separate file for TokenManager
        from coral_bot.tokens import TokenManager

        tm = TokenManager(token_file=user_data["token_file"])
        tm.update_tokens(monzo_access_token, monzo_refresh_token)

        logger.info("Created user %s for Monzo user ...%s", user_id, monzo_user_id[-6:])
        return user_id

    def get_token_file(self, user_id: str) -> str | None:
        """Return the Monzo token file path for a user, or None if not found."""
        user_file = self._user_file(user_id)
        if not user_file.exists():
            return None
        try:
            data = json.loads(user_file.read_text())
            return data.get("token_file")
        except (json.JSONDecodeError, OSError):
            return None

    def list_users(self) -> list[str]:
        """Return a list of all user IDs."""
        if not self._dir.exists():
            return []
        users = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                uid = data.get("user_id")
                if uid:
                    users.append(uid)
            except (json.JSONDecodeError, OSError):
                continue
        return users

    def delete_user(self, user_id: str) -> bool:
        """Delete a user and their token file. Returns True if the user existed."""
        user_file = self._user_file(user_id)
        if not user_file.exists():
            return False

        # Clean up token file
        try:
            data = json.loads(user_file.read_text())
            token_path = Path(data.get("token_file", ""))
            if token_path.exists():
                token_path.unlink()
        except (json.JSONDecodeError, OSError):
            pass

        user_file.unlink()
        logger.info("Deleted user %s", user_id)
        return True
