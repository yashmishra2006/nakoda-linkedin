# Nakoda LinkedIn desk

Runs Nakoda AI's LinkedIn page the way a careful person would. It posts a few times a week, not
every day. It marks UAE national and religious occasions on the right morning, responds promptly
and briefly to sad news, shares happy UAE news, and adds a real perspective on the AI and business
news that matters here. Every post is written and fact-checked by Claude through the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview), reviewed by a second,
independent Claude session, run through hard style checks, and only then queued.

It follows the same pattern as the Nakoda News newsroom (`nakoda-news/collector/newsroom`) and
reads that collector's database, so it sees everything WAM, the media offices, Gulf News, Khaleej
Times, The National and the AI labs publish, hour by hour.

```
            every 15 min (systemd)                         python -m desk tick
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ 1. publish what is due  ── mourning? date confirmed? spaced out? ─▶ LinkedIn │
 │ 2. calendar refresh     ── Claude + web search confirms Eid/Ramadan dates,   │
 │                            finds upcoming UAE events (weekly; 6-hourly near  │
 │                            a moon sighting)                                  │
 │ 3. plan, only if there is a reason:                                          │
 │      urgent news (a passing, national mourning, big breaking news)           │
 │      an occasion within 3 days with no post yet                              │
 │      a regular post is wanted this week, new news exists, last look >6h ago  │
 │                                                                              │
 │    planner (Claude) ── reads brief, signals, occasions, our history, voice   │
 │       │                guide; researches primary sources; searches, fetches  │
 │       │                and LOOKS AT images; writes 0-3 posts                 │
 │       ▼                                                                      │
 │    structural checks ─▶ critic (fresh Claude) ─▶ style checks ─▶ (one more   │
 │                         facts/tone/human/image   clichés, emoji,   critic    │
 │                         approve/revise/reject    repetition…       round)    │
 │       ▼                                                                      │
 │    scheduled at a human time  ─▶ webhook preview (cancel window)             │
 └──────────────────────────────────────────────────────────────────────────────┘
```

## What makes it read as human

**Rhythm** (`desk/cadence.py`)
- 2-4 regular posts a week, drawn fresh each week, with about one week in seven deliberately
  quiet. Occasion and condolence posts take room from that week's budget.
- Working days only (Mon-Fri in the UAE) for regular posts, in the windows people actually post:
  morning, lunch, late afternoon. Never at :00/:15/:30, never the same time twice.
- Rarely two days in a row. After each post it waits a while before looking again. At most one
  regular post a day.
- Urgent posts (sad news) go out within the hour, not instantly, and never between 22:30 and 07:00.
- Occasions go out on the morning of the day. Eid and Ramadan posts wait for the official
  moon-sighting announcement and move if the date moves.
- The planner can and often will decide to post nothing.

**Writing** (`voice/VOICE.md`, `voice/EXAMPLES.md`, `desk/style.py`)
- A voice guide written for this page, with separate rules for national days, religious
  occasions, solemn days, condolences, happy news, news with a perspective, and events. Both
  Claude sessions read it every time. Edit it to change how the page sounds.
- The planner sees the last 30 posts (opening style, length, format, tone, first line) and must
  vary against them. The code also rejects drafts that open like a recent post or overlap one.
- Hard rejects for the tells of generated text: "delve", "landscape", "game-changer",
  "it's not just X, it's Y", engagement-bait endings, rocket/sparkle emoji, hashtag walls,
  markdown, arrow lists, one-line-per-paragraph "broetry", too many em dashes or exclamation marks.
- An independent critic scores human-ness, accuracy, tone, worth and image. It rewrites or
  rejects anything below 7/10, and checks names, titles and numbers against the sources.

**Sensitivity**
- Condolences: 15-120 words, no emoji, no hashtags, no link, nothing about Nakoda, names and
  titles exactly as WAM gives them.
- When the UAE declares official mourning (detected from the news, or set by hand), every queued
  celebratory or regular post is cancelled and nothing but the condolence goes out until it ends.
- Commemoration Day is solemn. Religious occasions are sincere and never commercial.

**Images** (`desk/media.py`)
- Claude gets in-process MCP tools: `search_photos` (Pexels/Unsplash), `fetch_image`,
  `fetch_link_preview`, `list_brand_photos`. Every image is downloaded, size-checked
  (≥1200px), cropped into LinkedIn's 4:5–1.91:1 range, sharpness-scored and saved. Claude then
  opens it and looks at it before choosing, and writes the alt text from what it sees.
- Only images we may use can be fetched: official handouts (WAM, media offices, company
  newsrooms), licensed stock, our own photos in `brand/`, and our own pages' link previews.
  Photos by news outlets' photographers are refused by the tool itself.
- Text-only is a normal outcome when no image is excellent.

## Setup

On the VPS that runs the Nakoda News collector:

```bash
git clone <this repo> /opt/nakoda-linkedin && cd /opt/nakoda-linkedin
```

```bash
sudo ./deploy/install.sh
```

The install script creates `.env` from `.env.example`. Fill it in:

