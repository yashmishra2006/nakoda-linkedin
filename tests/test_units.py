import io
from dataclasses import replace
from datetime import date, datetime, timezone

from PIL import Image

from desk import cadence, linkedin, media, occasions, style
from desk.config import settings
from desk.store import Store


# ---------------------------------------------------------------- style


def test_clean_post_passes():
    text = ("Dubai's new rules on AI in government services take effect on 1 January, and the part most "
            "companies will feel first isn't the headline one.\n\nEvery entity using AI to support a decision "
            "about a person will need to explain that decision in plain language, the notice says. That's easy "
            "to agree with and hard to do, because most deployed models were never built to explain themselves.")
    assert style.check(text, ["UAE", "AI"], "news", "analytical", []) == []


def test_ai_tells_are_caught():
    text = ("🚀 In today's fast-paced world, the UAE is a beacon of innovation. This is not just about "
            "technology, but about people. We are excited to announce our journey. Thoughts?")
    problems = " | ".join(style.check(text, [], "news", "plain", []))
    for needle in ("fast-paced", "beacon", "not just", "excited to announce", "journey", "hype emoji", "Thoughts?"):
        assert needle.lower() in problems.lower(), needle


def test_condolence_rules():
    text = ("Nakoda AI extends its sincere condolences to the family and to the people of the UAE. "
            "May Allah have mercy on him. 🙏")
    problems = style.check(text, ["UAE"], "condolence", "solemn", [])
    assert any("emoji" in p for p in problems)
    assert any("hashtags" in p for p in problems)


def test_repetition_against_history():
    old = {"id": 7, "text": "Fifty-five years ago this week six emirates signed up to a shared future and the rest is history."}
    new = "Fifty-five years ago this week six emirates signed up to a shared future, and look where we are now with all of it."
    problems = style.check(new, [], "occasion", "warm", [old])
    assert any("#7" in p for p in problems)


def test_hashtags_in_text_and_markdown():
    problems = style.check("**Big news** for #AI in the UAE today, with plenty of detail to make the minimum "
                           "word count for a news post comfortably, because that is how it works here.",
                           [], "news", "plain", [])
    assert any("markdown" in p for p in problems)
    assert any("hashtags belong" in p for p in problems)


# ---------------------------------------------------------------- linkedin text


def test_little_text_escaping():
    assert linkedin.escape_little_text("Q3 (est.) [draft] @team #1 a_b") == "Q3 \\(est.\\) \\[draft\\] \\@team \\#1 a\\_b"


def test_commentary_hashtags():
    c = linkedin.commentary("Eid Mubarak.", ["EidAlFitr", "UAE"])
    assert c == "Eid Mubarak.\n\n{hashtag|\\#|EidAlFitr} {hashtag|\\#|UAE}"


# ---------------------------------------------------------------- cadence


def test_regular_slots_look_human():
    s = settings
    now = datetime(2026, 11, 6, 6, 0, tzinfo=s.tz)  # a Friday morning
    taken: list[datetime] = []
    for i in range(12):
        slot = cadence.next_regular_slot(s, now, taken, seed=i)
        assert slot.weekday() in cadence.WORKDAYS
        assert slot.minute % 15 != 0 or slot.minute % 10 != 0 or slot.second > 0
        assert any(a <= slot.time() <= b for a, b, _ in cadence.WEEKDAY_WINDOWS)
        assert all(abs((slot - t).total_seconds()) >= s.min_gap_hours * 3600 for t in taken)
        assert slot.date() not in {t.date() for t in taken}
        taken.append(slot)


def test_weekly_budget_varies_within_range():
    s = settings
    budgets = {cadence.weekly_budget(s, date(2026, 1, 5) + __import__("datetime").timedelta(weeks=w)) for w in range(40)}
    assert min(budgets) >= 1 and max(budgets) <= s.posts_per_week_max
    assert len(budgets) > 1  # it really varies


def test_urgent_at_night_waits_for_morning():
    s = settings
    late = datetime(2026, 11, 6, 23, 40, tzinfo=s.tz).astimezone(timezone.utc)
    slot = cadence.urgent_slot(s, late, "x").astimezone(s.tz)
    assert slot.date() == date(2026, 11, 7) and 7 <= slot.hour <= 8
    day = datetime(2026, 11, 6, 15, 0, tzinfo=s.tz).astimezone(timezone.utc)
    slot = cadence.urgent_slot(s, day, "y").astimezone(s.tz)
    assert slot.date() == date(2026, 11, 6) and 15 <= slot.hour <= 16


def test_occasion_slot_morning_on_the_day():
    slot = cadence.occasion_slot(settings, date(2026, 12, 2), "morning", "uae-national-day-2026")
    assert slot.date() == date(2026, 12, 2) and 8 <= slot.hour <= 10


# ---------------------------------------------------------------- occasions


def test_calendar_national_and_hijri(tmp_path):
    store = Store(tmp_path / "d.db")
    occ = occasions.upcoming(store, settings.calendar_file, date(2026, 11, 20), days=20)
    nd = next(o for o in occ if o.key == "uae-national-day")
    assert nd.date == date(2026, 12, 2) and nd.publishable and "55th" in nd.notes
    cd = next(o for o in occ if o.key == "commemoration-day")
    assert cd.tone == "solemn"
    occ = occasions.upcoming(store, settings.calendar_file, date(2027, 3, 1), days=20)
    eid = next(o for o in occ if o.key == "eid-al-fitr")
    assert eid.date == date(2027, 3, 9) and eid.needs_confirmation and not eid.publishable
    # Official announcement moves it a day: the confirmed date wins.
    with store.tx() as db:
        db.execute("INSERT INTO calendar_confirmed VALUES ('eid-al-fitr', 2027, '2027-03-10', '', '', 'https://wam.ae/x', '', 'now')")
    eid = next(o for o in occasions.upcoming(store, settings.calendar_file, date(2027, 3, 1), days=20) if o.key == "eid-al-fitr")
    assert eid.date == date(2027, 3, 10) and eid.publishable


# ---------------------------------------------------------------- media


def _jpeg(w, h):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (120, 80, 40)).save(b, "JPEG")
    return b.getvalue()


def test_normalise_crops_and_rejects():
    data, meta = media.normalise(_jpeg(4000, 1500), 1200, 800)
    assert data is not None and abs(meta["width"] / meta["height"] - 1.91) < 0.02 and meta["cropped"]
    data, meta = media.normalise(_jpeg(800, 600), 1200, 800)
    assert data is None and "too small" in meta


def test_press_policy(tmp_path):
    sess = media.MediaSession(replace(settings, data_dir=tmp_path), tmp_path, press_urls={"https://cdn.example/x.jpg"})
    assert media._ok_press("https://cdn.example/x.jpg", sess)
    assert media._ok_press("https://www.wam.ae/images/a.jpg", sess)
    assert not media._ok_press("https://images.gulfnews.com/photo.jpg", sess)
