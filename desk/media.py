"""Images: find, fetch, check and normalise, exposed to Claude as in-process
MCP tools (Agent SDK `create_sdk_mcp_server`).

Claude searches, fetches candidates into its workspace, then *looks at them*
with Read (it sees images) before choosing. Only images we are allowed to
post are reachable:

- press      official handouts in the story files (WAM, media offices, the
             companies' own newsrooms), released for media use
- stock      Pexels / Unsplash search results (free licences)
- brand      our own photos in brand/ (team, office, events), described in
             brand/library.yaml
- link       the preview image of a page we link to (article posts only)

Photos taken by news outlets' photographers are never fetched: they belong
to the outlet.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool
from PIL import Image, ImageFilter, ImageOps, ImageStat

from .config import Settings

log = logging.getLogger("desk")

PRESS_HOSTS = (
    "wam.ae", "mediaoffice.abudhabi", "mediaoffice.ae", "dubaimediaoffice.ae", "media.gov.ae",
    "openai.com", "anthropic.com", "deepmind.google", "blog.google", "microsoft.com", "nvidia.com",
    "about.fb.com", "apple.com", "huggingface.co", "mistral.ai",
)
MIN_ASPECT, MAX_ASPECT = 0.8, 1.91   # LinkedIn feed: 4:5 portrait to 1.91:1 landscape
MAX_LONG = 2400
_OG = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)', re.I)
_OG2 = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)', re.I)


@dataclass
class Fetched:
    path: str            # relative to the workspace, e.g. images/ab12.jpg
    origin: str          # press | stock | brand | link
    credit: str
    source_url: str
    width: int
    height: int
    download_ping: str = ""   # Unsplash: must be called when the photo is used


@dataclass
class MediaSession:
    settings: Settings
    workspace: Path
    press_urls: set[str] = field(default_factory=set)
    stock: dict[str, dict] = field(default_factory=dict)   # url -> {credit, ping}
    fetched: dict[str, Fetched] = field(default_factory=dict)
    tools: dict = field(default_factory=dict)

    @property
    def images_dir(self) -> Path:
        d = self.workspace / "images"
        d.mkdir(parents=True, exist_ok=True)
        return d


# ---------------------------------------------------------------- pure image work


def normalise(raw: bytes, min_long: int, min_short: int) -> tuple[bytes, dict] | tuple[None, str]:
    """Clean JPEG within LinkedIn's aspect range, or (None, reason)."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:  # noqa: BLE001
        return None, "not a readable image"
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB",):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) < min_long or min(w, h) < min_short:
        return None, f"too small ({w}x{h}); need at least {min_long}px on the long edge and {min_short}px on the short"
    ratio = w / h
    cropped = False
    if ratio > MAX_ASPECT:          # too wide: centre-crop width
        nw = int(h * MAX_ASPECT)
        x = (w - nw) // 2
        img = img.crop((x, 0, x + nw, h))
        cropped = True
    elif ratio < MIN_ASPECT:        # too tall: crop toward the top third (faces, headlines)
        nh = int(w / MIN_ASPECT)
        y = min((h - nh) // 3, h - nh)
        img = img.crop((0, y, w, y + nh))
        cropped = True
    w, h = img.size
    if max(w, h) > MAX_LONG:
        scale = MAX_LONG / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    small = img.convert("L")
    small.thumbnail((640, 640))
    sharp = ImageStat.Stat(small.filter(ImageFilter.FIND_EDGES)).stddev[0]
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True, progressive=True)
    return out.getvalue(), {"width": img.size[0], "height": img.size[1], "cropped": cropped,
                            "sharpness": round(sharp, 1), "soft": sharp < 9}


def _ok_press(url: str, session: MediaSession) -> bool:
    if url in session.press_urls:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in PRESS_HOSTS)


async def _download(url: str, s: Settings, referer: str = "") -> bytes:
    headers = {"User-Agent": s.user_agent, "Accept": "image/avif,image/webp,image/jpeg,image/*;q=0.8"}
    if referer:
        headers["Referer"] = quote(referer, safe=":/?#[]@!$&'()*+,;=%-._~")
    async with httpx.AsyncClient(follow_redirects=True, timeout=40) as c:
        r = await c.get(url, headers=headers)
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}")
    if not r.headers.get("content-type", "").startswith("image/"):
        raise ValueError(f"not an image ({r.headers.get('content-type', '?')})")
    if len(r.content) > 25 * 1024 * 1024:
        raise ValueError("image over 25 MB")
    return r.content


