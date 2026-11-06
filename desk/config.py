"""Settings, read from the environment (or ./.env next to the package).

Everything has a default so `python -m desk check` runs with no configuration.
Nothing is posted to LinkedIn until LINKEDIN_AUTHOR_URN and a token exist
and DESK_DRY_RUN is turned off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass(frozen=True)
class Settings:
    # ---- where things live
    data_dir: Path = field(default_factory=lambda: Path(_env("DESK_DATA_DIR", str(ROOT / "data"))))
    calendar_file: Path = field(default_factory=lambda: Path(_env("DESK_CALENDAR_FILE", str(ROOT / "calendar.yaml"))))
    voice_dir: Path = field(default_factory=lambda: Path(_env("DESK_VOICE_DIR", str(ROOT / "voice"))))
    brand_dir: Path = field(default_factory=lambda: Path(_env("DESK_BRAND_DIR", str(ROOT / "brand"))))
    timezone: str = _env("DESK_TIMEZONE", "Asia/Dubai")

    # ---- safety switches
    # On by default: drafts are written to data/previews and nothing is posted.
    dry_run: bool = _env("DESK_DRY_RUN", True)
    # auto:    validated posts publish at their slot (after hold_minutes).
    # approve: every post waits for `python -m desk approve <id>`.
    review_mode: str = _env("DESK_REVIEW_MODE", "auto")
    # Every post sits in the queue at least this long before it goes out, so a
    # person who gets the webhook preview can `desk cancel` it.
    hold_minutes: int = _env("DESK_HOLD_MINUTES", 45)
    urgent_hold_minutes: int = _env("DESK_URGENT_HOLD_MINUTES", 12)
    notify_webhook_url: str = _env("DESK_NOTIFY_WEBHOOK_URL", "")

    # ---- cadence (the "human pattern")
    # Regular posts per ISO week; the week's number is drawn in this range
    # (seeded per week), and some weeks are deliberately quiet.
    posts_per_week_min: int = _env("DESK_POSTS_PER_WEEK_MIN", 2)
    posts_per_week_max: int = _env("DESK_POSTS_PER_WEEK_MAX", 4)
    quiet_week_chance: float = _env("DESK_QUIET_WEEK_CHANCE", 0.15)
    min_gap_hours: float = _env("DESK_MIN_GAP_HOURS", 20.0)
    urgent_min_gap_hours: float = _env("DESK_URGENT_MIN_GAP_HOURS", 3.0)
    plan_interval_hours: float = _env("DESK_PLAN_INTERVAL_HOURS", 6.0)
    occasion_lookahead_days: int = _env("DESK_OCCASION_LOOKAHEAD_DAYS", 3)
    cadence_salt: str = _env("DESK_CADENCE_SALT", "nakoda")

    # ---- signals (read-only access to the Nakoda News collector)
    news_db_path: Path = field(default_factory=lambda: Path(_env(
        "DESK_NEWS_DB", "/var/lib/nakoda-news-collector/news.db")))
    signal_hours: int = _env("DESK_SIGNAL_HOURS", 36)
    max_signals: int = _env("DESK_MAX_SIGNALS", 25)
    # Nakoda News backend (optional): our own published coverage to link to.
    news_api_base: str = _env("NEWSROOM_API_BASE", "")
    news_api_token: str = _env("NEWSROOM_TOKEN", "")
    news_site_url: str = _env("NEWS_SITE_URL", "https://news.nakoda.ai")

    # ---- Claude (Agent SDK)
    model: str = _env("DESK_MODEL", "claude-opus-5-5")
    effort: str = _env("DESK_EFFORT", "high")
    critic_model: str = _env("DESK_CRITIC_MODEL", "claude-opus-5-5")
    max_turns: int = _env("DESK_MAX_TURNS", 60)
    max_budget_usd: float = _env("DESK_MAX_BUDGET_USD", 4.0)
    critic_budget_usd: float = _env("DESK_CRITIC_BUDGET_USD", 1.5)

    # ---- images
    pexels_api_key: str = _env("PEXELS_API_KEY", "")
    unsplash_access_key: str = _env("UNSPLASH_ACCESS_KEY", "")
    # Long edge must be at least this many px, short edge at least min_short.
    min_image_long: int = _env("DESK_MIN_IMAGE_LONG", 1200)
    min_image_short: int = _env("DESK_MIN_IMAGE_SHORT", 800)
    user_agent: str = _env(
        "DESK_USER_AGENT", "Mozilla/5.0 (compatible; NakodaSocialDesk/1.0; +https://nakoda.ai)")

    # ---- LinkedIn
    linkedin_client_id: str = _env("LINKEDIN_CLIENT_ID", "")
    linkedin_client_secret: str = _env("LINKEDIN_CLIENT_SECRET", "")
    linkedin_redirect_uri: str = _env("LINKEDIN_REDIRECT_URI", "http://localhost:8765/callback")
    # urn:li:organization:<id> for the company page (needs w_organization_social),
    # or urn:li:person:<id> for a personal profile (w_member_social).
    linkedin_author_urn: str = _env("LINKEDIN_AUTHOR_URN", "")
    linkedin_scopes: str = _env("LINKEDIN_SCOPES", "openid profile w_member_social w_organization_social")
    # LinkedIn's versioned REST API; versions are supported for about a year.
    linkedin_api_version: str = _env("LINKEDIN_API_VERSION", "202610")
    # Optional static token (otherwise data/linkedin_token.json from `desk auth`).
    linkedin_access_token: str = _env("LINKEDIN_ACCESS_TOKEN", "")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "desk.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "linkedin_token.json"


settings = Settings()

# Sources whose photos are official handouts released for media use. Photos
# from these may be used on LinkedIn; photos by news outlets' own
# photographers may not (they are licensed to the outlet, not to us).
PRESS_SOURCES = {
    "wam-en", "wam-ar", "abudhabi-media-office", "dubai-media-office",
    "openai", "anthropic", "google-deepmind", "google-ai", "google-research", "mistral",
    "microsoft", "nvidia", "meta-newsroom", "apple-newsroom", "apple-ml", "hugging-face",
}
