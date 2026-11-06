"""One planning pass: situation -> Claude planner -> critic + style checks ->
scheduled posts in the ledger.

Runs from `tick` only when there is a reason to (urgent news, an occasion
coming up, or a regular post wanted and the planner hasn't looked for a
while), so most ticks cost nothing.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime, timedelta

from . import agent, cadence, media, notify, occasions, signals, style
from .config import PRESS_SOURCES, Settings
from .store import Store, iso, now_utc, parse_iso

log = logging.getLogger("desk")
MIN_CRITIC_SCORE = 7
SOLEMN_KINDS = {"condolence"}


# ---------------------------------------------------------------- situation


def mourning(store: Store, today: date) -> dict | None:
    m = store.get_state("mourning")
    if m and m.get("until") and date.fromisoformat(m["until"]) >= today:
        return m
    return None


def history(store: Store, s: Settings) -> list[dict]:
    rows = store.published(30, include_dry=s.dry_run)
    out = []
    for p in rows:
        when = parse_iso(p["published_at"]).astimezone(s.tz)
        out.append({
            "id": p["id"], "date": when.strftime("%Y-%m-%d %a %H:%M"), "kind": p["kind"], "format": p["format"],
            "tone": p["tone"], "opening_style": p["opening_style"], "length": p["length_band"],
            "words": len(style.words(p["text"])), "first_line": p["text"].strip().splitlines()[0][:160],
            "hashtags": p["hashtags"], "occasion_key": p["occasion_key"], "story_id": p["story_id"],
        })
    return out


def situation(store: Store, s: Settings, now: datetime) -> dict:
    tz = s.tz
    today = now.astimezone(tz).date()
    published = store.published(60, include_dry=s.dry_run)
    pending = store.pending()
    view = cadence.view(s, now, published, pending)
    mourn = mourning(store, today)
    occ = occasions.upcoming(store, s.calendar_file, today, days=14)
    occ_list = []
    for o in occ:
        d = o.as_dict()
        handled = store.occasion_handled(o.instance)
        due_soon = 0 <= (o.date - today).days <= s.occasion_lookahead_days
        blocked = mourn is not None and o.tone != "solemn"
        d["already_has_post"] = handled
        d["needs_post"] = (not handled) and due_soon and not blocked and o.date >= today
        occ_list.append(d)
    sigs = [sg for sg in signals.collect(s, store.seen_signals()) if not store.story_handled(sg["story_id"])]
    sigs = sigs[: s.max_signals]
    return {"today": today, "view": view, "mourning": mourn, "occasions": occ_list, "occ_objs": occ,
            "signals": sigs, "pending": pending, "published": published}


def should_plan(store: Store, s: Settings, sit: dict, now: datetime, force: bool) -> str | None:
    if force:
        return "forced"
    urgent = [sg for sg in sit["signals"] if sg["urgent"] and not sg["already_seen_by_planner"]]
    if urgent:
        return f"urgent signal: {urgent[0]['headline'][:80]}"
    if sit["mourning"]:
        return None
    if any(o["needs_post"] for o in sit["occasions"]):
        return "occasion coming up: " + ", ".join(o["name"] for o in sit["occasions"] if o["needs_post"])
    local = now.astimezone(s.tz)
    last = parse_iso(store.get_state("last_plan_at"))
    fresh = [sg for sg in sit["signals"] if not sg["already_seen_by_planner"]]
    if (sit["view"].want_regular and fresh and 7 <= local.hour < 19 and local.weekday() in cadence.WORKDAYS
            and (last is None or now - last >= timedelta(hours=s.plan_interval_hours))):
        return f"regular post wanted ({sit['view'].reason}); {len(fresh)} new signals"
    return None


RULES = """\
RULES FROM THE SCHEDULER (hard limits; the code enforces them too)
- At most one regular post per day, and none at all when brief.md says no regular post is wanted.
- Condolence posts: 15-120 words, no emoji, no hashtags, no link, no image unless it is the official
  WAM portrait handout; tone solemn.
