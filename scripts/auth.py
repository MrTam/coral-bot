"""OAuth 2.0 helper for obtaining Monzo access tokens.

Usage:
    uv run python scripts/auth.py

Prerequisites:
    1. Register an app at https://developers.monzo.com
    2. Set your redirect URL to http://localhost:8080/callback
    3. Set MONZO_CLIENT_ID and MONZO_CLIENT_SECRET environment variables

The script will:
    1. Open a browser for Monzo login
    2. Listen for the OAuth callback on localhost:8080
    3. Exchange the auth code for an access token
    4. Print the token for you to export
"""

import http.server
import os
import secrets
import sys
import urllib.parse
import webbrowser

import httpx

CLIENT_ID = os.environ.get("MONZO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MONZO_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8080/callback"
AUTH_URL = "https://auth.monzo.com"
TOKEN_URL = "https://api.monzo.com/oauth2/token"

CALLBACK_TIMEOUT_SECONDS = 120


def get_auth_code() -> str:
    """Open browser for OAuth and capture the callback."""
    state = secrets.token_urlsafe(32)
    params = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": state,
        }
    )
    url = f"{AUTH_URL}/?{params}"
    print(f"Opening browser for Monzo login...\n{url}", file=sys.stderr)
    webbrowser.open(url)

    auth_code = None
    auth_failed = False

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code, auth_failed
            parsed = urllib.parse.urlparse(self.path)

            # Ignore requests that aren't the callback path
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            returned_state = params.get("state", [None])[0]

            if returned_state != state:
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Error: state mismatch</h1>")
                auth_failed = True
                return

            auth_code = params.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h1>Auth complete!</h1><p>You can close this tab. "
                b"Now approve the login in your Monzo app.</p>"
            )

        def log_message(self, format, *args):
            pass  # Suppress request logging

    server = http.server.HTTPServer(("localhost", 8080), Handler)
    server.timeout = CALLBACK_TIMEOUT_SECONDS
    print(
        f"Waiting for OAuth callback on localhost:8080 (timeout: {CALLBACK_TIMEOUT_SECONDS}s)...",
        file=sys.stderr,
    )
    while auth_code is None and not auth_failed:
        server.handle_request()
    server.server_close()

    if auth_failed or not auth_code:
        print("Error: Authentication failed.", file=sys.stderr)
        sys.exit(1)

    return auth_code


def exchange_token(auth_code: str) -> dict:
    """Exchange auth code for access and refresh tokens."""
    with httpx.Client() as client:
        try:
            response = client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uri": REDIRECT_URI,
                    "code": auth_code,
                },
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.json()
                msg = body.get("message", body.get("error_description", str(e)))
            except Exception:
                msg = f"HTTP {e.response.status_code}"
            print(f"Error exchanging token: {msg}", file=sys.stderr)
            sys.exit(1)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Monzo OAuth 2.0 authentication helper")
    parser.add_argument(
        "--multi-user",
        action="store_true",
        help="Create a multi-user API key via UserStore (for HTTP deployments)",
    )
    args = parser.parse_args()

    if not CLIENT_ID or not CLIENT_SECRET:
        print(
            "Error: Set MONZO_CLIENT_ID and MONZO_CLIENT_SECRET environment variables.\n"
            "Register an app at https://developers.monzo.com",
            file=sys.stderr,
        )
        sys.exit(1)

    auth_code = get_auth_code()
    print("\nGot auth code. Exchanging for access token...", file=sys.stderr)
    print(
        "(Note: You may need to approve the login in the Monzo app for full API access)\n",
        file=sys.stderr,
    )

    token_data = exchange_token(auth_code)

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    if args.multi_user:
        from coral_bot.users import UserStore

        # Get the Monzo user ID for deduplication
        resp = httpx.get(
            "https://api.monzo.com/ping/whoami",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        monzo_user_id = resp.json().get("user_id", "")

        store = UserStore()
        user_id = store.create_or_find_user(access_token, refresh_token, monzo_user_id)
        print("\n--- Multi-user mode ---", file=sys.stderr)
        print(f"User ID: {user_id}", file=sys.stderr)
        print(
            "\nUser created/updated. Connect via OAuth flow in HTTP mode.",
            file=sys.stderr,
        )
    else:
        print("\n--- Access Token ---", file=sys.stderr)
        print(access_token, file=sys.stderr)
        print("\n--- Refresh Token ---", file=sys.stderr)
        print(refresh_token, file=sys.stderr)
        print(
            f"\nTo use with Claude Code, run:\n  export MONZO_ACCESS_TOKEN='{access_token}'",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
