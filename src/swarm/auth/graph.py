"""Microsoft Graph OAuth token manager (PKCE flow, no client secret)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path

import aiohttp

_TOKEN_PATH = Path.home() / ".swarm" / "graph_tokens.json"
_AUTH_BASE = "https://login.microsoftonline.com"
_SCOPE = "Mail.Read Mail.Send offline_access"
_log = logging.getLogger(__name__)


class GraphTokenManager:
    """Manages Microsoft Graph OAuth tokens with automatic refresh."""

    def __init__(self, client_id: str, tenant_id: str = "common", port: int = 9090) -> None:
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.redirect_uri = f"http://localhost:{port}/auth/graph/callback"
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self.last_error: str = ""
        self._load()

    # --- Public API ---

    def is_connected(self) -> bool:
        """True if a refresh token is available (may need refresh)."""
        return bool(self._refresh_token)

    def get_auth_url(self, state: str, code_verifier: str) -> str:
        """Build the Microsoft OAuth authorize URL with PKCE challenge."""
        challenge = _pkce_challenge(code_verifier)
        params = (
            f"client_id={self.client_id}"
            f"&response_type=code"
            f"&redirect_uri={self.redirect_uri}"
            f"&response_mode=query"
            f"&scope={_SCOPE.replace(' ', '%20')}"
            f"&state={state}"
            f"&code_challenge={challenge}"
            f"&code_challenge_method=S256"
        )
        return f"{_AUTH_BASE}/{self.tenant_id}/oauth2/v2.0/authorize?{params}"

    async def exchange_code(self, code: str, code_verifier: str) -> bool:
        """Exchange authorization code for tokens. Returns True on success."""
        url = f"{_AUTH_BASE}/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "scope": _SCOPE,
        }
        return await self._token_request(url, data)

    async def get_token(self) -> str | None:
        """Return a valid access token, refreshing if needed."""
        if not self._refresh_token:
            return None
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        if await self._refresh():
            return self._access_token
        return None

    async def send_reply(self, message_id: str, body_html: str, *, reply_all: bool = True) -> bool:
        """Send a reply (or reply-all) to an existing message via Graph API.

        Requires ``Mail.Send`` scope. Users must re-authenticate after the
        scope change if they connected before ``Mail.Send`` was added.
        """
        token = await self.get_token()
        if not token:
            _log.warning("send_reply: no valid token")
            return False

        from urllib.parse import quote

        encoded = quote(message_id, safe="")
        action = "replyAll" if reply_all else "reply"
        url = f"https://graph.microsoft.com/v1.0/me/messages/{encoded}/{action}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {"comment": body_html}

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status in (200, 202):
                        _log.info("Reply sent to message %s", message_id[:30])
                        return True
                    err = await resp.text()
                    _log.warning("send_reply failed (%s): %s", resp.status, err[:200])
                    return False
        except Exception as exc:
            _log.warning("send_reply exception: %s", exc)
            return False

    def disconnect(self) -> None:
        """Remove stored tokens."""
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        if _TOKEN_PATH.exists():
            _TOKEN_PATH.unlink()

    # --- Internal ---

    async def _refresh(self) -> bool:
        """Use refresh_token to get a new access_token."""
        if not self._refresh_token:
            return False
        url = f"{_AUTH_BASE}/{self.tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": _SCOPE,
        }
        return await self._token_request(url, data)

    async def _token_request(self, url: str, data: dict) -> bool:
        """POST to token endpoint, save result. Returns True on success."""
        self.last_error = ""
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url, data=data, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        err_body = await resp.text()
                        try:
                            err_json = json.loads(err_body)
                            self.last_error = err_json.get(
                                "error_description", err_json.get("error", err_body[:300])
                            )
                        except Exception:
                            self.last_error = err_body[:300]
                        _log.warning(
                            "Graph token request failed (%s): %s", resp.status, self.last_error
                        )
                        return False
                    body = await resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            _log.warning("Graph token request exception: %s", exc)
            return False

        self._access_token = body.get("access_token")
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        expires_in = body.get("expires_in", 3600)
        self._expires_at = time.time() + expires_in
        self._save()
        return True

    def _load(self) -> None:
        """Load tokens from disk."""
        if not _TOKEN_PATH.exists():
            return
        try:
            raw = json.loads(_TOKEN_PATH.read_text())
            self._access_token = raw.get("access_token")
            self._refresh_token = raw.get("refresh_token")
            self._expires_at = raw.get("expires_at", 0.0)
        except Exception:
            pass

    def _save(self) -> None:
        """Write tokens to disk with restrictive permissions."""
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(
            json.dumps(
                {
                    "access_token": self._access_token,
                    "refresh_token": self._refresh_token,
                    "expires_at": self._expires_at,
                }
            )
        )
        os.chmod(_TOKEN_PATH, 0o600)


def generate_pkce_verifier() -> str:
    """Generate a random 43-character code verifier for PKCE."""
    return secrets.token_urlsafe(32)


def _pkce_challenge(verifier: str) -> str:
    """SHA256 hash of verifier, base64url-encoded (no padding)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