| Setting | What |
|---|---|
| `ANTHROPIC_API_KEY` | Claude (same as the newsroom) |
| `DESK_NEWS_DB` | the collector's `news.db` (default `/var/lib/nakoda-news-collector/news.db`) |
| `NEWSROOM_API_BASE`, `NEWSROOM_TOKEN` | optional: lets posts link our own news.nakoda.ai stories |
| `PEXELS_API_KEY`, `UNSPLASH_ACCESS_KEY` | free stock photo keys (either or both) |
| `DESK_NOTIFY_WEBHOOK_URL` | Slack/Discord webhook: a preview of every queued post, with the cancel command |
| `LINKEDIN_*` | see below |
| `DESK_DRY_RUN` | starts `true`: posts are written to `data/previews/*.md` instead of LinkedIn |

### LinkedIn app

1. Create an app at <https://www.linkedin.com/developers/apps> and link it to the Nakoda AI
   company page (a page admin verifies it).
2. Add products:
   - **Company page:** *Community Management API* (scope `w_organization_social`; LinkedIn
     reviews the request, and may require it on an app of its own). The authorising user must
     be an admin of the page.
   - **Personal profile instead:** *Share on LinkedIn* and *Sign In with LinkedIn using OpenID
     Connect* (`openid profile w_member_social`, approved instantly). Set `LINKEDIN_SCOPES`
     to those.
3. Under Auth, add the redirect URL (`LINKEDIN_REDIRECT_URI`, e.g.
   `http://localhost:8765/callback`; nothing has to listen there, you just copy the URL).
4. Put the client ID and secret in `.env`, then:

```bash
.venv/bin/python -m desk auth url
```

Open the printed URL, approve, and copy the URL you are redirected to:

```bash
.venv/bin/python -m desk auth code 'http://localhost:8765/callback?code=...'
```

5. Set `LINKEDIN_AUTHOR_URN`: `urn:li:organization:<page id>` (the number in the page admin
   URL), or `urn:li:person:<id>` from `desk auth whoami`.

Access tokens last 60 days. The desk refreshes automatically when LinkedIn issued a refresh token;
otherwise `desk check` shows the days left and the log warns a week before expiry.

### Going live

1. Leave `DESK_DRY_RUN=true` for a week or two. Read `data/previews/`, `desk history` and `desk queue`.
2. Tune `voice/VOICE.md` until the previews sound like you.
3. Set `DESK_DRY_RUN=false`. Use `DESK_REVIEW_MODE=approve` if you want to approve every post
   by hand at first, or keep `auto` with the webhook preview and a `DESK_HOLD_MINUTES` window.

## Day to day

```bash
python -m desk queue                    # what's scheduled, and this week's rhythm
```

```bash
python -m desk show 12                  # one post in full: text, images, facts, critic verdict
```

```bash
python -m desk cancel 12                # or: approve 12 (in approve mode)
```

```bash
python -m desk history                  # what went out (with LinkedIn links)
```

```bash
python -m desk calendar                 # the next 120 days of occasions and their date status
```

```bash
python -m desk plan --force             # plan right now (e.g. to test the voice)
```

```bash
python -m desk mourning set 2027-01-15 "Official mourning for ..."
```

```bash
journalctl -u nakoda-linkedin -f
```

### Editing what it knows

- `calendar.yaml`: national days, Islamic occasions (computed with the Umm al-Qura calendar,
  confirmed by announcement), community occasions, and any event you want to fix by hand. Each
  has an importance (`high` always posts, `medium` normally, `optional` when there's something
  genuine to say), a tone and notes for the writer.
- `voice/VOICE.md` and `voice/EXAMPLES.md`: the brief.
- `brand/library.yaml`: our own photos (team at GITEX, the office), with descriptions. Only
  include people who agreed to appear.

## Layout

```
desk/
  __main__.py        CLI: tick, plan, publish, queue, history, show, approve, cancel, calendar, mourning, auth, check
  planner.py         situation → workspace → planner → checks → critic → schedule
  agent.py           the three Claude Agent SDK sessions (planner, critic, calendar) and their prompts
  schema.py          JSON contracts for those sessions
  cadence.py         the human posting rhythm
  occasions.py       calendar.yaml + Hijri dates + confirmations + discovered events
  calendar_refresh.py  confirm dates / discover events
  signals.py         news from the collector DB, scored and sorted into happy / sad / mourning
  media.py           image tools for Claude (search, fetch, check, crop) and the usage policy
  style.py           hard style checks (clichés, emoji, hashtags, repetition, length)
  publisher.py       last-minute checks and posting, retries, dry-run previews
  linkedin.py        OAuth, image upload, Posts API, Little Text escaping
  store.py           SQLite ledger (posts, calendar, signals seen, state)
voice/               VOICE.md, EXAMPLES.md
brand/               our own photos + library.yaml
calendar.yaml
deploy/              systemd service + 15-minute timer, install.sh
tests/
```

## Tests

```bash
pip install -r requirements-dev.txt && python -m pytest -q
```

The pipeline test runs a full plan → critic → schedule → dry-run publish against a fake collector
database with Claude stubbed out. Nothing calls Claude or LinkedIn.
