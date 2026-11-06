"""LinkedIn: OAuth tokens, image upload and the versioned Posts API.

Docs (Microsoft Learn, "LinkedIn Marketing / Community Management"):
  Posts API    POST /rest/posts
  Images API   POST /rest/images?action=initializeUpload, then PUT the bytes
  Little Text  commentary format: reserved characters must be escaped
Company pages need the Community Management API product (scope
w_organization_social) and an ADMINISTRATOR/CONTENT_ADMIN role on the page;
personal profiles need "Share on LinkedIn" (w_member_social).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from .config import Settings

log = logging.getLogger("desk")

API = "https://api.linkedin.com/rest"
OAUTH = "https://www.linkedin.com/oauth/v2"
# Little Text Format reserved characters. Unescaped, they can truncate the post.
RESERVED = set("\\|{}@[]()<>#*_~")


class LinkedInError(Exception):
    pass


def escape_little_text(text: str) -> str:
    return "".join("\\" + ch if ch in RESERVED else ch for ch in text)


def commentary(text: str, hashtags: list[str]) -> str:
    body = escape_little_text(text.strip())
    if hashtags:
        tags = " ".join("{hashtag|\\#|" + h + "}" for h in hashtags)
        body = f"{body}\n\n{tags}"
    return body


# ---------------------------------------------------------------- tokens


def load_token(s: Settings) -> dict:
    if s.linkedin_access_token:
        return {"access_token": s.linkedin_access_token, "expires_at": None}
    if not s.token_path.exists():
        raise LinkedInError("no LinkedIn token: run `python -m desk auth url` and `python -m desk auth code ...`")
    return json.loads(s.token_path.read_text())


def save_token(s: Settings, tok: dict) -> None:
    now = int(time.time())
    data = {
        "access_token": tok["access_token"],
        "expires_at": now + int(tok.get("expires_in", 0)) if tok.get("expires_in") else None,
        "refresh_token": tok.get("refresh_token", ""),
        "refresh_expires_at": now + int(tok["refresh_token_expires_in"]) if tok.get("refresh_token_expires_in") else None,
        "scope": tok.get("scope", ""),
        "saved_at": now,
    }
    s.token_path.parent.mkdir(parents=True, exist_ok=True)
    s.token_path.write_text(json.dumps(data, indent=1))
    os.chmod(s.token_path, 0o600)


def authorize_url(s: Settings) -> str:
    q = {"response_type": "code", "client_id": s.linkedin_client_id, "redirect_uri": s.linkedin_redirect_uri,
         "state": secrets.token_urlsafe(12), "scope": s.linkedin_scopes}
    return f"{OAUTH}/authorization?{urlencode(q, quote_via=quote)}"


async def exchange_code(s: Settings, code: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{OAUTH}/accessToken", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": s.linkedin_redirect_uri,
            "client_id": s.linkedin_client_id, "client_secret": s.linkedin_client_secret})
    if r.status_code != 200:
        raise LinkedInError(f"token exchange failed: HTTP {r.status_code} {r.text[:300]}")
    tok = r.json()
    save_token(s, tok)
    return tok


async def refresh_if_needed(s: Settings) -> dict:
    tok = load_token(s)
    exp = tok.get("expires_at")
    if not exp or exp - time.time() > 7 * 86400:
        return tok
    if not tok.get("refresh_token"):
        log.warning("LinkedIn token expires in %.1f days and there is no refresh token: re-run `desk auth`",
                    (exp - time.time()) / 86400)
        return tok
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{OAUTH}/accessToken", data={
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            "client_id": s.linkedin_client_id, "client_secret": s.linkedin_client_secret})
    if r.status_code != 200:
        log.error("token refresh failed: HTTP %s %s", r.status_code, r.text[:200])
        return tok
    new = r.json()
    new.setdefault("refresh_token", tok["refresh_token"])
    save_token(s, new)
    return load_token(s)


def token_days_left(s: Settings) -> float | None:
    try:
        exp = load_token(s).get("expires_at")
    except LinkedInError:
        return None
    return None if not exp else round((exp - time.time()) / 86400, 1)


# ---------------------------------------------------------------- client


class LinkedIn:
    def __init__(self, s: Settings, access_token: str):
        self.s = s
        self.author = s.linkedin_author_urn
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": s.linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    @classmethod
    async def connect(cls, s: Settings) -> "LinkedIn":
        if not s.linkedin_author_urn:
            raise LinkedInError("LINKEDIN_AUTHOR_URN is not set (urn:li:organization:<id> or urn:li:person:<id>)")
        tok = await refresh_if_needed(s)
        return cls(s, tok["access_token"])

    async def whoami(self) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get("https://api.linkedin.com/v2/userinfo",
                            headers={"Authorization": self.headers["Authorization"]})
        if r.status_code != 200:
            raise LinkedInError(f"userinfo: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    async def upload_image(self, path: Path) -> str:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{API}/images?action=initializeUpload", headers=self.headers,
                             json={"initializeUploadRequest": {"owner": self.author}})
            if r.status_code != 200:
                raise LinkedInError(f"initializeUpload: HTTP {r.status_code} {r.text[:300]}")
            v = r.json()["value"]
            up = await c.put(v["uploadUrl"], content=path.read_bytes(),
                             headers={"Authorization": self.headers["Authorization"],
                                      "Content-Type": "application/octet-stream"})
            if up.status_code not in (200, 201):
                raise LinkedInError(f"image upload: HTTP {up.status_code} {up.text[:300]}")
            urn = v["image"]
            # Wait until LinkedIn has processed it; posting a PROCESSING image fails.
            for _ in range(20):
                g = await c.get(f"{API}/images/{quote(urn, safe='')}", headers=self.headers)
                if g.status_code == 200 and g.json().get("status") == "AVAILABLE":
                    return urn
                if g.status_code == 200 and g.json().get("status") in ("PROCESSING_FAILED",):
                    raise LinkedInError(f"image processing failed for {path.name}")
                await asyncio.sleep(1.5)
        return urn  # usually fine even if status lagged

    async def create_post(self, text: str, hashtags: list[str], fmt: str, images: list[dict],
                          link_url: str = "", link_title: str = "", link_description: str = "") -> str:
        """Publish and return the post URN."""
        body: dict = {
            "author": self.author,
            "commentary": commentary(text, hashtags),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        urns = [await self.upload_image(Path(im["path"])) for im in images]
        if fmt == "image" and urns:
            body["content"] = {"media": {"id": urns[0], "altText": images[0].get("alt_text", "")[:4086]}}
        elif fmt == "multi_image" and len(urns) >= 2:
            body["content"] = {"multiImage": {"images": [
                {"id": u, "altText": im.get("alt_text", "")[:4086]} for u, im in zip(urns, images)]}}
        elif fmt == "article" and link_url:
            art = {"source": link_url, "title": link_title[:400] or link_url}
            if link_description:
                art["description"] = link_description[:4086]
            if urns:
                art["thumbnail"] = urns[0]
            body["content"] = {"article": art}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{API}/posts", headers=self.headers, json=body)
        if r.status_code != 201:
            raise LinkedInError(f"create post: HTTP {r.status_code} {r.text[:500]}")
        return r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id") or ""


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""
