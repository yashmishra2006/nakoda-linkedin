"""End to end without Claude or LinkedIn: a fake collector DB with a WAM
story, a stubbed planner and critic, then a dry-run publish."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

from desk import agent, media, planner, publisher
from desk.config import settings
from desk.store import Store, iso, now_utc

from .test_units import _jpeg

ITEMS = """CREATE TABLE items (id TEXT, story_id TEXT, url TEXT, source_id TEXT, source_name TEXT, publisher TEXT,
region TEXT, lang TEXT, title TEXT, summary TEXT, text TEXT, image TEXT, published_at TEXT, first_seen_at TEXT,
topics TEXT, text_status TEXT)"""


def _news_db(path):
    db = sqlite3.connect(path)
    db.execute(ITEMS)
    t = iso(now_utc() - timedelta(hours=2))
    rows = [
        ("i1", "s1", "https://wam.ae/a", "wam-en", "WAM", "WAM", "uae", "en",
         "UAE astronaut completes record mission", "summary", "Long text " * 50,
         "https://www.wam.ae/img/hero.jpg", t, t, json.dumps(["uae", "space"]), "ok"),
        ("i2", "s1", "https://gulfnews.com/a", "gulf-news", "Gulf News", "Gulf News", "uae", "en",
         "Astronaut back home after record mission", "", "Text " * 40, "https://gulfnews.com/p.jpg", t, t,
         json.dumps(["uae"]), "ok"),
    ]
    db.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


def test_plan_review_schedule_and_dry_publish(tmp_path, monkeypatch):
    news = tmp_path / "news.db"
    _news_db(news)
    s = replace(settings, data_dir=tmp_path / "data", news_db_path=news, dry_run=True, notify_webhook_url="")
    store = Store(s.db_path)

    good_text = ("Sultan and the crew landed this morning after the longest mission an Arab astronaut has flown, "
                 "according to the Mohammed bin Rashid Space Centre.\n\nWhat stays with us is how much of the "
                 "preparation happened here in the UAE, by people who were students when the programme began. "
                 "Congratulations to the whole team.")

    async def fake_plan(s_, work, server, tools, rules):
        # Pretend Claude fetched and looked at the official WAM photo.
        sess = server_session["s"]
        data, meta = media.normalise(_jpeg(2000, 1300), s_.min_image_long, s_.min_image_short)
        rec = media._save(sess, data, meta, "press", "WAM", "https://www.wam.ae/img/hero.jpg")
        assert (work / "brief.md").exists() and (work / "signals" / "s1.md").exists()
        assert "NOT usable" in (work / "signals" / "s1.md").read_text()  # Gulf News photo flagged
        return {
            "mourning": {"active": False, "until": "", "reason": "", "source": ""},
            "posts": [{
                "kind": "celebration", "occasion_key": "", "story_id": "s1", "slot": "next_natural",
                "tone": "warm", "format": "image", "opening_style": "news-lede", "text": good_text,
                "hashtags": ["UAE"], "images": [{"path": rec["path"], "alt_text": "The crew after landing."}],
                "link_url": "", "link_title": "", "facts": [{"claim": "landed", "source": "signals/s1.md"}],
                "why_post": "national achievement",
            }],
            "passed_on": [], "note": "one post",
        }, {"cost_usd": 0}

    async def fake_critique(s_, work, lint):
        d = json.loads((work / "draft.json").read_text())
        return {"verdict": "approve", "scores": {"human": 9, "accuracy": 9, "tone": 9, "worth": 8, "image": 9},
                "issues": [], "text": d["text"], "hashtags": d["hashtags"], "drop_images": False,
                "alt_texts": []}, {}

    server_session: dict = {}
    real_build = media.build_server

    def capture_build(sess):
        server_session["s"] = sess
        return real_build(sess)

    monkeypatch.setattr(agent, "plan", fake_plan)
    monkeypatch.setattr(agent, "critique", fake_critique)
    monkeypatch.setattr(media, "build_server", capture_build)

    report = asyncio.run(planner.run(store, s, force=True))
    assert report["posts"], report
    post = store.get_post(report["posts"][0]["id"])
    assert post["status"] == "scheduled" and len(post["images"]) == 1
    assert post["images"][0]["origin"] == "press"
    sched = post["scheduled_at"]
    local = __import__("desk.store", fromlist=["parse_iso"]).parse_iso(sched).astimezone(s.tz)
    assert local.weekday() < 5

    # Second pass does not duplicate the story.
    assert store.story_handled("s1")

    # Make it due and publish in dry-run mode.
    store.update_post(post["id"], scheduled_at=iso(now_utc() - timedelta(minutes=1)))
    done = asyncio.run(publisher.publish_due(store, s))
    assert done and done[0]["id"] == post["id"]
    assert store.get_post(post["id"])["status"] == "dry_published"
    assert list((s.data_dir / "previews").glob("*.md"))


def test_mourning_cancels_celebrations(tmp_path):
    s = replace(settings, data_dir=tmp_path / "data", news_db_path=tmp_path / "none.db")
    store = Store(s.db_path)
    pid = store.add_post(kind="occasion", status="scheduled", text="x", format="text", tone="celebratory",
                         scheduled_at=iso(now_utc() - timedelta(minutes=5)))
    cid = store.add_post(kind="condolence", status="scheduled", text="y", format="text", tone="solemn",
                         scheduled_at=iso(now_utc() + timedelta(hours=5)))
    store.set_state("mourning", {"until": (now_utc() + timedelta(days=3)).date().isoformat(), "reason": "test"})
    asyncio.run(publisher.publish_due(store, s))
    assert store.get_post(pid)["status"] == "cancelled"
    assert store.get_post(cid)["status"] == "scheduled"
