"""Strict JSON contracts for the three Claude sessions (planner, critic,
calendar refresh). Passed to the Agent SDK as output_format=json_schema."""

from __future__ import annotations

KINDS = ["news", "occasion", "condolence", "celebration", "insight", "event"]
TONES = ["celebratory", "warm", "reflective", "solemn", "analytical", "curious", "plain"]
FORMATS = ["text", "image", "multi_image", "article"]
OPENINGS = ["scene", "fact", "number", "question", "quote", "direct", "personal", "contrast", "greeting", "news-lede"]
SLOTS = ["urgent", "next_natural", "occasion_day"]

_IMAGE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "images/<file>.jpg as returned by fetch_image / fetch_link_preview"},
        "alt_text": {"type": "string", "description": "What the picture shows, for screen readers. One sentence."},
    },
    "required": ["path", "alt_text"],
    "additionalProperties": False,
}

_FACT = {
    "type": "object",
    "properties": {"claim": {"type": "string"}, "source": {"type": "string", "description": "URL or story file"}},
    "required": ["claim", "source"],
    "additionalProperties": False,
}

_POST = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "occasion_key": {"type": "string", "description": "occasion_key from occasions.json, or ''"},
        "story_id": {"type": "string", "description": "story_id from signals.json, or ''"},
        "slot": {"type": "string", "enum": SLOTS,
                 "description": "urgent: within the hour (sad news, major breaking news only); "
                                "occasion_day: on the occasion's date at its post_time; next_natural: the next "
                                "normal slot the scheduler picks"},
        "tone": {"type": "string", "enum": TONES},
        "format": {"type": "string", "enum": FORMATS},
        "opening_style": {"type": "string", "enum": OPENINGS},
        "text": {"type": "string", "description": "The post exactly as it should appear. Plain text, line breaks "
                                                  "allowed, no hashtags, no markdown."},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "0-3, without '#'"},
        "images": {"type": "array", "items": _IMAGE, "description": "1 for image, 2-6 for multi_image, "
                                                                   "1 thumbnail (optional) for article, 0 for text"},
        "link_url": {"type": "string", "description": "article format only, else ''"},
        "link_title": {"type": "string"},
        "facts": {"type": "array", "items": _FACT, "description": "every factual claim in the post and where it is from"},
        "why_post": {"type": "string", "description": "one sentence: why this is worth Nakoda's page"},
    },
    "required": ["kind", "occasion_key", "story_id", "slot", "tone", "format", "opening_style", "text", "hashtags",
                 "images", "link_url", "link_title", "facts", "why_post"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "mourning": {
            "type": "object",
            "description": "Set active=true only when the UAE (or Dubai) has officially declared a mourning period.",
            "properties": {
                "active": {"type": "boolean"},
                "until": {"type": "string", "description": "YYYY-MM-DD last day of mourning, or ''"},
                "reason": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["active", "until", "reason", "source"],
            "additionalProperties": False,
        },
        "posts": {"type": "array", "items": _POST, "description": "0-3 posts. Zero is a normal answer."},
        "passed_on": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"story_id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["story_id", "reason"],
                "additionalProperties": False,
            },
            "description": "signals you looked at and decided not to post about",
        },
        "note": {"type": "string", "description": "one or two sentences on today's decision"},
    },
    "required": ["mourning", "posts", "passed_on", "note"],
    "additionalProperties": False,
}

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "scores": {
            "type": "object",
            "properties": {
                "human": {"type": "integer", "description": "1-10: would a reader believe a thoughtful person wrote it"},
                "accuracy": {"type": "integer", "description": "1-10: every claim checked against a source"},
                "tone": {"type": "integer", "description": "1-10: right for the moment and for the UAE"},
                "worth": {"type": "integer", "description": "1-10: worth a follower's attention"},
                "image": {"type": "integer", "description": "1-10 (10 if text-only is the right call)"},
            },
            "required": ["human", "accuracy", "tone", "worth", "image"],
            "additionalProperties": False,
        },
        "issues": {"type": "array", "items": {"type": "string"}},
        "text": {"type": "string", "description": "Final text: unchanged when approving, rewritten when revising"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "drop_images": {"type": "boolean", "description": "true if the image hurts the post; it then goes text-only"},
        "alt_texts": {"type": "array", "items": {"type": "string"}, "description": "corrected alt text per image, or []"},
    },
    "required": ["verdict", "scores", "issues", "text", "hashtags", "drop_images", "alt_texts"],
    "additionalProperties": False,
}

CALENDAR_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {
            "type": "array",
            "description": "Official dates for occasions listed in to_confirm.json",
            "items": {
                "type": "object",
                "properties": {
                    "occasion_key": {"type": "string", "description": "as given, e.g. eid-al-fitr-2027"},
                    "date": {"type": "string", "description": "YYYY-MM-DD of the day itself"},
                    "end_date": {"type": "string", "description": "last day of the holiday/event or ''"},
                    "source_url": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["occasion_key", "date", "end_date", "source_url", "note"],
                "additionalProperties": False,
            },
        },
        "discovered": {
            "type": "array",
            "description": "Notable upcoming UAE/Dubai events worth a post (AI, tech, business, government)",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "lowercase-hyphenated with year, e.g. gitex-global-2027"},
                    "name": {"type": "string"},
                    "date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "category": {"type": "string", "enum": ["event", "national", "community"]},
                    "why": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["key", "name", "date", "end_date", "category", "why", "source_url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["confirmed", "discovered"],
    "additionalProperties": False,
}
