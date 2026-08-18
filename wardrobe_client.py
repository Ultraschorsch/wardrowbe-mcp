"""
Internal client for talking to the Wardrowbe backend on Johanna's behalf.

This is NOT exposed to ChatGPT. It runs entirely inside the docker network
and uses Wardrowbe's existing dev-mode `/auth/sync` mechanism to mint a
service token scoped to Johanna's account, exactly like a real Wardrowbe
client (web/mobile app) does when logging in.

Only read (GET) endpoints are ever called here. No write/mutating endpoints
are wired into this client on purpose - the MCP server must stay read-only.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

WARDROBE_INTERNAL_URL = os.environ.get("WARDROBE_INTERNAL_URL", "http://backend:8000")
WARDROBE_USER_EXTERNAL_ID = os.environ["WARDROBE_USER_EXTERNAL_ID"]
WARDROBE_USER_EMAIL = os.environ["WARDROBE_USER_EMAIL"]
# UserSyncRequest requires display_name (added upstream). This server only
# ever syncs Johanna's account, so a fixed default is fine; override via
# env if you ever want a different display name.
WARDROBE_USER_DISPLAY_NAME = os.environ.get("WARDROBE_USER_DISPLAY_NAME", "Johanna")

# Fields that must never leave this process, even though the underlying
# Wardrowbe API might include them on some responses. Stripped recursively.
_FORBIDDEN_FIELDS = {"purchase_price", "purchase_date"}


def _scrub(obj: Any) -> Any:
    """Recursively remove forbidden (price-related) fields from any JSON-ish value."""
    if isinstance(obj, dict):
        return {
            k: _scrub(v)
            for k, v in obj.items()
            if k not in _FORBIDDEN_FIELDS
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


class WardrobeClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # All Wardrowbe backend routes live under /api/v1 (see app.main:
        # `app.include_router(api_router, prefix="/api/v1")`). Bake that
        # into the base_url so every call below (/auth/sync, /items,
        # /outfits, /analytics) resolves correctly instead of 404ing.
        self._http = httpx.AsyncClient(
            base_url=f"{WARDROBE_INTERNAL_URL}/api/v1", timeout=15.0
        )

    async def _ensure_token(self) -> str:
        # Refresh a bit before actual expiry to avoid racing a 401.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        resp = await self._http.post(
            "/auth/sync",
            json={
                "external_id": WARDROBE_USER_EXTERNAL_ID,
                "email": WARDROBE_USER_EMAIL,
                "display_name": WARDROBE_USER_DISPLAY_NAME,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # Wardrowbe issues 7-day tokens (see create_access_token in auth.py).
        self._token_expires_at = time.time() + 7 * 24 * 3600
        return self._token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._http.get(path, params=params, headers=headers)
        if resp.status_code == 401:
            # Token might have been invalidated server-side; force a fresh one once.
            self._token = None
            token = await self._ensure_token()
            headers = {"Authorization": f"Bearer {token}"}
            resp = await self._http.get(path, params=params, headers=headers)
        resp.raise_for_status()
        return _scrub(resp.json())

    async def search_items(
        self,
        *,
        category: str | None = None,
        color: str | None = None,
        occasion: str | None = None,
        formality: str | None = None,
        season: str | None = None,
        # "ready" = finished AI tagging, usable item. This is the closest
        # match to "active wardrobe item"; the ItemStatus enum has no
        # "active" value (only processing/ready/error/archived), and
        # is_archived defaults to False on the backend already.
        status: str = "ready",
        page: int = 1,
        page_size: int = 50,
    ) -> Any:
        params: dict[str, Any] = {"page": page, "page_size": page_size, "status": status}
        if category:
            params["type"] = category
        if color:
            params["color"] = color
        if occasion:
            params["occasion"] = occasion
        if formality:
            params["formality"] = formality
        if season:
            params["season"] = season
        return await self._get("/items", params=params)

    async def get_outfit_history(
        self, *, limit: int = 20, status: str | None = None
    ) -> Any:
        params: dict[str, Any] = {"page": 1, "page_size": limit}
        if status:
            params["status"] = status
        return await self._get("/outfits", params=params)

    async def get_wardrobe_insights(self) -> Any:
        return await self._get("/analytics")

    async def get_items_for_occasion(
        self,
        *,
        occasion: str,
        season: str | None = None,
        formality: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "page": 1,
            "page_size": 100,
            "status": "ready",
            "occasion": occasion,
        }
        if season:
            params["season"] = season
        if formality:
            params["formality"] = formality
        return await self._get("/items", params=params)


wardrobe_client = WardrobeClient()
