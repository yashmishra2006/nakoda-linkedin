"""The occasion calendar: calendar.yaml + confirmed dates + discovered events.

Hijri occasions are computed with the Umm al-Qura calendar (hijridate). The
UAE announces Ramadan and the Eids after the moon sighting, so the computed
day is only "expected" until `desk calendar refresh` confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml
from hijridate import Gregorian, Hijri

from .store import Store


@dataclass
class Occasion:
    key: str                 # calendar key, e.g. eid-al-fitr
    instance: str            # key + year, e.g. eid-al-fitr-2027 (one post per instance)
    name: str
    date: date
    end_date: date | None
    importance: str          # high | medium | optional
    tone: str
    post_time: str
    category: str            # national | islamic | community | event
    needs_confirmation: bool
    confirmed: bool
    notes: str = ""
    source: str = "calendar"  # calendar | discovered
    source_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def publishable(self) -> bool:
        return self.confirmed or not self.needs_confirmation

    def as_dict(self) -> dict:
        return {
            "occasion_key": self.instance, "name": self.name, "date": self.date.isoformat(),
            "end_date": self.end_date.isoformat() if self.end_date else "",
            "weekday": self.date.strftime("%A"), "importance": self.importance, "tone": self.tone,
            "post_time": self.post_time, "category": self.category,
            "date_status": "confirmed" if self.confirmed else ("expected - awaiting official announcement"
                                                                if self.needs_confirmation else "fixed"),
            "notes": self.notes, "source": self.source, "source_url": self.source_url,
        }


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _category(key: str, rule: dict) -> str:
    if "hijri" in rule:
        return "islamic"
    if key in {"diwali", "christmas", "international-womens-day"}:
        return "community"
    return "national"


def load_calendar(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("occasions", [])


def _instances(entry: dict, start: date, end: date) -> list[tuple[date, dict]]:
    """Every date this entry falls on between start and end (inclusive)."""
    rule = entry.get("rule") or {}
    out: list[tuple[date, dict]] = []
    if "fixed" in rule:
        mm, dd = (int(x) for x in str(rule["fixed"]).split("-"))
        for y in range(start.year, end.year + 1):
            d = date(y, mm, dd)
            if start <= d <= end:
                out.append((d, {}))
    elif "hijri" in rule:
        mm, dd = (int(x) for x in str(rule["hijri"]).split("-"))
        h0 = Gregorian(start.year, start.month, start.day).to_hijri().year
        h1 = Gregorian(end.year, end.month, end.day).to_hijri().year
        for hy in range(h0, h1 + 1):
            try:
                g = Hijri(hy, mm, dd).to_gregorian()
            except (ValueError, OverflowError):
                continue
            d = date(g.year, g.month, g.day)
            if start <= d <= end:
                out.append((d, {"hijri_year": hy}))
    elif "dates" in rule:
        for y, md in (rule["dates"] or {}).items():
            mm, dd = (int(x) for x in str(md).split("-"))
            d = date(int(y), mm, dd)
            if start <= d <= end:
                out.append((d, {}))
    return out


def upcoming(store: Store, calendar_file: Path, today: date, days: int = 14, back: int = 0) -> list[Occasion]:
    """Occasions from `today - back` to `today + days`, with confirmed dates
    applied and discovered events included, in date order."""
    start, end = today - timedelta(days=back), today + timedelta(days=days)
    # Look a little wider so a confirmed date that moved a day still matches.
    wide_start, wide_end = start - timedelta(days=3), end + timedelta(days=3)
    confirmed = {(r["key"], r["year"]): dict(r) for r in store.db.execute("SELECT * FROM calendar_confirmed")}
    out: list[Occasion] = []
    for e in load_calendar(calendar_file):
        rule = e.get("rule") or {}
        needs = bool(e.get("confirm")) or "hijri" in rule
        for d, extra in _instances(e, wide_start, wide_end):
            conf = confirmed.get((e["key"], d.year))
            end_d = None
            if conf:
                d = date.fromisoformat(conf["date"])
                end_d = date.fromisoformat(conf["end_date"]) if conf.get("end_date") else None
            if not (start <= d <= end):
                continue
            notes = (e.get("notes") or "").strip()
            notes = notes.replace("{ordinal}", _ordinal(d.year - 1971))
            notes = notes.replace("{hijri_year}", str(extra.get("hijri_year", "")))
            out.append(Occasion(
                key=e["key"], instance=f"{e['key']}-{d.year}", name=e["name"], date=d, end_date=end_d,
                importance=e.get("importance", "optional"), tone=e.get("tone", "warm"),
                post_time=e.get("post_time", "morning"), category=_category(e["key"], rule),
                needs_confirmation=needs, confirmed=bool(conf), notes=notes,
                source_url=(conf or {}).get("source_url", ""), extra=extra,
            ))
    for r in store.db.execute("SELECT * FROM calendar_discovered WHERE date BETWEEN ? AND ?",
                              (start.isoformat(), end.isoformat())):
        d = date.fromisoformat(r["date"])
        out.append(Occasion(
            key=r["key"], instance=r["key"], name=r["name"], date=d,
            end_date=date.fromisoformat(r["end_date"]) if r["end_date"] else None,
            importance="optional", tone="warm", post_time="morning", category=r["category"],
            needs_confirmation=False, confirmed=True, notes=r["why"], source="discovered",
            source_url=r["source_url"],
        ))
    out.sort(key=lambda o: (o.date, o.importance != "high"))
    return out


def unconfirmed_soon(store: Store, calendar_file: Path, today: date, days: int = 4) -> list[Occasion]:
    return [o for o in upcoming(store, calendar_file, today, days) if o.needs_confirmation and not o.confirmed]
