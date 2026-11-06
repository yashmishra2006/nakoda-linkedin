"""Publish queued posts whose time has come, after last-minute checks:
mourning, confirmed occasion dates, spacing from the previous post."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import httpx

from . import cadence, notify, occasions
from .config import Settings
from .linkedin import LinkedIn, LinkedInError, post_url
from .planner import SOLEMN_KINDS, mourning
from .store import Store, iso, now_utc, parse_iso

log = logging.getLogger("desk")
MAX_ATTEMPTS = 3


def _occasion_check(store: Store, s: Settings, post: dict) -> tuple[str, object]:
    """('ok'|'wait'|'move'|'cancel', detail) for an occasion post about to go out."""
    if not post["occasion_key"]:
        return "ok", None
    today = now_utc().astimezone(s.tz).date()
    occ = {o.instance: o for o in occasions.upcoming(store, s.calendar_file, today, days=20, back=4)}
    o = occ.get(post["occasion_key"])
    if o is None:
        return "ok", None  # discovered event or calendar edited; trust the queue
    if not o.publishable:
        if (today - o.date).days > 2:
            return "cancel", "date never confirmed"
        return "wait", "awaiting the official date announcement"
    slot = cadence.occasion_slot(s, o.date, o.post_time, o.instance)
    sched = parse_iso(post["scheduled_at"]).astimezone(s.tz)
    if sched.date() != slot.date():
        return "move", slot
    return "ok", None


def _preview(s: Settings, post: dict) -> Path:
    s.previews_dir.mkdir(parents=True, exist_ok=True)
    f = s.previews_dir / f"{post['id']:04d}-{post['kind']}.md"
    imgs = "\n".join(f"![{im.get('alt_text', '')}]({im['path']})  \n_{im.get('credit', '')}_" for im in post["images"])
    tags = " ".join("#" + h for h in post["hashtags"])
    when = parse_iso(post["scheduled_at"]).astimezone(s.tz).strftime("%A %d %B %Y %H:%M")
    critic = post.get("critic") or {}
    f.write_text(
        f"# #{post['id']} {post['kind']} / {post['format']} / {post['tone']}\n\n"
        f"Would post: {when} (Dubai)\nWhy: {post['reason']}\nCritic: {critic.get('final', '')} "
        f"{critic.get('scores', '')}\n\n---\n\n{post['text']}\n\n{tags}\n\n"
        + (f"Link: {post['link_url']}\n\n" if post["link_url"] else "") + imgs + "\n\n---\nFacts:\n"
        + "\n".join(f"- {x['claim']} ({x['source']})" for x in post["facts"]) + "\n")
    return f


async def _ping_unsplash(s: Settings, images: list[dict]) -> None:
    for im in images:
        if im.get("download_ping") and s.unsplash_access_key:
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    await c.get(im["download_ping"], headers={"Authorization": f"Client-ID {s.unsplash_access_key}"})
            except Exception:  # noqa: BLE001
                pass


async def publish_due(store: Store, s: Settings) -> list[dict]:
    now = now_utc()
    today = now.astimezone(s.tz).date()
    mourn = mourning(store, today)
    done = []
    client: LinkedIn | None = None
    for post in store.pending():
        if post["status"] != "scheduled":
            continue
        sched = parse_iso(post["scheduled_at"])
        if mourn and post["kind"] not in SOLEMN_KINDS and post["tone"] != "solemn":
            store.update_post(post["id"], status="cancelled", error=f"mourning until {mourn['until']}")
            continue
        if sched > now:
            continue
        if now - sched > timedelta(hours=30) and not post["occasion_key"]:
            store.update_post(post["id"], status="cancelled", error="went stale in the queue")
            continue
        verdict, detail = _occasion_check(store, s, post)
        if verdict == "wait":
            store.update_post(post["id"], scheduled_at=iso(now + timedelta(hours=1)), error=str(detail))
            continue
        if verdict == "cancel":
            store.update_post(post["id"], status="cancelled", error=str(detail))
            continue
        if verdict == "move":
            store.update_post(post["id"], scheduled_at=iso(detail), error="moved to the confirmed date")
            log.info("#%d moved to confirmed date %s", post["id"], detail)
            continue
        # Spacing: never two posts close together (urgent ones need less room).
        last = store.published(1, include_dry=s.dry_run)
        if last and not post["urgent"] and not post["occasion_key"]:
            gap_h = (now - parse_iso(last[0]["published_at"])).total_seconds() / 3600
            if gap_h < s.min_gap_hours:
                taken = [parse_iso(last[0]["published_at"]).astimezone(s.tz)]
                nxt = cadence.next_regular_slot(s, now.astimezone(s.tz), taken, seed=post["id"])
                store.update_post(post["id"], scheduled_at=iso(nxt), error="spaced out from the previous post")
                continue

        if s.dry_run:
            f = _preview(s, post)
            store.update_post(post["id"], status="dry_published", published_at=iso(now), linkedin_urn="dry-run")
            log.info("dry run: #%d written to %s", post["id"], f)
            done.append({"id": post["id"], "dry_run": str(f)})
            continue
        try:
            client = client or await LinkedIn.connect(s)
            urn = await client.create_post(post["text"], post["hashtags"], post["format"], post["images"],
                                           post["link_url"], post["link_title"])
        except (LinkedInError, httpx.HTTPError) as e:
            attempts = post["attempts"] + 1
            transient = any(code in str(e) for code in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")) \
                or isinstance(e, httpx.HTTPError)
            if transient and attempts < MAX_ATTEMPTS:
                store.update_post(post["id"], attempts=attempts, error=str(e)[:500],
                                  scheduled_at=iso(now + timedelta(minutes=20 * attempts)))
            else:
                store.update_post(post["id"], attempts=attempts, status="failed", error=str(e)[:500])
                await notify.send(s, f"[Nakoda LinkedIn] post #{post['id']} FAILED: {e}")
            log.error("publish #%d failed: %s", post["id"], e)
            continue
        store.update_post(post["id"], status="published", published_at=iso(now_utc()), linkedin_urn=urn, error="")
        await _ping_unsplash(s, post["images"])
        await notify.send(s, f"[Nakoda LinkedIn] published #{post['id']}: {post_url(urn)}")
        log.info("published #%d %s", post["id"], post_url(urn))
        done.append({"id": post["id"], "urn": urn})
    return done
