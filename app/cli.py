"""Command line helpers.

    python -m app.cli init-db
    python -m app.cli create-user admin@example.com --name "Pat" --role admin
    python -m app.cli sync [--days 90]
    python -m app.cli agents
    python -m app.cli brief [--date 2026-09-30] [--html brief.html] [--markdown brief.md] [--json brief.json]
                            [--dashboard dashboard.html] [--notes notes.txt]
    python -m app.cli jobs [--market "Greater Boston"] [--pdf jobs.pdf] [--html jobs.html] [--markdown jobs.md]
    python -m app.cli workiz-probe [--market "Greater Boston"] [--days 30]
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import db
from app.auth import hash_password
from app.brief import build_brief, load_activity
from app.brief_render import render_html, render_markdown
from app.dashboard import render_dashboard, render_jobs_report
from app.jobs import build_jobs_report, fetch_workiz, render_jobs_markdown
from app.workiz_client import MARKET_ENV, WorkizClient, WorkizError, configured_markets
from app.config import get_settings
from app.ctm_client import CTMClient
from app.models import ROLES, Agent, User
from app.sync import sync_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create database tables")
    cu = sub.add_parser("create-user", help="add a login")
    cu.add_argument("email")
    cu.add_argument("--name", required=True)
    cu.add_argument("--role", choices=ROLES, default="agent")
    cu.add_argument("--agent-id", help="CTM agent (user) id to link, for agent logins")
    cu.add_argument("--password", help="omit to be prompted")
    sy = sub.add_parser("sync", help="pull calls and agents from CTM")
    sy.add_argument("--days", type=int, help="re-pull this many days instead of syncing incrementally")
    sub.add_parser("agents", help="list CTM agents known locally")
    br = sub.add_parser("brief", help="build the Daily Call Center Brief straight from CTM")
    br.add_argument("--date", help="report day, YYYY-MM-DD (default: yesterday)")
    br.add_argument("--html", type=Path, help="write email-ready HTML here")
    br.add_argument("--markdown", type=Path, help="write Markdown here (printed when no output is given)")
    br.add_argument("--json", type=Path, help="write the raw numbers and action lists here")
    br.add_argument("--dashboard", type=Path, help="write the full dashboard page (tabs, charts) here")
    br.add_argument("--notes", type=Path, help="text file with commentary to put under the title")
    jb = sub.add_parser("jobs", help="Workiz jobs & revenue report, matched to CTM by phone number")
    jb.add_argument("--market", help="one market, e.g. 'Greater Boston' (default: every configured market)")
    jb.add_argument("--json", type=Path, help="write the raw report here")
    jb.add_argument("--markdown", type=Path, help="write Markdown here (printed when no output is given)")
    jb.add_argument("--html", type=Path, help="write the printable report (HTML) here")
    jb.add_argument("--pdf", type=Path, help="write the report as a PDF (needs Chromium or Chrome installed)")
    wp = sub.add_parser("workiz-probe", help="test Workiz connections and show what job data comes back")
    wp.add_argument("--market", help="one market, e.g. 'Greater Boston' (default: every configured market)")
    wp.add_argument("--days", type=int, default=30, help="look at jobs from this many days back")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.command == "brief":
        return run_brief(args, settings)
    if args.command == "jobs":
        return run_jobs(args, settings)
    if args.command == "workiz-probe":
        return run_workiz_probe(args)
    db.configure(settings.database_url)
    db.init_db()

    if args.command == "init-db":
        print("Database ready.")
    elif args.command == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if len(password) < 8:
            print("Password must be at least 8 characters.", file=sys.stderr)
            return 1
        with db.new_session() as session:
            if session.scalar(select(User).where(User.email == args.email.lower())):
                print(f"{args.email} already exists.", file=sys.stderr)
                return 1
            if args.agent_id and session.get(Agent, args.agent_id) is None:
                print(f"Unknown agent id {args.agent_id}; run `sync` first or check `agents`.", file=sys.stderr)
                return 1
            session.add(User(email=args.email.lower(), name=args.name, role=args.role,
                             agent_id=args.agent_id, password_hash=hash_password(password)))
            session.commit()
        print(f"Created {args.role} {args.email}.")
    elif args.command == "sync":
        result = sync_once(lambda: CTMClient.from_settings(settings), settings.initial_sync_days, days=args.days)
        print(json.dumps(result, indent=2))
    elif args.command == "agents":
        with db.new_session() as session:
            for agent in session.scalars(select(Agent).order_by(Agent.name)):
                print(f"{agent.id}\t{agent.name}\t{agent.email or ''}")
    return 0


def run_workiz_probe(args: argparse.Namespace) -> int:
    """Connect to each configured Workiz account and report field coverage (never prints the token)."""
    from collections import Counter

    markets = configured_markets()
    if args.market:
        markets = {m: t for m, t in markets.items() if m.lower() == args.market.lower()}
    if not markets:
        names = ", ".join(f"WORKIZ_TOKEN_{s}" for s in MARKET_ENV.values())
        print(f"No Workiz token found. Set one of: {names}", file=sys.stderr)
        return 1
    start = date.today() - timedelta(days=args.days)
    for market, token in markets.items():
        print(f"== {market}")
        try:
            with WorkizClient(token) as client:
                jobs = list(client.iter_jobs(start))
                team = client.team()
        except WorkizError as exc:
            print(f"   connection failed: {exc}")
            continue
        fields = Counter(k for job in jobs for k, v in job.items() if v not in (None, "", [], {}))
        print(f"   connected · {len(jobs)} jobs since {start} · {len(team)} team members")
        print("   fields filled (share of jobs): " + ", ".join(
            f"{k} {v / len(jobs):.0%}" for k, v in sorted(fields.items(), key=lambda kv: -kv[1])) if jobs else "   no jobs")
        statuses = Counter(str(j.get("Status")) for j in jobs)
        print(f"   statuses: {dict(statuses.most_common(10))}")
    return 0


def jobs_reports(act, today: date, market: str | None = None) -> list[dict]:
    """One Jobs & Revenue report per market with a Workiz token. A market whose Workiz call fails is skipped
    with a warning, so the rest still runs."""
    reports = []
    for name, token in configured_markets().items():
        if market and name.lower() != market.lower():
            continue
        try:
            with WorkizClient(token) as client:
                jobs, leads = fetch_workiz(client, today)
        except WorkizError as exc:
            print(f"Workiz {name}: {exc}", file=sys.stderr)
            continue
        reports.append(build_jobs_report(name, jobs, leads, act, today))
    return reports


def run_jobs(args: argparse.Namespace, settings) -> int:
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    if not configured_markets():
        print("No Workiz token found. Set WORKIZ_TOKEN_<MARKET>; see README.", file=sys.stderr)
        return 1
    with CTMClient.from_settings(settings) as client:
        act = load_activity(client, today, tz)
    reports = jobs_reports(act, today, args.market)
    if not reports:
        print("No Workiz market could be read.", file=sys.stderr)
        return 1
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, default=str))
    if args.html or args.pdf:
        page = render_jobs_report(reports)
        if args.html:
            args.html.write_text(page)
        if args.pdf:
            write_pdf(page, args.pdf)
    if args.markdown or not (args.json or args.html or args.pdf):
        text = render_jobs_markdown(reports)
        if args.markdown:
            args.markdown.write_text(text)
        else:
            print(text)
    return 0


def write_pdf(page: str, out: Path) -> None:
    """Print an HTML page to PDF with headless Chromium/Chrome (CHROME_PATH, or the first one on PATH)."""
    import glob
    import shutil
    import subprocess
    import tempfile

    candidates = [os.environ.get("CHROME_PATH")] + [shutil.which(n) for n in (
        "chromium", "chromium-browser", "google-chrome", "google-chrome-stable")]
    candidates += sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    browser = next((c for c in candidates if c and os.path.exists(c)), None)
    if not browser:
        raise SystemExit("No Chromium or Chrome found for --pdf; set CHROME_PATH or use --html and print it.")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.html"
        src.write_text(page)
        subprocess.run([browser, "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                        "--virtual-time-budget=5000", f"--print-to-pdf={out.resolve()}", src.as_uri()],
                       check=True, capture_output=True)


def run_brief(args: argparse.Namespace, settings) -> int:
    tz = ZoneInfo(settings.timezone)
    day = date.fromisoformat(args.date) if args.date else datetime.now(tz).date() - timedelta(days=1)
    with CTMClient.from_settings(settings) as client:
        act = load_activity(client, day, tz)
    brief = build_brief(act, day)
    notes = args.notes.read_text() if args.notes else ""
    if args.json:
        args.json.write_text(json.dumps(brief, indent=2, default=str))
    if args.html:
        args.html.write_text(render_html(brief, notes))
    if args.dashboard:
        jobs = None
        if configured_markets():
            today = datetime.now(tz).date()
            jobs = jobs_reports(act, today)
        args.dashboard.write_text(render_dashboard(brief, notes, jobs))
    if args.markdown or not (args.json or args.html or args.dashboard):
        text = render_markdown(brief, notes)
        if args.markdown:
            args.markdown.write_text(text)
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
