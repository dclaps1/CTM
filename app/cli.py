"""Command line helpers.

    python -m app.cli init-db
    python -m app.cli create-user admin@example.com --name "Pat" --role admin
    python -m app.cli sync [--days 90]
    python -m app.cli agents
    python -m app.cli brief [--date 2026-09-30] [--html brief.html] [--markdown brief.md] [--json brief.json]
                            [--dashboard dashboard.html] [--notes notes.txt]
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import db
from app.auth import hash_password
from app.brief import build_brief, load_activity
from app.brief_render import render_html, render_markdown
from app.dashboard import render_dashboard
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
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.command == "brief":
        return run_brief(args, settings)
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


def run_brief(args: argparse.Namespace, settings) -> int:
    tz = ZoneInfo(settings.timezone)
    day = date.fromisoformat(args.date) if args.date else datetime.now(tz).date() - timedelta(days=1)
    with CTMClient.from_settings(settings) as client:
        brief = build_brief(load_activity(client, day, tz), day)
    notes = args.notes.read_text() if args.notes else ""
    if args.json:
        args.json.write_text(json.dumps(brief, indent=2, default=str))
    if args.html:
        args.html.write_text(render_html(brief, notes))
    if args.dashboard:
        args.dashboard.write_text(render_dashboard(brief, notes))
    if args.markdown or not (args.json or args.html or args.dashboard):
        text = render_markdown(brief, notes)
        if args.markdown:
            args.markdown.write_text(text)
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
