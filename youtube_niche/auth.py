"""Auth strategies for the YouTube Data API.

The public read endpoints this tool uses (search/videos/channels/commentThreads) accept
either a simple API key (`?key=`) or an OAuth 2.0 bearer token. Both consume the same
project quota.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import requests


class ApiKeyAuth:
    kind = "api_key"

    def __init__(self, key: str):
        self.key = key

    def apply(self, params: dict, headers: dict) -> None:
        params["key"] = self.key


class OAuthAuth:
    """OAuth installed/web-app credentials. Refreshes the access token via the refresh token.

    Tolerant of both token formats:
      - Node googleapis: {access_token, refresh_token, scope, token_type, expiry_date(ms)}
      - google-auth (py): {token, refresh_token, scopes, expiry(ISO)}
    """

    kind = "oauth"

    def __init__(self, client_secret_path: str, token_path: str):
        self.client_secret_path = Path(client_secret_path)
        self.token_path = Path(token_path)

        cs = json.loads(self.client_secret_path.read_text())
        root = cs.get("installed") or cs.get("web") or cs
        self.client_id = root["client_id"]
        self.client_secret = root["client_secret"]
        self.token_uri = root.get("token_uri", "https://oauth2.googleapis.com/token")

        tok = json.loads(self.token_path.read_text())
        self.refresh_token = tok.get("refresh_token")
        self._access_token = tok.get("access_token") or tok.get("token")
        self._expiry = self._parse_expiry(tok)
        self.scope = tok.get("scope") or tok.get("scopes")

        if not self.refresh_token and not self._access_token:
            raise ValueError(
                f"Token file has neither refresh_token nor access_token: {self.token_path}"
            )

    @staticmethod
    def _parse_expiry(tok: dict) -> float:
        if tok.get("expiry_date") is not None:  # Node: ms epoch
            try:
                return float(tok["expiry_date"]) / 1000.0
            except (TypeError, ValueError):
                return 0.0
        if tok.get("expiry"):  # google-auth: ISO string
            try:
                s = str(tok["expiry"]).replace("Z", "+00:00")
                return dt.datetime.fromisoformat(s).timestamp()
            except Exception:
                return 0.0
        return 0.0

    def _valid(self) -> bool:
        return bool(self._access_token) and time.time() < (self._expiry - 60)

    def _refresh(self) -> str:
        if not self.refresh_token:
            raise RuntimeError("Access token expired and no refresh_token to renew it.")
        r = requests.post(
            self.token_uri,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"OAuth token refresh failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        self._access_token = data["access_token"]
        self._expiry = time.time() + float(data.get("expires_in", 3600))
        return self._access_token

    def access_token(self) -> str:
        if self._valid():
            return self._access_token
        return self._refresh()

    def invalidate(self) -> None:
        """Force a refresh on the next call (used after a 401)."""
        self._expiry = 0.0

    def apply(self, params: dict, headers: dict) -> None:
        headers["Authorization"] = f"Bearer {self.access_token()}"
