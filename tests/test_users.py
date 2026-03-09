"""Tests for UserStore in coral_bot.users."""

import json

import pytest

from coral_bot.users import UserStore


@pytest.fixture
def user_store(tmp_path):
    """Create a UserStore using a temporary directory."""
    return UserStore(users_dir=str(tmp_path / "users"))


class TestCreateOrFindUser:
    def test_returns_user_id(self, user_store):
        user_id = user_store.create_or_find_user("access_tok", "refresh_tok", "monzo-user-1")
        assert user_id  # non-empty UUID string

    def test_creates_user_file(self, user_store):
        user_id = user_store.create_or_find_user("access_tok", "refresh_tok", "monzo-user-1")
        user_file = user_store._user_file(user_id)
        assert user_file.exists()

        data = json.loads(user_file.read_text())
        assert data["user_id"] == user_id
        assert data["monzo_user_id"] == "monzo-user-1"
        assert "token_file" in data

    def test_creates_monzo_token_file(self, user_store):
        user_id = user_store.create_or_find_user("access_tok", "refresh_tok", "monzo-user-1")
        token_file = user_store.get_token_file(user_id)
        assert token_file is not None

        from pathlib import Path

        token_data = json.loads(Path(token_file).read_text())
        assert token_data["access_token"] == "access_tok"
        assert token_data["refresh_token"] == "refresh_tok"

    def test_multiple_users_get_different_ids(self, user_store):
        id1 = user_store.create_or_find_user("tok1", "ref1", "monzo-user-1")
        id2 = user_store.create_or_find_user("tok2", "ref2", "monzo-user-2")
        assert id1 != id2


class TestMonzoUserIdDeduplication:
    def test_same_monzo_user_returns_same_id(self, user_store):
        id1 = user_store.create_or_find_user("tok1", "ref1", "monzo-user-1")
        id2 = user_store.create_or_find_user("tok2", "ref2", "monzo-user-1")
        assert id1 == id2

    def test_dedup_updates_tokens(self, user_store):
        from pathlib import Path

        user_id = user_store.create_or_find_user("old_access", "old_refresh", "monzo-user-1")
        user_store.create_or_find_user("new_access", "new_refresh", "monzo-user-1")

        token_file = user_store.get_token_file(user_id)
        assert token_file is not None
        token_data = json.loads(Path(token_file).read_text())
        assert token_data["access_token"] == "new_access"
        assert token_data["refresh_token"] == "new_refresh"

    def test_different_monzo_users_get_different_ids(self, user_store):
        id1 = user_store.create_or_find_user("tok1", "ref1", "monzo-user-1")
        id2 = user_store.create_or_find_user("tok2", "ref2", "monzo-user-2")
        assert id1 != id2


class TestFindUserByMonzoUserId:
    def test_found(self, user_store):
        user_id = user_store.create_or_find_user("tok", "ref", "monzo-user-1")
        assert user_store.find_user_by_monzo_user_id("monzo-user-1") == user_id

    def test_not_found(self, user_store):
        assert user_store.find_user_by_monzo_user_id("no-such-user") is None

    def test_empty_store(self, user_store):
        assert user_store.find_user_by_monzo_user_id("any") is None


class TestGetTokenFile:
    def test_existing_user(self, user_store):
        user_id = user_store.create_or_find_user("access_tok", "refresh_tok", "monzo-user-1")
        token_file = user_store.get_token_file(user_id)
        assert token_file is not None
        assert user_id in token_file

    def test_nonexistent_user(self, user_store):
        assert user_store.get_token_file("nonexistent-id") is None


class TestListUsers:
    def test_empty(self, user_store):
        assert user_store.list_users() == []

    def test_lists_created_users(self, user_store):
        id1 = user_store.create_or_find_user("tok1", "ref1", "monzo-user-1")
        id2 = user_store.create_or_find_user("tok2", "ref2", "monzo-user-2")
        users = user_store.list_users()
        assert set(users) == {id1, id2}


class TestDeleteUser:
    def test_delete_existing(self, user_store):
        user_id = user_store.create_or_find_user("access_tok", "refresh_tok", "monzo-user-1")
        assert user_store.delete_user(user_id) is True
        assert user_id not in user_store.list_users()

    def test_delete_nonexistent(self, user_store):
        assert user_store.delete_user("nonexistent") is False

    def test_delete_removes_token_file(self, user_store):
        from pathlib import Path

        user_id = user_store.create_or_find_user("access_tok", "refresh_tok", "monzo-user-1")
        token_file = user_store.get_token_file(user_id)
        assert token_file is not None
        assert Path(token_file).exists()

        user_store.delete_user(user_id)
        assert not Path(token_file).exists()
