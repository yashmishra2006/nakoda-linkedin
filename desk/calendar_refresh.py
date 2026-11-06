"""`desk calendar refresh`: confirm moon-sighting dates and discover upcoming
UAE events with Claude + web search."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import date, timedelta

from . import agent, occasions
from .config import Settings
from .store import Store, iso, now_utc

log = logging.getLogger("desk")
_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def needs_refresh(store: Store, s: Settings, today: date) -> bool:
    last = store.get_state("last_calendar_refresh")
    last_dt = date.fromisoformat(last[:10]) if last else None
    soon = occasions.unconfirmed_soon(store, s.calendar_file, today, days=4)
    if soon:
        # Around moon-sighting time, check every 6 hours.
        stamp = store.get_state("last_calendar_refresh_at")
        return stamp is None or (now_utc().timestamp() - stamp) > 6 * 3600
    return last_dt is None or (today - last_dt).days >= 7


async def refresh(store: Store, s: Settings) -> dict:
    today = now_utc().astimezone(s.tz).date()
    work = s.work_dir / f"calendar-{today.isoformat()}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    to_confirm = [o.as_dict() | {"expected_date": o.date.isoformat()}
                  for o in occasions.upcoming(store, s.calendar_file, today, days=35)
                  if o.needs_confirmation and not o.confirmed]
    known = [o.name for o in occasions.upcoming(store, s.calendar_file, today, days=90)]
    (work / "to_confirm.json").write_text(json.dumps(to_confirm, ensure_ascii=False, indent=1))
    (work / "known.json").write_text(json.dumps(known, ensure_ascii=False, indent=1))
    out, stats = await agent.refresh_calendar(s, work, today.isoformat())
    store.set_state("last_calendar_refresh", today.isoformat())
    store.set_state("last_calendar_refresh_at", now_utc().timestamp())
    report: dict = {"stats": stats, "confirmed": [], "discovered": [], "ignored": []}
    if not out:
        return report
    expected = {o["occasion_key"]: date.fromisoformat(o["expected_date"]) for o in to_confirm}
    for c in out.get("confirmed", []):
        k = c["occasion_key"]
        try:
            d = date.fromisoformat(c["date"])
        except ValueError:
            report["ignored"].append(c)
            continue
        if k not in expected or abs((d - expected[k]).days) > 3 or not c["source_url"].startswith("http"):
            report["ignored"].append(c)
            continue
        base, year = k.rsplit("-", 1)
        with store.tx() as db:
            db.execute("INSERT OR REPLACE INTO calendar_confirmed VALUES (?,?,?,?,?,?,?,?)",
                       (base, int(year), d.isoformat(), c.get("end_date", ""), "", c["source_url"],
                        c.get("note", ""), iso(now_utc())))
        report["confirmed"].append(c)
        log.info("confirmed %s on %s (%s)", k, d, c["source_url"])
    for e in out.get("discovered", [])[:6]:
        try:
            d = date.fromisoformat(e["date"])
        except ValueError:
            report["ignored"].append(e)
            continue
        if not _KEY.match(e["key"]) or not (today <= d <= today + timedelta(days=90)):
            report["ignored"].append(e)
            continue
        with store.tx() as db:
            db.execute("INSERT OR IGNORE INTO calendar_discovered VALUES (?,?,?,?,?,?,?,?)",
                       (e["key"], e["name"], d.isoformat(), e.get("end_date", ""), e["category"], e["why"],
                        e["source_url"], iso(now_utc())))
        report["discovered"].append(e)
    return report