- 0-3 hashtags (none in the text itself), plain words people follow. Emoji: 0-2, never hype emoji.
- Under 3000 characters. No markdown, no bullet lists, no bold.
- Never post the same story or occasion twice: occasions.json and history.json show what is done.
"""


def write_brief(sit: dict, s: Settings, now: datetime, reason: str) -> str:
    v = sit["view"]
    local = now.astimezone(s.tz)
    lines = [
        "# Brief", "",
        f"Now in Dubai: {local.strftime('%A %d %B %Y, %H:%M')}",
        f"Why you were called: {reason}", "",
        "## Rhythm",
        f"- Regular posts this week: {v.regular_this_week} of a budget of {v.weekly_budget} "
        f"(the budget varies week to week on purpose).",
        f"- Last post: {v.last_post_at.strftime('%A %d %B %H:%M') if v.last_post_at else 'never'}"
        + (f" ({v.days_since_last:.1f} days ago)" if v.days_since_last is not None else ""),
        f"- Regular post wanted now: {'YES' if v.want_regular else 'NO'} ({v.reason})",
        f"- If you write one with slot next_natural it would go out around "
        f"{v.next_regular_slot.strftime('%A %H:%M') if v.next_regular_slot else '-'}.", "",
    ]
    if sit["mourning"]:
        m = sit["mourning"]
        lines += ["## MOURNING PERIOD", f"Official mourning until {m['until']}: {m.get('reason', '')}.",
                  "Post nothing except a condolence post if we have not posted one.", ""]
    if sit["pending"]:
        lines += ["## Already queued (do not duplicate)"]
        for p in sit["pending"]:
            when = parse_iso(p["scheduled_at"]).astimezone(s.tz).strftime("%a %d %b %H:%M")
            lines.append(f"- #{p['id']} {p['kind']} {p['occasion_key'] or p['story_id']} at {when}: "
                         f"{p['text'][:90]!r}")
        lines.append("")
    needs = [o for o in sit["occasions"] if o["needs_post"]]
    lines += ["## Occasions needing a post now",
              *(f"- {o['name']} ({o['occasion_key']}) on {o['weekday']} {o['date']}, importance {o['importance']}, "
                f"tone {o['tone']}, date {o['date_status']}" for o in needs), *([] if needs else ["- none"]), ""]
    urgent = [sg for sg in sit["signals"] if sg["urgent"]]
    if urgent:
        lines += ["## Possibly urgent (check carefully)",
                  *(f"- {sg['story_id']}: {sg['headline']} [{sg['mood_guess']}]" for sg in urgent), ""]
    return "\n".join(lines)


def build_workspace(s: Settings, sit: dict, run_id: str, now: datetime, reason: str, our_news: list[dict]):
    work = s.work_dir / run_id
    if work.exists():
        shutil.rmtree(work)
    (work / "images").mkdir(parents=True)
    for f in ("VOICE.md", "EXAMPLES.md"):
        shutil.copyfile(s.voice_dir / f, work / f)
    signals.write_story_files(sit["signals"], work / "signals")
    (work / "signals.json").write_text(json.dumps(signals.public(sit["signals"]), ensure_ascii=False, indent=1))
    (work / "occasions.json").write_text(json.dumps(sit["occasions"], ensure_ascii=False, indent=1))
    (work / "our_news.json").write_text(json.dumps(our_news, ensure_ascii=False, indent=1, default=str))
    (work / "brief.md").write_text(write_brief(sit, s, now, reason))
    return work


# ---------------------------------------------------------------- checking a draft


def structural_problems(p: dict, sess: media.MediaSession, valid_occ: set[str], valid_story: set[str]) -> list[str]:
    errs = []
    if p["kind"] == "occasion" and p["occasion_key"] not in valid_occ:
        errs.append(f"occasion_key {p['occasion_key']!r} is not an occasion that needs a post")
    if p["kind"] in {"news", "celebration", "condolence"} and p["story_id"] and p["story_id"] not in valid_story:
        errs.append(f"story_id {p['story_id']!r} is not in signals.json")
    n = len(p["images"])
    want = {"text": (0, 0), "image": (1, 1), "multi_image": (2, 6), "article": (0, 1)}[p["format"]]
    if not want[0] <= n <= want[1]:
        errs.append(f"format {p['format']} needs {want[0]}-{want[1]} images, got {n}")
    for im in p["images"]:
        if im["path"] not in sess.fetched:
            errs.append(f"image {im['path']} was not fetched with the media tools")
        if p["format"] != "article" and im["path"] in sess.fetched and sess.fetched[im["path"]].origin == "link":
            errs.append("a link preview image can only be the thumbnail of an article post")
    if p["format"] == "article" and not p["link_url"].startswith("https://"):
        errs.append("article posts need an https link_url")
    if p["kind"] == "condolence" and (p["hashtags"] or p["format"] == "article"):
        errs.append("condolence posts take no hashtags and no link")
    return errs


async def review(s: Settings, work, p: dict, recent: list[dict]) -> tuple[dict | None, dict]:
    """Critic (up to two rounds) + style checks. Returns (final post | None, review log)."""
    log_: dict = {"rounds": []}
    lint = style.check(p["text"], p["hashtags"], p["kind"], p["tone"], recent)
    for rnd in (1, 2):
        (work / "draft.json").write_text(json.dumps(p, ensure_ascii=False, indent=1))
        out, stats = await agent.critique(s, work, lint)
        log_["rounds"].append({"lint_before": lint, "critic": out, "stats": stats})
        if not out:
            log_["final"] = "critic produced no verdict"
            return None, log_
        if out["verdict"] == "reject":
            log_["final"] = "rejected by critic: " + "; ".join(out["issues"])[:400]
            return None, log_
        low = {k: v for k, v in out["scores"].items() if v < MIN_CRITIC_SCORE}
        if low and out["verdict"] == "approve":
            log_["final"] = f"approved with low scores {low}; treated as reject"
            return None, log_
        p = {**p, "text": out["text"].strip(), "hashtags": [h.lstrip("#") for h in out["hashtags"]]}
        if out["drop_images"]:
            p["images"] = []
            p["format"] = "article" if p["format"] == "article" else "text"
        if out["alt_texts"] and len(out["alt_texts"]) == len(p["images"]):
            p["images"] = [{**im, "alt_text": a} for im, a in zip(p["images"], out["alt_texts"])]
        lint = style.check(p["text"], p["hashtags"], p["kind"], p["tone"], recent)
        if not lint:
            log_["final"] = f"{out['verdict']} (round {rnd})"
            log_["scores"] = out["scores"]
            return p, log_
    log_["final"] = "style checks still failing after two critic rounds: " + "; ".join(lint)[:400]
    return None, log_


# ---------------------------------------------------------------- scheduling


def slot_for(s: Settings, p: dict, occ_by_instance: dict, now: datetime, taken: list[datetime]) -> datetime:
    seed = p["story_id"] or p["occasion_key"] or p["text"][:20]
    if p["kind"] == "condolence":
        return cadence.urgent_slot(s, now, seed)
    if p["slot"] == "urgent":
        when = cadence.urgent_slot(s, now, seed)
        if cadence.fits_gap(s, when, taken, urgent=True):
            return when
        return cadence.next_regular_slot(s, now.astimezone(s.tz), taken, seed=seed)
    if p["occasion_key"] and p["occasion_key"] in occ_by_instance:
        o = occ_by_instance[p["occasion_key"]]
        when = cadence.occasion_slot(s, o.date, o.post_time, o.instance)
        earliest = now + timedelta(minutes=s.hold_minutes)
        if when < earliest:
            # Planned late on the day itself: still post, after the hold window.
            when = cadence.human_minute(earliest.astimezone(s.tz) + timedelta(minutes=7), cadence._rng(seed))
        return when
    return cadence.next_regular_slot(s, now.astimezone(s.tz), taken, seed=seed)


async def run(store: Store, s: Settings, *, force: bool = False) -> dict:
    now = now_utc()
    sit = situation(store, s, now)
    reason = should_plan(store, s, sit, now, force)
    if not reason:
        return {"planned": False, "why": "nothing to plan", "cadence": sit["view"].as_dict()}
    run_id = "plan-" + now.astimezone(s.tz).strftime("%Y%m%d-%H%M%S")
    store.start_run(run_id, "plan")
    report: dict = {"run_id": run_id, "reason": reason, "cadence": sit["view"].as_dict(), "posts": [], "rejected": []}
    log.info("planning (%s)", reason)
    try:
        our_news = await signals.nakoda_news_recent(s)
        work = build_workspace(s, sit, run_id, now, reason, our_news)
        (work / "history.json").write_text(json.dumps(history(store, s), ensure_ascii=False, indent=1))
        press = {it["image"] for sg in sit["signals"] for it in sg["_items"]
                 if it.get("image") and it["source_id"] in PRESS_SOURCES}
        sess = media.MediaSession(s, work, press_urls=press)
        server, tool_names = media.build_server(sess)
        out, stats = await agent.plan(s, work, server, tool_names, RULES)
        report["planner"] = stats
        store.set_state("last_plan_at", iso(now))
        if not out:
            report["error"] = "planner returned nothing"
            return report
        report["note"] = out.get("note", "")
        report["passed_on"] = out.get("passed_on", [])

        # Mark what the planner has looked at, so the same news doesn't wake it again.
        counts = {sg["story_id"]: sg["item_count"] for sg in sit["signals"]}
        for sg in out.get("passed_on", []):
            if sg["story_id"] in counts:
                store.mark_seen(sg["story_id"], counts[sg["story_id"]], "passed: " + sg["reason"])
        already = store.seen_signals()
        for sid, n in counts.items():
            if sid not in already:
                store.mark_seen(sid, n, "shown")

        m = out.get("mourning") or {}
        if m.get("active") and m.get("until"):
            store.set_state("mourning", {"until": m["until"], "reason": m.get("reason", ""), "source": m.get("source", "")})
            for p in store.pending():
                if p["kind"] not in SOLEMN_KINDS and p["tone"] != "solemn":
                    store.update_post(p["id"], status="cancelled", error=f"mourning until {m['until']}")
            report["mourning"] = m
            log.warning("mourning period until %s: %s", m["until"], m.get("reason"))

        valid_occ = {o["occasion_key"] for o in sit["occasions"] if o["needs_post"]}
        valid_story = {sg["story_id"] for sg in sit["signals"]}
        occ_by_instance = {o.instance: o for o in sit["occ_objs"]}
        recent = store.published(40, include_dry=s.dry_run)
        mourn_active = mourning(store, sit["today"]) is not None
        regular_left = sit["view"].want_regular

        for p in out.get("posts", [])[:3]:
            p["hashtags"] = [h.lstrip("#").strip() for h in p["hashtags"] if h.strip()]
            errs = structural_problems(p, sess, valid_occ, valid_story)
            if mourn_active and p["kind"] != "condolence":
                errs.append("mourning period: only a condolence post may go out")
            if p["kind"] in cadence.REGULAR_KINDS and p["slot"] != "urgent":
                if not regular_left:
                    errs.append("no regular post wanted right now")
                regular_left = False
            if errs:
                report["rejected"].append({"post": p["text"][:120], "why": errs})
                log.warning("draft rejected: %s", errs)
                continue
            final, rev = await review(s, work, p, recent)
            if final is None:
                report["rejected"].append({"post": p["text"][:120], "why": rev.get("final")})
                log.warning("draft rejected: %s", rev.get("final"))
                continue
            taken = [parse_iso(x["published_at"]) for x in recent if x.get("published_at")]
            taken += [parse_iso(x["scheduled_at"]) for x in store.pending() if x.get("scheduled_at")]
            taken = [t.astimezone(s.tz) for t in taken if t]
            when = slot_for(s, final, occ_by_instance, now, taken)
            status = "awaiting_approval" if s.review_mode == "approve" else "scheduled"
            post_id = store.add_post(
                kind=final["kind"], occasion_key=final["occasion_key"], story_id=final["story_id"], status=status,
                urgent=int(final["slot"] == "urgent" or final["kind"] == "condolence"), scheduled_at=iso(when),
                text=final["text"], hashtags=final["hashtags"], format=final["format"], images=[],
                link_url=final["link_url"], link_title=final["link_title"], tone=final["tone"],
                opening_style=final["opening_style"], length_band=style.length_band(final["text"]),
                reason=final["why_post"], facts=final["facts"], critic=rev, run_id=run_id,
            )
            kept = []
            for im in final["images"]:
                rec = media.keep(sess, im["path"], s.media_dir / str(post_id))
                if rec:
                    kept.append({**rec, "alt_text": im["alt_text"]})
            store.update_post(post_id, images=kept)
            if final["story_id"] in counts:
                store.mark_seen(final["story_id"], counts[final["story_id"]], f"posted #{post_id}")
            row = store.get_post(post_id)
            local = when.astimezone(s.tz).strftime("%a %d %b %H:%M")
            await notify.send(s, notify.preview(row, local))
            report["posts"].append({"id": post_id, "kind": final["kind"], "when": local, "status": status,
                                    "first_line": final["text"].splitlines()[0][:100]})
            log.info("queued #%d %s for %s", post_id, final["kind"], local)
        return report
    finally:
        store.finish_run(run_id, report)
