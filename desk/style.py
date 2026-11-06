"""Hard checks a draft must pass before it can be queued.

The critic agent judges taste; this module catches the mechanical tells of
machine-written posts, repetition against what we have already posted, and
tone mistakes (emoji or hashtags on a condolence). Anything here is a hard
reject that goes back to the critic once for a rewrite.
"""

from __future__ import annotations

import re
import unicodedata

# Phrases that make a post read as AI-written or as corporate filler.
BANNED = [
    r"\bdelve", r"in today'?s (fast[- ]paced|digital|ever)", r"fast[- ]paced world", r"ever[- ]evolving",
    r"game[- ]?changer", r"revolutioni[sz]", r"unlock(ing)? the (power|potential)", r"\bunleash",
    r"harness(ing)? the power", r"(excited|thrilled|delighted|proud) to (announce|share)", r"\btapestry",
    r"testament to", r"\bnavigat(e|ing) the", r"\bseamless(ly)?\b", r"cutting[- ]edge", r"\bsynerg",
    r"\belevate\b", r"\bembark", r"\bparadigm", r"let that sink in", r"here'?s the thing", r"buckle up",
    r"\ba beacon\b", r"stands as a", r"\bunderscore", r"\bpivotal\b", r"\bfoster(s|ing)?\b", r"\brealm\b",
    r"\blandscape\b", r"\bin conclusion\b", r"\bfurthermore\b", r"\bmoreover\b", r"\bnotably\b",
    r"\bit'?s worth noting\b", r"\bat the end of the day\b", r"\bthe future is (here|now)\b",
    r"\bnot just\b[^.!?\n]{1,60}\bbut\b", r"\bit'?s not about\b[^.!?\n]{1,60}\bit'?s about\b",
    r"\bin a world where\b", r"\bjourney\b", r"\bcurated\b", r"\brobust\b", r"\bleverag",
    r"\bwhat do you think\?\s*$", r"\bthoughts\?\s*$", r"\bagree\?\s*$", r"\b(comment|drop) below\b",
    r"\bkey takeaways?\b", r"\bhere are \d+ (things|lessons|ways)\b", r"\bdive (in|into|deep)\b",
    r"\btransformative\b", r"\bgroundbreaking\b", r"\bempower(s|ing)?\b", r"\bholistic\b",
]
BANNED_RE = [re.compile(p, re.I | re.M) for p in BANNED]
LOUD_EMOJI = set("🚀✨🔥💡👇🎯📈💯🙌⚡🤯👀🧵")
MARKDOWN = re.compile(r"(\*\*|__|^#{1,6}\s|^\s*[-*•]\s.+\n\s*[-*•]\s)", re.M)
ARROW_LIST = re.compile(r"^\s*(→|➡️|✅|👉|▶)", re.M)


def is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x1F1E6 <= cp <= 0x1F1FF)


def count_emoji(text: str) -> int:
    # Flags are two regional indicators; count them as one.
    n = sum(1 for ch in text if is_emoji(ch) and not (0x1F1E6 <= ord(ch) <= 0x1F1FF))
    n += sum(1 for ch in text if 0x1F1E6 <= ord(ch) <= 0x1F1FF) // 2
    return n


def words(text: str) -> list[str]:
    return re.findall(r"[\w'’]+", text.lower())


def shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    w = words(text)
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def opening(text: str, n: int = 4) -> str:
    return " ".join(words(text)[:n])


def length_band(text: str) -> str:
    n = len(words(text))
    return "short" if n < 70 else ("medium" if n < 170 else "long")


LIMITS = {
    # kind: (min words, max words, max emoji, max hashtags)
    "condolence": (15, 120, 0, 0),
    "occasion": (20, 220, 2, 3),
    "celebration": (25, 260, 2, 3),
    "news": (50, 320, 1, 3),
    "insight": (60, 380, 1, 3),
    "event": (30, 280, 1, 3),
}


def check(text: str, hashtags: list[str], kind: str, tone: str, recent: list[dict]) -> list[str]:
    """Return a list of problems; empty means the draft passes."""
    problems: list[str] = []
    t = unicodedata.normalize("NFC", text).strip()
    lo, hi, max_emoji, max_tags = LIMITS.get(kind, LIMITS["news"])
    if tone == "solemn":
        max_emoji, max_tags = 0, min(max_tags, 1)
    n = len(words(t))
    if n < lo:
        problems.append(f"too short for a {kind} post ({n} words, min {lo})")
    if n > hi:
        problems.append(f"too long for a {kind} post ({n} words, max {hi})")
    if len(t) + sum(len(h) + 2 for h in hashtags) > 2900:
        problems.append("over LinkedIn's 3000-character limit")
    for rx in BANNED_RE:
        m = rx.search(t)
        if m:
            problems.append(f"cliché / machine-sounding phrase: '{m.group(0).strip()}'")
    if MARKDOWN.search(t):
        problems.append("markdown (bold, headings or bullet lists) - LinkedIn shows it as raw symbols")
    if len(ARROW_LIST.findall(t)) >= 2:
        problems.append("arrow/checkmark list formatting reads as a template")
    if "#" in t:
        problems.append("hashtags belong in the hashtags field, not in the text")
    emo = count_emoji(t)
    if emo > max_emoji:
        problems.append(f"{emo} emoji (max {max_emoji} for this post)")
    if any(ch in LOUD_EMOJI for ch in t):
        problems.append("hype emoji (🚀✨🔥💡👇 and friends)")
    if len(hashtags) > max_tags:
        problems.append(f"{len(hashtags)} hashtags (max {max_tags} here)")
    for h in hashtags:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,40}", h):
            problems.append(f"hashtag '{h}' must be letters/digits only, without '#'")
    if t.count("—") > 2:
        problems.append("more than two em dashes")
    if t.count("!") > (2 if tone == "celebratory" else 1):
        problems.append("too many exclamation marks")
    paras = [p for p in re.split(r"\n\s*\n", t) if p.strip()]
    if len(paras) >= 6 and sum(1 for p in paras if len(re.findall(r"[.!?]", p)) <= 1) / len(paras) > 0.8:
        problems.append("one-sentence-per-line 'broetry' layout")
    # Repetition against our own recent posts.
    op = opening(t)
    for p in recent[:15]:
        if op and opening(p["text"]) == op:
            problems.append(f"opens with the same words as post #{p['id']} ('{op}')")
            break
    for p in recent[:40]:
        sim = similarity(t, p["text"])
        if sim > 0.3:
            problems.append(f"too similar to post #{p['id']} ({sim:.2f} overlap)")
            break
    return problems
