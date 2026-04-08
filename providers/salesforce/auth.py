"""
SalesforceAuth: OAuth 2.0 Client Credentials token manager.

Reads from environment:
  SF_CLIENT_ID       - Connected App consumer key
  SF_CLIENT_SECRET   - Connected App consumer secret
  SF_LOGIN_URL       - e.g. https://login.salesforce.com (default) or sandbox
  SF_API_VERSION     - Salesforce API version, default "v59.0"

Token is cached in-memory and auto-refreshed on expiry.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT = "/services/oauth2/token"
# Refresh 60 s before actual expiry to avoid races
EXPIRY_BUFFER_SECS = 60


@dataclass
class _Token:
    access_token: str
    instance_url: str
    expires_at: float  # unix timestamp


class SalesforceAuth:
    """
    Async-safe OAuth 2.0 client-credentials token manager.

    Usage:
        auth = SalesforceAuth()
        token = await auth.get_token()
        headers = await auth.auth_headers()
    """

    def __init__(self):
        self.client_id = os.environ["SF_CLIENT_ID"]
        self.client_secret = os.environ["SF_CLIENT_SECRET"]
        self.login_url = os.getenv("SF_LOGIN_URL", "https://login.salesforce.com").rstrip("/")
        self.api_version = os.getenv("SF_API_VERSION", "v59.0")
        self._token: _Token | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> _Token:
        """Return a valid token, refreshing if expired."""
        async with self._lock:
            if self._token and time.time() < self._token.expires_at:
                return self._token
            self._token = await self._fetch_token()
            return self._token

    async def _fetch_token(self) -> _Token:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.login_url}{TOKEN_ENDPOINT}",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

        expires_in = data.get("expires_in", 7200)
        token = _Token(
            access_token=data["access_token"],
            instance_url=data["instance_url"].rstrip("/"),
            expires_at=time.time() + expires_in - EXPIRY_BUFFER_SECS,
        )
        logger.info(f"Salesforce token acquired (instance: {token.instance_url})")
        return token

    async def auth_headers(self) -> dict[str, str]:
        token = await self.get_token()
        return {"Authorization": f"Bearer {token.access_token}"}

    async def instance_url(self) -> str:
        token = await self.get_token()
        return token.instance_url
