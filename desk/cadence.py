"""When to post: the human pattern.

A person running a company page doesn't post on a timer. They post a few
times a week, mostly on working days (Mon-Fri in the UAE), in the morning
or around lunch, at whatever minute they happen to hit "Post". Some weeks
are quiet. Big national days get their post in the morning; sad news gets a
prompt, short message. This module turns those habits into numbers.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import Settings

# (start, end, weight) in local time. Weekday windows for regular posts.
WEEKDAY_WINDOWS = [
    (time(8, 20), time(10, 15), 0.48),
    (time(12, 10), time(13, 50), 0.30),
    (time(16, 35), time(18, 20), 0.22),
]
OCCASION_WINDOWS = {
    "morning": (time(8, 35), time(10, 25)),
    "midday": (time(11, 50), time(13, 30)),
    "evening": (time(18, 10), time(20, 15)),
    "eve": (time(19, 0), time(21, 0)),   # the evening before the occasion
}
# Urgent (sad news, big breaking news) goes out promptly, but not at 2am.
URGENT_EARLIEST = time(7, 0)
URGENT_LATEST = time(22, 30)
WORKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri (UAE weekend is Sat-Sun)


def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def human_minute(dt: datetime, rng: random.Random) -> datetime:
    """Nobody posts at exactly :00/:15/:30/:45. Nudge off round minutes and
    add seconds."""
    m = dt.minute
    if m % 15 == 0 or m % 10 == 0:
        m = min(59, max(0, m + rng.choice([-4, -3, -2, 2, 3, 4, 7])))
    return dt.replace(minute=m, second=rng.randint(3, 57), microsecond=0)


def _pick_in(day: date, start: time, end: time, tz, rng: random.Random) -> datetime:
    a = datetime.combine(day, start, tz)
    b = datetime.combine(day, end, tz)
    # Triangular: people cluster in the middle of their usual window.
    secs = rng.triangular(0, (b - a).total_seconds(), (b - a).total_seconds() * rng.uniform(0.3, 0.6))
    return human_minute(a + timedelta(seconds=secs), rng)


def weekly_budget(s: Settings, d: date) -> int:
    """Regular (non-occasion) posts for d's ISO week. Seeded by the week, so
    every tick in the same week agrees."""
    iso_year, week, _ = d.isocalendar()
    r = _rng(s.cadence_salt, "week", iso_year, week)
    n = r.randint(s.posts_per_week_min, s.posts_per_week_max)
    if r.random() < s.quiet_week_chance:
        n = max(1, s.posts_per_week_min - 1)
    return n


@dataclass
class CadenceView:
    now_local: datetime
    weekly_budget: int
    regular_this_week: int
    days_since_last: float | None
    last_post_at: datetime | None
    next_regular_slot: datetime | None
    want_regular: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "now_local": self.now_local.isoformat(timespec="minutes"),
            "weekday": self.now_local.strftime("%A"),
            "weekly_budget": self.weekly_budget,
            "regular_posts_this_week": self.regular_this_week,
            "days_since_last_post": None if self.days_since_last is None else round(self.days_since_last, 1),
            "next_regular_slot": self.next_regular_slot.isoformat(timespec="minutes") if self.next_regular_slot else None,
            "regular_post_wanted": self.want_regular,
            "why": self.reason,
        }


REGULAR_KINDS = {"news", "insight", "celebration", "event"}


def _rest_days(s: Settings, d: date, budget: int) -> float:
    """How long to leave a post alone before looking for the next one: about
    half the even spacing for this week's budget, varied day to day."""
    even = 5.0 / max(budget, 1)   # five working days
    return round(even * _rng(s.cadence_salt, "rest", d).uniform(0.35, 0.7), 2)


def _local(s: str, tz) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz)


