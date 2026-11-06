"""What is happening: stories from the Nakoda News collector (read-only), our
own published Nakoda News articles, and a first sort into happy / sad /
national-mourning so the planner can react to the right things promptly.

The collector already scrapes WAM, the media offices, Gulf News, Khaleej
Times, The National and the AI labs every hour; this module only reads its
SQLite database.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx

from .config import PRESS_SOURCES, Settings
from .store import iso, now_utc

log = logging.getLogger("desk")

SAD = re.compile(
    r"\b(pass(ed|es)? away|death of|dies\b|died\b|obituar|mourn|condolence|funeral|killed|martyr|tragedy|"
    r"victims?|earthquake|flood(s|ing)?|crash|collapse|fire kills|lost (his|her|their) li(fe|ves))", re.I)
SAD_AR = re.compile(r"(نعى|ينعى|نعت|الحداد|وفاة|تعاز|عزاء|استشهاد|شهداء|فقيد|رحيل)")
MOURNING = re.compile(r"(official mourning|mourning period|days of mourning|half[- ]mast|"
                      r"الحداد الرسمي|تنكيس الأعلام|إعلان الحداد)", re.I)
HAPPY = re.compile(
    r"\b(wins?|won|award(ed|s)?|record|first[- ]ever|world'?s first|ranks? first|tops|celebrat|milestone|"
    r"achiev|historic|landmark|successful(ly)?|launch(es|ed)?|inaugurat|honou?r(s|ed)?|crowned)", re.I)
HAPPY_AR = re.compile(r"(يفوز|فوز|إنجاز|قياسي|الأول من نوعه|تتويج|يحتفي|احتفال|يدشن|افتتاح)")
LEADERSHIP = re.compile(
    r"(Mohamed bin Zayed|Mohammed bin Rashid|Hamdan bin Mohammed|Khaled bin Mohamed|Al Nahyan|Al Maktoum|"
    r"Al Qasimi|Al Nuaimi|Al Mualla|Al Sharqi|Ruler of|Crown Prince|President of the UAE|"
    r"محمد بن زايد|محمد بن راشد|حمدان بن محمد|آل نهيان|آل مكتوم|حاكم)", re.I)


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        log.warning("collector db %s not found; no news signals this run", path)
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _classify(text: str) -> dict:
    return {
        "sad": bool(SAD.search(text) or SAD_AR.search(text)),
        "mourning": bool(MOURNING.search(text)),
        "happy": bool(HAPPY.search(text) or HAPPY_AR.search(text)),
        "leadership": bool(LEADERSHIP.search(text)),
    }


def _image_use(item: dict) -> str:
    if not item.get("image"):
        return "none"
    return "press-handout (usable)" if item["source_id"] in PRESS_SOURCES else "outlet photo (NOT usable: their copyright)"


def collect(s: Settings, seen: dict[str, int]) -> list[dict]:
    """Stories with reporting in the last signal_hours, scored, strongest first."""
    db = _connect_ro(s.news_db_path)
    if db is None:
        return []
    since = iso(now_utc() - timedelta(hours=s.signal_hours))
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT id, story_id, url, source_id, source_name, publisher, region, lang, title, summary, text, "
            "image, published_at, first_seen_at, topics, text_status FROM items "
            "WHERE first_seen_at >= ? AND story_id IS NOT NULL ORDER BY first_seen_at", (since,))]
    finally:
        db.close()
    by_story: dict[str, list[dict]] = {}
    for r in rows:
        r["topics"] = json.loads(r.get("topics") or "[]")
        by_story.setdefault(r["story_id"], []).append(r)

    out = []
    for sid, items in by_story.items():
        lead = max(items, key=lambda m: (m["text_status"] == "ok", m["lang"] == "en", m["region"] != "global"))
        blob = " ".join(f"{m['title']} {m['summary']} {(m['text'] or '')[:1500]}" for m in items)
        cls = _classify(blob)
        publishers = sorted({m["publisher"] or m["source_name"] for m in items})
        topics = sorted({t for m in items for t in m["topics"]})
        regions = sorted({m["region"] for m in items})
        uae = "uae" in regions or "uae" in topics
        press = any(m["source_id"] in PRESS_SOURCES for m in items)
        score = 2.0 * len(publishers) + (3 if uae else 0) + (2 if press else 0) + (2 if "ai" in topics else 0)
        score += 3 if cls["mourning"] else 0
        score += 2 if (cls["leadership"] and uae) else 0
        urgent = cls["mourning"] or (cls["sad"] and uae and (press or cls["leadership"] or len(publishers) >= 3))
        mood = "sad" if (cls["sad"] or cls["mourning"]) else ("happy" if cls["happy"] else "neutral")
        prev = seen.get(sid)
        out.append({
            "story_id": sid,
            "headline": lead["title"],
            "mood_guess": mood,
            "national_mourning_words": cls["mourning"],
            "mentions_leadership": cls["leadership"],
            "urgent": urgent,
            "uae": uae,
            "score": round(score, 1),
            "outlets": publishers[:10],
            "topics": topics,
            "latest": max((m["published_at"] or m["first_seen_at"]) for m in items),
            "has_usable_image": any(_image_use(m).startswith("press") for m in items),
            "already_seen_by_planner": prev is not None and len(items) <= prev,
            "item_count": len(items),
            "_items": items,
        })
    out.sort(key=lambda x: (x["urgent"], x["score"], x["latest"]), reverse=True)
    return out


def write_story_files(signals: list[dict], folder: Path, chars: int = 5000) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for sg in signals:
        lines = [f"# {sg['headline']}", "", f"story_id: {sg['story_id']}",
                 f"mood_guess: {sg['mood_guess']} | urgent: {sg['urgent']} | outlets: {', '.join(sg['outlets'])}", ""]
        for it in sorted(sg["_items"], key=lambda m: (m["text_status"] == "ok", len(m["text"] or "")), reverse=True)[:5]:
            use = _image_use(it)
            lines += ["---", f"## [{it['id']}] {it['title']}",
                      f"publisher: {it['publisher'] or it['source_name']} | published: {it['published_at'] or '?'}",
                      f"url: {it['url']}",
                      f"image: {use}" + (f" -> {it['image']}" if use.startswith("press") else ""), ""]
            text = (it["text"] or "").strip() or (it["summary"] or "(headline only)")
            lines.append(text[:chars] + (" [...]" if len(text) > chars else ""))
            lines.append("")
        (folder / f"{sg['story_id']}.md").write_text("\n".join(lines))


def public(signals: list[dict]) -> list[dict]:
    return [{k: v for k, v in sg.items() if not k.startswith("_")} | {"file": f"signals/{sg['story_id']}.md"}
            for sg in signals]


async def nakoda_news_recent(s: Settings, hours: int = 72) -> list[dict]:
    """Our own published articles on news.nakoda.ai (optional)."""
    if not (s.news_api_base and s.news_api_token):
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{s.news_api_base}/newsroom/recent", params={"hours": hours},
                            headers={"X-Newsroom-Token": s.news_api_token})
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 - optional input
        log.info("nakoda news recent unavailable: %s", e)
        return []
    return [{"title": a["title"], "url": f"{s.news_site_url}/article/{a['slug']}", "story_id": a.get("story_id", ""),
             "published_at": a.get("published_at")}
            for a in r.json() if str(a.get("status", "")).lower().endswith("published")]
