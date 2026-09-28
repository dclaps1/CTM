"""Command line helpers.

    python -m app.cli init-db
    python -m app.cli create-user admin@example.com --name "Pat" --role admin
    python -m app.cli sync [--days 90]
    python -m app.cli agents
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from sqlalchemy import select

from app import db
from app.auth import hash_password
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
    args = parser.parse_args(argv)

    settings = get_settings()
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


if __name__ == "__main__":
    raise SystemExit(main())
