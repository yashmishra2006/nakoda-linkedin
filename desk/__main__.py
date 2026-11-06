"""Nakoda LinkedIn desk.

    python -m desk tick                  every 15 min (systemd): publish what's due,
                                         refresh the calendar when needed, plan when needed
    python -m desk plan [--force]        run the planner now
    python -m desk publish               publish due posts now
    python -m desk queue                 what is scheduled
    python -m desk history               what went out
    python -m desk show <id>             one post in full
    python -m desk approve <id> | cancel <id>
    python -m desk calendar [show|refresh]
    python -m desk mourning set YYYY-MM-DD "reason" | clear
    python -m desk auth url | code <code-or-redirect-url> | whoami
    python -m desk check                 configuration and connectivity
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import sys
from contextlib import contextmanager
from datetime import date
from urllib.parse import parse_qs, urlparse

from . import cadence, calendar_refresh, linkedin, occasions, planner, publisher
from .config import settings as S
from .store import Store, now_utc, parse_iso

log = logging.getLogger("desk")


@contextmanager
def lock():
    S.data_dir.mkdir(parents=True, exist_ok=True)
    fh = open(S.data_dir / ".desk.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit("another desk run is in progress; skipping")
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _local(ts: str | None) -> str:
    return parse_iso(ts).astimezone(S.tz).strftime("%a %d %b %H:%M") if ts else "-"


async def tick(store: Store) -> dict:
    report: dict = {"published": await publisher.publish_due(store, S)}
    today = now_utc().astimezone(S.tz).date()
    if calendar_refresh.needs_refresh(store, S, today):
        try:
            report["calendar"] = await calendar_refresh.refresh(store, S)
        except Exception as e:  # noqa: BLE001 - calendar trouble never blocks posting
            log.exception("calendar refresh failed")
            report["calendar_error"] = str(e)
    report["plan"] = await planner.run(store, S)
    # A post planned as "urgent" may already be due.
    report["published"] += await publisher.publish_due(store, S)
    return report


def cmd_queue(store: Store) -> None:
    rows = store.pending()
    if not rows:
        print("queue is empty")
    for p in rows:
        print(f"#{p['id']:<4} {p['status']:<18} {_local(p['scheduled_at']):<17} {p['kind']:<11} {p['format']:<11} "
              f"{p['text'].splitlines()[0][:70]}")
    v = cadence.view(S, now_utc(), store.published(60, include_dry=S.dry_run), rows)
    print("\ncadence:", json.dumps(v.as_dict(), indent=1))


def cmd_history(store: Store) -> None:
    for p in store.posts("status NOT IN ('scheduled','awaiting_approval')", order="created_at DESC", limit=30):
        when = _local(p["published_at"] or p["scheduled_at"])
        extra = f"  {linkedin.post_url(p['linkedin_urn'])}" if p["linkedin_urn"].startswith("urn:") else ""
        print(f"#{p['id']:<4} {p['status']:<14} {when:<17} {p['kind']:<11} {p['text'].splitlines()[0][:60]}{extra}"
              + (f"  [{p['error'][:60]}]" if p["error"] else ""))


def cmd_show(store: Store, post_id: int) -> None:
    p = store.get_post(post_id)
    if not p:
        raise SystemExit("no such post")
    print(json.dumps({k: v for k, v in p.items() if k != "critic"}, ensure_ascii=False, indent=1))
    print("\ncritic:", json.dumps(p["critic"].get("final"), ensure_ascii=False), p["critic"].get("scores", ""))


def cmd_calendar(store: Store) -> None:
    today = now_utc().astimezone(S.tz).date()
    for o in occasions.upcoming(store, S.calendar_file, today, days=120):
        status = "confirmed" if o.confirmed else ("EXPECTED" if o.needs_confirmation else "fixed")
        posted = "posted/queued" if store.occasion_handled(o.instance) else ""
        print(f"{o.date} {o.date.strftime('%a')}  {o.name:<52} {o.importance:<9} {status:<10} {posted}")


async def cmd_auth(args) -> None:
    if args.auth_cmd == "url":
        if not S.linkedin_client_id:
            raise SystemExit("set LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET / LINKEDIN_REDIRECT_URI first")
        print("Open this URL while signed in as a page admin, approve, then run\n"
              "  python -m desk auth code '<the full URL you were redirected to>'\n")
        print(linkedin.authorize_url(S))
    elif args.auth_cmd == "code":
        code = args.value
        if code.startswith("http"):
            code = parse_qs(urlparse(code).query).get("code", [""])[0]
        tok = await linkedin.exchange_code(S, code)
        print(f"saved token to {S.token_path} (expires in {int(tok.get('expires_in', 0)) // 86400} days"
              f"{', with refresh token' if tok.get('refresh_token') else ', no refresh token: re-auth before expiry'})")
    elif args.auth_cmd == "whoami":
        tok = linkedin.load_token(S)
        li = linkedin.LinkedIn(S, tok["access_token"])
        me = await li.whoami()
        print(json.dumps(me, indent=1))
        print(f"\nyour person URN: urn:li:person:{me.get('sub')}")
        print(f"author configured: {S.linkedin_author_urn or '(not set)'}; token days left: {linkedin.token_days_left(S)}")


def cmd_check(store: Store) -> int:
    ok = True
    print(f"data dir        {S.data_dir}")
    print(f"dry run         {S.dry_run}   review mode: {S.review_mode}   hold: {S.hold_minutes} min")
    print(f"model           {S.model} (effort {S.effort}), critic {S.critic_model}")
    print(f"news db         {S.news_db_path} {'OK' if S.news_db_path.exists() else 'MISSING (no news signals)'}")
    print(f"nakoda news api {S.news_api_base or '(off)'}")
    print(f"stock photos    pexels={'on' if S.pexels_api_key else 'off'} unsplash={'on' if S.unsplash_access_key else 'off'}")
    print(f"author          {S.linkedin_author_urn or '(not set)'}")
    days = linkedin.token_days_left(S)
    print(f"linkedin token  {'none' if days is None and not S.linkedin_access_token else (f'{days} days left' if days is not None else 'static env token')}")
    print(f"notify webhook  {'on' if S.notify_webhook_url else 'off'}")
    for f in ("VOICE.md", "EXAMPLES.md"):
        if not (S.voice_dir / f).exists():
            print(f"MISSING voice/{f}")
            ok = False
    try:
        today = now_utc().astimezone(S.tz).date()
        nxt = occasions.upcoming(store, S.calendar_file, today, days=60)
        print(f"calendar        {len(nxt)} occasions in the next 60 days")
    except Exception as e:  # noqa: BLE001
        print(f"calendar        ERROR {e}")
        ok = False
    print(f"this week       budget {cadence.weekly_budget(S, today)} regular posts")
    if not S.dry_run and not (S.linkedin_author_urn and (days is not None or S.linkedin_access_token)):
        print("NOT READY to post: set LINKEDIN_AUTHOR_URN and run `desk auth`")
        ok = False
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="desk", description="Nakoda AI LinkedIn desk")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick")
    pl = sub.add_parser("plan")
    pl.add_argument("--force", action="store_true", help="plan even if nothing calls for it")
    sub.add_parser("publish")
    sub.add_parser("queue")
    sub.add_parser("history")
    sub.add_parser("check")
    for name in ("show", "approve", "cancel"):
        sub.add_parser(name).add_argument("id", type=int)
    cal = sub.add_parser("calendar")
    cal.add_argument("action", nargs="?", default="show", choices=["show", "refresh"])
    mo = sub.add_parser("mourning")
    mo.add_argument("action", choices=["set", "clear", "show"])
    mo.add_argument("until", nargs="?")
    mo.add_argument("reason", nargs="?", default="")
    au = sub.add_parser("auth")
    au.add_argument("auth_cmd", choices=["url", "code", "whoami"])
    au.add_argument("value", nargs="?")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s [desk] %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(S.db_path)
    try:
        if args.cmd == "tick":
            with lock():
                print(json.dumps(asyncio.run(tick(store)), ensure_ascii=False, indent=1, default=str))
        elif args.cmd == "plan":
            with lock():
                print(json.dumps(asyncio.run(planner.run(store, S, force=args.force)), ensure_ascii=False,
                                 indent=1, default=str))
        elif args.cmd == "publish":
            with lock():
                print(json.dumps(asyncio.run(publisher.publish_due(store, S)), indent=1, default=str))
        elif args.cmd == "queue":
            cmd_queue(store)
        elif args.cmd == "history":
            cmd_history(store)
        elif args.cmd == "show":
            cmd_show(store, args.id)
        elif args.cmd in ("approve", "cancel"):
            post = store.get_post(args.id)
            if not post or post["status"] not in ("scheduled", "awaiting_approval"):
                raise SystemExit("not a queued post")
            store.update_post(args.id, status="scheduled" if args.cmd == "approve" else "cancelled",
                              error="" if args.cmd == "approve" else "cancelled by hand")
            print(f"#{args.id} {'approved' if args.cmd == 'approve' else 'cancelled'}")
        elif args.cmd == "calendar":
            if args.action == "refresh":
                with lock():
                    print(json.dumps(asyncio.run(calendar_refresh.refresh(store, S)), ensure_ascii=False,
                                     indent=1, default=str))
            else:
                cmd_calendar(store)
        elif args.cmd == "mourning":
            if args.action == "set":
                date.fromisoformat(args.until or "")
                store.set_state("mourning", {"until": args.until, "reason": args.reason, "source": "manual"})
                for post in store.pending():
                    if post["kind"] not in planner.SOLEMN_KINDS and post["tone"] != "solemn":
                        store.update_post(post["id"], status="cancelled", error=f"mourning until {args.until}")
                print(f"mourning until {args.until}; queued posts cancelled")
            elif args.action == "clear":
                store.set_state("mourning", None)
                print("mourning cleared")
            else:
                print(store.get_state("mourning"))
        elif args.cmd == "auth":
            asyncio.run(cmd_auth(args))
        elif args.cmd == "check":
            return cmd_check(store)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
