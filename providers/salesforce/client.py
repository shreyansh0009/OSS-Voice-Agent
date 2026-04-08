"""
SalesforceClient: thin async REST API wrapper.

All methods auto-refresh auth via SalesforceAuth.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from providers.salesforce.auth import SalesforceAuth

logger = logging.getLogger(__name__)


class SalesforceClient:
    def __init__(self, auth: SalesforceAuth):
        self.auth = auth

    # ── internal helpers ─────────────────────────────────────────────────────

    async def _base(self) -> str:
        url = await self.auth.instance_url()
        return f"{url}/services/data/{self.auth.api_version}"

    async def _headers(self) -> dict[str, str]:
        h = await self.auth.auth_headers()
        h["Content-Type"] = "application/json"
        return h

    # ── SOQL query ────────────────────────────────────────────────────────────

    async def query(self, soql: str) -> dict:
        """Run a SOQL SELECT query. Returns the full response dict (records, totalSize, done)."""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{await self._base()}/query",
                params={"q": soql},
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    # ── sObject CRUD ──────────────────────────────────────────────────────────

    async def get(self, sobject: str, record_id: str, fields: list[str] | None = None) -> dict:
        """Retrieve a single sObject record by ID. Optionally restrict to specific fields."""
        url = f"{await self._base()}/sobjects/{sobject}/{record_id}"
        params = {"fields": ",".join(fields)} if fields else {}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=await self._headers())
            resp.raise_for_status()
            return resp.json()

    async def create(self, sobject: str, data: dict) -> dict:
        """Create a new sObject record. Returns {"id": "...", "success": true}."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{await self._base()}/sobjects/{sobject}/",
                json=data,
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    async def update(self, sobject: str, record_id: str, data: dict) -> dict:
        """Update an existing sObject record. Returns {} on success (204)."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{await self._base()}/sobjects/{sobject}/{record_id}",
                json=data,
                headers=await self._headers(),
            )
            # 204 No Content is a success for PATCH
            if resp.status_code == 204:
                return {"success": True, "id": record_id}
            resp.raise_for_status()
            return resp.json()

    async def delete(self, sobject: str, record_id: str) -> dict:
        """Delete an sObject record. Returns {} on success (204)."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{await self._base()}/sobjects/{sobject}/{record_id}",
                headers=await self._headers(),
            )
            if resp.status_code == 204:
                return {"success": True, "id": record_id}
            resp.raise_for_status()
            return resp.json()

    # ── SOSL search ───────────────────────────────────────────────────────────

    async def search(self, sosl: str) -> dict:
        """Run a SOSL FIND query. Returns searchRecords list."""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{await self._base()}/search",
                params={"q": sosl},
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
