"""
Wardrowbe MCP server - read-only ChatGPT connector for Johanna.

Auth model:
    ChatGPT <--OAuth (DCR-shaped)--> this server (FastMCP OAuthProxy)
    this server <--real OIDC--> Pocket-ID (upstream authorization server)

Pocket-ID doesn't support Dynamic Client Registration, so we use FastMCP's
OAuthProxy: it presents a DCR-compliant face to ChatGPT while using one
pre-registered Pocket-ID OIDC client behind the scenes. FastMCP issues its
own short-lived tokens to ChatGPT; the real Pocket-ID token never leaves
this process (see FastMCP's "token factory" / anti-passthrough design).

Every tool here is read-only. None of them call a Wardrowbe endpoint that
creates, updates, or deletes anything.
"""

from __future__ import annotations

import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.disk import DiskStore

from wardrobe_client import wardrobe_client

# Registered OAuth clients (e.g. ChatGPT's dynamically-registered client) and
# issued tokens must survive container rebuilds, or every redeploy forces
# everyone to reconnect their connector from scratch. This directory is
# mounted as a persistent Docker volume - see docker-compose.yml.
client_storage = DiskStore(directory="/data/oauth-storage")

POCKET_ID_ISSUER = "https://auth.oachkatzl.me"
# Comma-separated list of Pocket-ID emails allowed to use this connector.
ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.environ["WARDROBE_USER_EMAIL"].split(",")
    if e.strip()
}

token_verifier = JWTVerifier(
    jwks_uri=f"{POCKET_ID_ISSUER}/.well-known/jwks.json",
    issuer=POCKET_ID_ISSUER,
    audience=os.environ["POCKET_ID_CLIENT_ID"],
    # NOTE: no required_scopes here - Pocket-ID's access tokens carry no
    # "scope" claim at all (only aud/exp/iat/iss/sub/type). Scope enforcement
    # already happened at Pocket-ID's own consent screen; requiring a scope
    # claim that never exists would reject every single upstream token.
)

auth = OAuthProxy(
    upstream_authorization_endpoint=f"{POCKET_ID_ISSUER}/authorize",
    upstream_token_endpoint=f"{POCKET_ID_ISSUER}/api/oidc/token",
    upstream_client_id=os.environ["POCKET_ID_CLIENT_ID"],
    upstream_client_secret=os.environ["POCKET_ID_CLIENT_SECRET"],
    token_verifier=token_verifier,
    base_url=os.environ["MCP_PUBLIC_BASE_URL"],  # e.g. https://wardrowbe-mcp.oachkatzl.me
    jwt_signing_key=os.environ["MCP_JWT_SIGNING_KEY"],
    require_authorization_consent=True,
    # Pocket-ID requires a scope on the /authorize request - without this,
    # it responds "Scope is required" before we ever get to login.
    valid_scopes=["openid", "email", "profile"],
    client_storage=client_storage,
)
# FastMCP only derives the DCR default scope from token_verifier.required_scopes,
# which we deliberately leave unset above (Pocket-ID tokens carry no scope
# claim). Without this, DCR clients that omit "scope" during /register (e.g.
# ChatGPT) get an empty registered scope, so every later /authorize call fails
# with invalid_scope before ever reaching Pocket-ID.
auth._default_scope_str = "openid email profile"

mcp = FastMCP(name="Wardrowbe", auth=auth)


async def _assert_is_johanna() -> None:
    """
    Refuse to serve anyone but Johanna, regardless of who Pocket-ID authenticated.

    FastMCP's client-facing token is intentionally minimal (no email claim), so
    we fetch the real identity from Pocket-ID's userinfo endpoint using the
    upstream token FastMCP holds on our behalf for this request.
    """
    access_token = get_access_token()
    upstream_token = getattr(access_token, "token", None) or getattr(
        access_token, "access_token", None
    )
    if not upstream_token:
        raise PermissionError("Could not resolve upstream identity token.")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{POCKET_ID_ISSUER}/api/oidc/userinfo",
            headers={"Authorization": f"Bearer {upstream_token}"},
        )
    resp.raise_for_status()
    email = (resp.json().get("email") or "").lower().strip()

    if email not in ALLOWED_EMAILS:
        raise PermissionError(
            "This Wardrowbe connector is only available to specific accounts."
        )


@mcp.tool
async def search_items(
    category: str | None = None,
    color: str | None = None,
    occasion: str | None = None,
    formality: str | None = None,
    season: str | None = None,
) -> dict:
    """
    Search Johanna's wardrobe. All filters are optional and combinable.

    category: clothing type, e.g. "top", "bottom", "dress", "shoes", "outerwear"
    color: primary color, e.g. "coral", "navy", "black"
    occasion: e.g. "casual", "work", "formal", "lecture", "presentation"
    formality: e.g. "casual", "smart_casual", "formal"
    season: e.g. "spring", "summer", "autumn", "winter"

    Never returns purchase price or purchase date.
    """
    await _assert_is_johanna()
    return await wardrobe_client.search_items(
        category=category,
        color=color,
        occasion=occasion,
        formality=formality,
        season=season,
    )


@mcp.tool
async def get_outfit_history(limit: int = 20, status: str | None = None) -> dict:
    """
    List Johanna's past outfits (what she's worn/planned), most recent first.

    limit: how many outfits to return (default 20)
    status: optional filter, e.g. "worn", "planned", "suggested"
    """
    await _assert_is_johanna()
    return await wardrobe_client.get_outfit_history(limit=limit, status=status)


@mcp.tool
async def get_wardrobe_insights() -> dict:
    """
    Aggregate stats about Johanna's wardrobe: category/color distribution,
    most-worn, least-worn, and never-worn items, plus Wardrowbe's own
    built-in insights. Use this for "shop your closet" (rediscover
    under-used pieces) and for spotting gaps before recommending a purchase.

    Never includes purchase price or purchase date.
    """
    await _assert_is_johanna()
    return await wardrobe_client.get_wardrobe_insights()


@mcp.tool
async def get_items_for_occasion(
    occasion: str,
    season: str | None = None,
    formality: str | None = None,
) -> dict:
    """
    Fetch candidate items from Johanna's real wardrobe for a given occasion
    (and optionally season/formality). Use this data to compose an outfit
    suggestion yourself - this tool does not call Wardrowbe's own AI
    suggestion engine and does not create or save anything in Wardrowbe.

    occasion: e.g. "casual", "work", "formal", "lecture", "presentation"
    season / formality: optional narrowing filters
    """
    await _assert_is_johanna()
    return await wardrobe_client.get_items_for_occasion(
        occasion=occasion, season=season, formality=formality
    )


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