def view(s: Settings, now_utc: datetime, published: list[dict], pending: list[dict]) -> CadenceView:
    """Is a regular post wanted now, and what is the earliest natural slot?"""
    tz = s.tz
    now = now_utc.astimezone(tz)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    times = [_local(p["published_at"], tz) for p in published if p.get("published_at")]
    last = max(times) if times else None
    regular_week = special_week = 0
    for p in [*published, *pending]:
        when = p.get("published_at") or p.get("scheduled_at")
        if not when or not (week_start <= _local(when, tz) < week_start + timedelta(days=7)):
            continue
        if p["kind"] in REGULAR_KINDS:
            regular_week += 1
        else:
            special_week += 1
    # Occasion and condolence posts take room from the week's regular posts:
    # a National Day week does not also get three news posts.
    budget = max(0, weekly_budget(s, now.date()) - special_week)
    days_since = (now - last).total_seconds() / 86400 if last else None
    pending_regular = [p for p in pending if p["kind"] in REGULAR_KINDS]
    slot = next_regular_slot(s, now, [*times, *[_local(p["scheduled_at"], tz) for p in pending if p.get("scheduled_at")]])
    if pending_regular:
        want, why = False, "a regular post is already queued"
    elif regular_week >= budget:
        want, why = False, f"this week's budget of {budget} regular posts is used"
    elif days_since is not None and days_since * 24 < s.min_gap_hours:
        want, why = False, "posted too recently"
    elif days_since is not None and budget and days_since < _rest_days(s, now.date(), budget):
        want, why = False, f"letting the last post breathe ({days_since:.1f} days ago)"
    else:
        want, why = True, f"{budget - regular_week} regular post(s) left this week"
    return CadenceView(now, budget, regular_week, days_since, last, slot, want, why)


def _too_close(dt: datetime, others: list[datetime], gap_h: float) -> bool:
    return any(abs((dt - o).total_seconds()) < gap_h * 3600 for o in others)


def next_regular_slot(s: Settings, now: datetime, taken: list[datetime], *, seed: object = "") -> datetime:
    """The next natural slot for a regular post: a workday, inside a usual
    window, not too close to another post, never on the same calendar day as
    another post."""
    tz = s.tz
    rng = _rng(s.cadence_salt, "slot", now.date(), seed)
    taken_days = {t.date() for t in taken}
    last_taken = max((t for t in taken if t <= now + timedelta(days=60)), default=None)
    for offset in range(0, 60):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in WORKDAYS or day in taken_days:
            continue
        # People rarely post on back-to-back days: the day after a post is
        # usually skipped, and any open day is skipped now and then.
        if last_taken and offset < 45 and (day - last_taken.date()).days == 1 and rng.random() < 0.8:
            continue
        if offset < 45 and rng.random() < 0.2:
            continue
        windows = WEEKDAY_WINDOWS[:]
        rng.shuffle(windows)
        windows.sort(key=lambda w: -w[2] * rng.random())
        for start, end, _ in windows:
            dt = _pick_in(day, start, end, tz, rng)
            if dt <= now + timedelta(minutes=s.hold_minutes):
                continue
            if _too_close(dt, taken, s.min_gap_hours):
                continue
            return dt
    day = now.date() + timedelta(days=1)
    while day.weekday() not in WORKDAYS:
        day += timedelta(days=1)
    return _pick_in(day, time(9, 0), time(10, 0), tz, rng)


def occasion_slot(s: Settings, day: date, post_time: str, key: str) -> datetime:
    rng = _rng(s.cadence_salt, "occasion", key)
    start, end = OCCASION_WINDOWS.get(post_time, OCCASION_WINDOWS["morning"])
    target = day - timedelta(days=1) if post_time == "eve" else day
    return _pick_in(target, start, end, s.tz, rng)


def urgent_slot(s: Settings, now_utc: datetime, seed: object = "") -> datetime:
    """A person who sees sad or big news posts within the hour, not instantly,
    and waits for morning if it broke late at night."""
    tz = s.tz
    now = now_utc.astimezone(tz)
    rng = _rng(s.cadence_salt, "urgent", seed, now.isoformat(timespec="minutes"))
    dt = now + timedelta(minutes=s.urgent_hold_minutes + rng.randint(4, 28))
    if URGENT_EARLIEST <= dt.time() <= URGENT_LATEST:
        return human_minute(dt, rng)
    day = now.date() if dt.time() < URGENT_EARLIEST and dt.date() == now.date() else now.date() + timedelta(days=1)
    return _pick_in(day, time(7, 20), time(8, 25), tz, rng)


def fits_gap(s: Settings, dt: datetime, others: list[datetime], urgent: bool) -> bool:
    return not _too_close(dt, others, s.urgent_min_gap_hours if urgent else s.min_gap_hours)