def _save(session: MediaSession, data: bytes, meta: dict, origin: str, credit: str, source_url: str,
          ping: str = "") -> dict:
    name = hashlib.sha1(data).hexdigest()[:14] + ".jpg"
    (session.images_dir / name).write_bytes(data)
    rel = f"images/{name}"
    session.fetched[rel] = Fetched(rel, origin, credit, source_url, meta["width"], meta["height"], ping)
    return {"path": rel, **meta, "origin": origin, "credit": credit,
            "next": f"Read {rel} to look at it before you choose it."}


def _text(obj: Any) -> dict:
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


# ---------------------------------------------------------------- stock search


async def search_pexels(s: Settings, q: str, orientation: str, n: int) -> list[dict]:
    if not s.pexels_api_key:
        return []
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.pexels.com/v1/search", headers={"Authorization": s.pexels_api_key},
                        params={"query": q, "per_page": n, "orientation": orientation, "size": "large"})
    r.raise_for_status()
    return [{
        "provider": "pexels", "url": f"{p['src']['original']}?auto=compress&cs=tinysrgb&w={MAX_LONG}",
        "description": p.get("alt") or "", "width": p["width"], "height": p["height"],
        "credit": f"{p['photographer']} / Pexels", "page": p["url"], "ping": "",
    } for p in r.json().get("photos", [])]


async def search_unsplash(s: Settings, q: str, orientation: str, n: int) -> list[dict]:
    if not s.unsplash_access_key:
        return []
    o = {"square": "squarish"}.get(orientation, orientation)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.unsplash.com/search/photos",
                        headers={"Authorization": f"Client-ID {s.unsplash_access_key}", "Accept-Version": "v1"},
                        params={"query": q, "per_page": n, "orientation": o, "content_filter": "high"})
    r.raise_for_status()
    return [{
        "provider": "unsplash", "url": f"{p['urls']['raw']}&w={MAX_LONG}&q=85&fm=jpg&fit=max",
        "description": p.get("description") or p.get("alt_description") or "",
        "width": p["width"], "height": p["height"],
        "credit": f"{p['user']['name']} / Unsplash", "page": p["links"]["html"],
        "ping": p["links"]["download_location"],
    } for p in r.json().get("results", [])]


def brand_library(s: Settings) -> list[dict]:
    lib = s.brand_dir / "library.yaml"
    if not lib.exists():
        return []
    entries = (yaml.safe_load(lib.read_text()) or {}).get("images", [])
    return [e for e in entries if (s.brand_dir / e.get("file", "")).is_file()]


# ---------------------------------------------------------------- MCP server


