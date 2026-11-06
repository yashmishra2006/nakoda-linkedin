"""Optional webhook (Slack / Discord / Teams-compatible JSON) so a person sees
every queued post in time to `desk cancel` it."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger("desk")


async def send(s: Settings, message: str) -> None:
    if not s.notify_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(s.notify_webhook_url, json={"text": message[:3800], "content": message[:1900]})
    except Exception as e:  # noqa: BLE001 - notifications never block posting
        log.info("notify failed: %s", e)


def preview(post: dict, when_local: str) -> str:
    imgs = len(post.get("images") or [])
    tags = " ".join("#" + h for h in post.get("hashtags") or [])
    return (f"[Nakoda LinkedIn] #{post['id']} {post['kind']} / {post['format']}"
            f"{f' / {imgs} image(s)' if imgs else ''}, {post['status']} for {when_local}\n\n"
            f"{post['text']}\n{tags}\n\n"
            f"Cancel: python -m desk cancel {post['id']}"
            + (f"   Approve: python -m desk approve {post['id']}" if post["status"] == "awaiting_approval" else ""))
