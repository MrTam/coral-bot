"""Tests for the OAuth metadata patching middleware helper."""

import json

from coral_bot.server import _patch_oauth_metadata


class TestPatchOAuthMetadata:
    def test_strips_trailing_slash_from_issuer(self):
        body = json.dumps({"issuer": "https://example.com/"}).encode()
        result = json.loads(_patch_oauth_metadata(body))
        assert result["issuer"] == "https://example.com"

    def test_strips_trailing_slash_from_authorization_servers(self):
        body = json.dumps(
            {"authorization_servers": ["https://example.com/", "https://other.com"]}
        ).encode()
        result = json.loads(_patch_oauth_metadata(body))
        assert result["authorization_servers"] == [
            "https://example.com",
            "https://other.com",
        ]

    def test_adds_none_to_token_endpoint_auth_methods(self):
        body = json.dumps(
            {
                "token_endpoint_auth_methods_supported": [
                    "client_secret_post",
                    "client_secret_basic",
                ]
            }
        ).encode()
        result = json.loads(_patch_oauth_metadata(body))
        assert "none" in result["token_endpoint_auth_methods_supported"]

    def test_adds_none_to_revocation_endpoint_auth_methods(self):
        body = json.dumps(
            {
                "revocation_endpoint_auth_methods_supported": [
                    "client_secret_post",
                    "client_secret_basic",
                ]
            }
        ).encode()
        result = json.loads(_patch_oauth_metadata(body))
        assert "none" in result["revocation_endpoint_auth_methods_supported"]

    def test_does_not_duplicate_none(self):
        body = json.dumps(
            {"token_endpoint_auth_methods_supported": ["client_secret_post", "none"]}
        ).encode()
        result = _patch_oauth_metadata(body)
        # Should return unchanged (no patching needed)
        assert result == body

    def test_no_changes_returns_original(self):
        body = json.dumps({"issuer": "https://example.com"}).encode()
        result = _patch_oauth_metadata(body)
        assert result == body

    def test_invalid_json_returns_original(self):
        body = b"not json"
        assert _patch_oauth_metadata(body) == body

    def test_all_patches_applied_together(self):
        body = json.dumps(
            {
                "issuer": "https://example.com/",
                "authorization_servers": ["https://example.com/"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_post",
                    "client_secret_basic",
                ],
            }
        ).encode()
        result = json.loads(_patch_oauth_metadata(body))
        assert result["issuer"] == "https://example.com"
        assert result["authorization_servers"] == ["https://example.com"]
        assert "none" in result["token_endpoint_auth_methods_supported"]