def build_server(session: MediaSession):
    s = session.settings

    @tool("search_photos",
          "Search licensed stock photography (Pexels, Unsplash) for an image to post. Use concrete visual "
          "queries ('Dubai skyline at dawn', 'UAE flag on a building facade', 'Ramadan lantern close up'), not "
          "abstract ones ('innovation'). Returns candidates; fetch the promising ones with fetch_image and look "
          "at them.",
          {"type": "object", "properties": {
              "query": {"type": "string"},
              "orientation": {"type": "string", "enum": ["landscape", "portrait", "square"]},
          }, "required": ["query"]})
    async def search_photos(args: dict) -> dict:
        q = (args.get("query") or "").strip()
        if not q:
            return _err("query is empty")
        orient = args.get("orientation") or "landscape"
        results: list[dict] = []
        for fn in (search_pexels, search_unsplash):
            try:
                results += await fn(s, q, orient, 8)
            except Exception as e:  # noqa: BLE001
                log.info("%s failed: %s", fn.__name__, e)
        if not results:
            return _err("no stock provider configured or no results; try another query, a press image, or a text post")
        for r in results:
            session.stock[r["url"]] = {"credit": r["credit"], "ping": r["ping"]}
        return _text([{k: r[k] for k in ("provider", "url", "description", "width", "height", "credit")} for r in results])

    @tool("fetch_image",
          "Download an image you may use, check its size and quality, crop it into LinkedIn's 4:5-1.91:1 range and "
          "save it in images/. origin: 'press' for an official handout listed in a story file (image: "
          "press-handout), 'stock' for a URL returned by search_photos, 'brand' for a file from list_brand_photos "
          "(pass the file name as url). Returns the saved path; Read it to see the picture.",
          {"type": "object", "properties": {
              "url": {"type": "string"},
              "origin": {"type": "string", "enum": ["press", "stock", "brand"]},
              "credit": {"type": "string", "description": "who the photo is by / from, e.g. 'WAM' or 'Dubai Media Office'"},
              "page_url": {"type": "string", "description": "the article page the image came from (press only)"},
          }, "required": ["url", "origin"]})
    async def fetch_image(args: dict) -> dict:
        url, origin = args["url"].strip(), args["origin"]
        credit, ping = (args.get("credit") or "").strip(), ""
        try:
            if origin == "brand":
                p = (s.brand_dir / url).resolve()
                if not p.is_file() or s.brand_dir.resolve() not in p.parents:
                    return _err("no such brand photo; call list_brand_photos")
                raw, credit = p.read_bytes(), credit or "Nakoda AI"
            elif origin == "stock":
                meta = session.stock.get(url)
                if not meta:
                    return _err("only URLs returned by search_photos can be fetched as stock")
                raw, credit, ping = await _download(url, s), meta["credit"], meta["ping"]
            elif origin == "press":
                if not _ok_press(url, session):
                    return _err("that image is not an official handout we may use (outlet photographers' "
                                "pictures belong to the outlet). Use a press-handout image from a story file, "
                                "stock, a brand photo, or post without an image.")
                raw = await _download(url, s, args.get("page_url", ""))
                credit = credit or (urlparse(url).hostname or "")
            else:
                return _err("origin must be press, stock or brand")
        except Exception as e:  # noqa: BLE001
            return _err(f"could not fetch: {type(e).__name__}: {e}")
        data, meta = normalise(raw, s.min_image_long, s.min_image_short)
        if data is None:
            return _err(f"rejected: {meta}")
        return _text(_save(session, data, meta, origin, credit, url, ping))

    @tool("fetch_link_preview",
          "For an 'article' post linking to a page (e.g. our own news.nakoda.ai story): fetch that page's preview "
          "image (og:image), check it and save it as the link card thumbnail.",
          {"type": "object", "properties": {"page_url": {"type": "string"}}, "required": ["page_url"]})
    async def fetch_link_preview(args: dict) -> dict:
        page = args["page_url"].strip()
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                         headers={"User-Agent": s.user_agent}) as c:
                html = (await c.get(page)).text
            m = _OG.search(html) or _OG2.search(html)
            if not m:
                return _err("the page has no preview image; post the link with text only, or pick another image")
            img_url = httpx.URL(page).join(m.group(1)).__str__()
            raw = await _download(img_url, s, page)
        except Exception as e:  # noqa: BLE001
            return _err(f"could not fetch preview: {type(e).__name__}: {e}")
        data, meta = normalise(raw, 1000, 520)
        if data is None:
            return _err(f"rejected: {meta}")
        return _text(_save(session, data, meta, "link", urlparse(page).hostname or "", page))

    @tool("list_brand_photos", "List Nakoda AI's own photos (team, office, events) with descriptions.",
          {"type": "object", "properties": {}})
    async def list_brand_photos(args: dict) -> dict:
        lib = brand_library(s)
        return _text(lib or "no brand photos in the library yet")

    tools = [search_photos, fetch_image, fetch_link_preview, list_brand_photos]
    session.tools = {t.name: t for t in tools}  # handy for tests
    server = create_sdk_mcp_server(name="media", version="1.0.0", tools=tools)
    names = [f"mcp__media__{t.name}" for t in tools]
    return server, names


def keep(session: MediaSession, rel: str, dest_dir: Path) -> dict | None:
    """Copy a chosen workspace image to permanent storage; return its record."""
    f = session.fetched.get(rel)
    if f is None:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(rel).name
    shutil.copyfile(session.workspace / rel, dest)
    return {"path": str(dest), "origin": f.origin, "credit": f.credit, "source_url": f.source_url,
            "width": f.width, "height": f.height, "download_ping": f.download_ping}
