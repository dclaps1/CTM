"""Web app: dashboards, call log, recordings, missed-call callbacks, QA reviews and the CTM webhook.

Run with:  uvicorn --factory app.main:create_app
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import math
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, selectinload
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.auth import NotAuthenticated, admin_user, current_user, hash_password, manager_user, verify_password
from app.config import Settings, get_settings
from app.ctm_client import CTMClient, CTMError
from app.metrics import (
    CallFilters,
    call_query,
    leaderboard,
    missed_callbacks,
    review_stats,
    summarize,
    to_local,
    utc_bounds,
)
from app.models import ROLES, Agent, Call, CallReview, User, utcnow
from app.scorecard import CRITERIA, MAX_POINTS, score_form, to_ctm_score
from app.sync import SyncWorker, get_state, sync_once, upsert_call

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent
PER_PAGE = 50
NO_AGENT = "__none__"  # agent users without a linked CTM agent see no calls


async def form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_phone(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return number or ""


def _fmt_pct(value: float | None, scale: float = 1.0) -> str:
    return "—" if value is None else f"{value * scale:.0f}%"


def _parse_date(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[], CTMClient] | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    db.configure(settings.database_url)
    db.init_db()
    tz = ZoneInfo(settings.timezone)
    make_client = client_factory or (lambda: CTMClient.from_settings(settings))

    if settings.secret_key == "change-me":
        log.warning("SECRET_KEY is the default value; set a long random SECRET_KEY before going live")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = None
        if start_worker and settings.ctm_configured and settings.sync_interval_minutes > 0:
            worker = SyncWorker(make_client, settings.sync_interval_minutes, settings.initial_sync_days)
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="CTM Call Center Hub", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.session_https_only,
        max_age=12 * 3600,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters.update(
        duration=_fmt_duration,
        phone=_fmt_phone,
        pct=_fmt_pct,
        local=lambda dt, fmt="%b %d, %Y %I:%M %p": to_local(dt, tz).strftime(fmt) if dt else "",
        money=lambda v: "—" if v is None else f"${v:,.0f}",
    )

    # -- helpers -------------------------------------------------------------

    def flash(request: Request, message: str, kind: str = "ok") -> None:
        request.session.setdefault("flashes", []).append({"kind": kind, "message": message})

    def render(request: Request, name: str, user: User | None, **ctx: Any) -> HTMLResponse:
        flashes = request.session.pop("flashes", [])
        return templates.TemplateResponse(
            request, name, {"user": user, "flashes": flashes, "settings": settings, **ctx}
        )

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def today_local() -> date:
        return datetime.now(tz).date()

    def parse_filters(request: Request, user: User, default_days: int = 7) -> CallFilters:
        p = request.query_params
        end = _parse_date(p.get("end"), today_local())
        start = _parse_date(p.get("start"), end - timedelta(days=default_days - 1))
        if start > end:
            start, end = end, start
        agent_id = p.get("agent") or None
        if not user.is_manager:
            agent_id = user.agent_id or NO_AGENT
        return CallFilters(
            start=start,
            end=end,
            agent_id=agent_id,
            direction=p.get("direction") or None,
            outcome=p.get("outcome") or None,
            source=p.get("source") or None,
            tag=p.get("tag") or None,
            q=p.get("q") or None,
        )

    def filter_context(session: Session, filters: CallFilters) -> dict[str, Any]:
        sources = session.scalars(
            select(Call.source).where(Call.source != "").distinct().order_by(Call.source)
        ).all()
        agents = session.scalars(select(Agent).order_by(Agent.name)).all()
        today = today_local()
        presets = [
            ("Today", today, today),
            ("Yesterday", today - timedelta(days=1), today - timedelta(days=1)),
            ("7 days", today - timedelta(days=6), today),
            ("30 days", today - timedelta(days=29), today),
            ("Month to date", today.replace(day=1), today),
        ]
        return {
            "filters": filters,
            "sources": sources,
            "agents": agents,
            "presets": [(label, s.isoformat(), e.isoformat()) for label, s, e in presets],
            "query": urlencode({k: v for k, v in filters.as_params().items() if v != NO_AGENT}),
        }

    def visible_call(session: Session, user: User, call_id: int) -> Call:
        call = session.get(Call, call_id)
        if call is None or (not user.is_manager and (not user.agent_id or call.agent_id != user.agent_id)):
            raise HTTPException(status_code=404, detail="Call not found")
        return call

    def light_calls(session: Session, filters: CallFilters) -> list[Call]:
        stmt = call_query(filters, tz).options(defer(Call.raw), defer(Call.transcript), defer(Call.notes))
        return list(session.scalars(stmt).all())

    # -- auth ----------------------------------------------------------------

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, _exc: NotAuthenticated):
        return redirect(f"/login?next={quote(request.url.path)}")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, session: Session = Depends(db.get_db)):
        if session.scalar(select(func.count(User.id))):
            return redirect("/login")
        return render(request, "setup.html", None)

    @app.post("/setup")
    def setup_submit(request: Request, form: dict = Depends(form_data), session: Session = Depends(db.get_db)):
        if session.scalar(select(func.count(User.id))):
            return redirect("/login")
        email, name, password = form.get("email", "").strip().lower(), form.get("name", "").strip(), form.get("password", "")
        if not email or not name or len(password) < 8:
            flash(request, "Name, email and a password of at least 8 characters are required.", "error")
            return redirect("/setup")
        user = User(email=email, name=name, role="admin", password_hash=hash_password(password))
        session.add(user)
        session.commit()
        request.session["uid"] = user.id
        flash(request, "Admin account created. Next: sync CTM and add your team.")
        return redirect("/admin")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, session: Session = Depends(db.get_db)):
        if not session.scalar(select(func.count(User.id))):
            return redirect("/setup")
        return render(request, "login.html", None, next=request.query_params.get("next", "/"))

    @app.post("/login")
    def login_submit(request: Request, form: dict = Depends(form_data), session: Session = Depends(db.get_db)):
        email = form.get("email", "").strip().lower()
        user = session.scalar(select(User).where(User.email == email))
        if user is None or not user.active or not verify_password(form.get("password", ""), user.password_hash):
            flash(request, "Incorrect email or password.", "error")
            return redirect("/login")
        request.session.clear()
        request.session["uid"] = user.id
        target = form.get("next") or "/"
        return redirect(target if target.startswith("/") and not target.startswith("//") else "/")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return redirect("/login")

    # -- dashboard -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: User = Depends(current_user), session: Session = Depends(db.get_db)):
        filters = parse_filters(request, user)
        calls = light_calls(session, filters)
        summary = summarize(calls, filters, tz)
        qa = review_stats(session, filters, tz)
        qa_count = sum(s["count"] for s in qa.values())
        qa_avg = (sum(s["avg"] * s["count"] for s in qa.values()) / qa_count) if qa_count else None
        board = leaderboard(session, calls, filters, tz) if user.is_manager else []
        pending_ack = []
        if not user.is_manager and user.agent_id:
            pending_ack = session.scalars(
                select(CallReview).where(CallReview.agent_id == user.agent_id, CallReview.acknowledged_at.is_(None))
                .order_by(CallReview.created_at.desc()).limit(10)
            ).all()
        chart = {"by_day": summary["by_day"], "by_hour": summary["by_hour"]}
        return render(
            request, "dashboard.html", user, nav="dashboard", summary=summary, board=board,
            qa_avg=qa_avg, qa_count=qa_count, pending_ack=pending_ack, chart_json=json.dumps(chart),
            **filter_context(session, filters),
        )

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request, user: User = Depends(manager_user), session: Session = Depends(db.get_db)):
        filters = parse_filters(request, user, default_days=30)
        filters.agent_id = None
        calls = light_calls(session, filters)
        board = leaderboard(session, calls, filters, tz)
        return render(request, "agents.html", user, nav="agents", board=board, **filter_context(session, filters))

    # -- calls ---------------------------------------------------------------

    @app.get("/calls", response_class=HTMLResponse)
    def calls_page(request: Request, user: User = Depends(current_user), session: Session = Depends(db.get_db)):
        filters = parse_filters(request, user)
        stmt = call_query(filters, tz)
        total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        pages = max(1, math.ceil(total / PER_PAGE))
        try:
            page = min(max(1, int(request.query_params.get("page", 1))), pages)
        except ValueError:
            page = 1
        rows = session.scalars(
            stmt.options(defer(Call.raw), defer(Call.transcript), selectinload(Call.reviews))
            .limit(PER_PAGE).offset((page - 1) * PER_PAGE)
        ).all()
        return render(
            request, "calls.html", user, nav="calls", calls=rows, total=total, page=page, pages=pages,
            **filter_context(session, filters),
        )

    @app.get("/calls/export.csv")
    def export_calls(request: Request, user: User = Depends(current_user), session: Session = Depends(db.get_db)):
        filters = parse_filters(request, user)
        calls = light_calls(session, filters)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "call_id", "called_at", "direction", "outcome", "status", "caller_number", "caller_name",
            "city", "state", "source", "tracking_label", "agent", "ring_seconds", "talk_seconds",
            "duration_seconds", "new_caller", "tags", "conversion", "sale_value", "has_recording",
        ])
        for c in calls:
            writer.writerow([
                c.id, to_local(c.called_at, tz).strftime("%Y-%m-%d %H:%M:%S"), c.direction, c.outcome, c.status,
                c.caller_number, c.caller_name, c.caller_city, c.caller_state, c.source, c.tracking_label,
                c.agent_name, c.ring_time, c.talk_time, c.duration, "yes" if c.is_new_caller else "no",
                ";".join(c.tag_list), "" if c.sale_conversion is None else ("yes" if c.sale_conversion else "no"),
                "" if c.sale_value is None else c.sale_value, "yes" if c.has_recording else "no",
            ])
        filename = f"calls_{filters.start}_{filters.end}.csv"
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/calls/{call_id}", response_class=HTMLResponse)
    def call_detail(call_id: int, request: Request, user: User = Depends(current_user),
                    session: Session = Depends(db.get_db)):
        call = visible_call(session, user, call_id)
        history = session.scalars(
            select(Call).where(Call.caller_number == call.caller_number, Call.id != call.id, Call.caller_number != "")
            .options(defer(Call.raw), defer(Call.transcript)).order_by(Call.called_at.desc()).limit(10)
        ).all()
        if not user.is_manager:
            history = [h for h in history if h.agent_id == user.agent_id]
        return render(
            request, "call_detail.html", user, nav="calls", call=call, history=history,
            criteria=CRITERIA, max_points=MAX_POINTS,
        )

    @app.get("/calls/{call_id}/recording")
    def call_recording(call_id: int, request: Request, user: User = Depends(current_user),
                       session: Session = Depends(db.get_db)):
        call = visible_call(session, user, call_id)
        if not call.audio_url:
            raise HTTPException(status_code=404, detail="No recording for this call")
        audio_url = call.audio_url
        session.close()  # don't hold a DB connection while streaming audio
        try:
            client = make_client()
        except CTMError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            upstream = client.open_recording(audio_url, request.headers.get("range"))
        except CTMError as exc:
            client.close()
            log.warning("Recording fetch failed for call %s: %s", call_id, exc)
            raise HTTPException(status_code=502, detail="Could not fetch the recording from CTM") from exc

        headers = {k: upstream.headers[k] for k in ("content-type", "content-length", "content-range", "accept-ranges")
                   if k in upstream.headers}
        headers.setdefault("content-type", "audio/mpeg")
        headers["cache-control"] = "private, max-age=300"
        if request.query_params.get("download"):
            headers["content-disposition"] = f'attachment; filename="call-{call_id}.mp3"'

        def body():
            try:
                yield from upstream.iter_bytes()
            finally:
                upstream.close()
                client.close()

        return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)

    # -- reviews / coaching ----------------------------------------------------

    @app.post("/calls/{call_id}/reviews")
    def create_review(call_id: int, request: Request, form: dict = Depends(form_data),
                      user: User = Depends(manager_user), session: Session = Depends(db.get_db)):
        call = visible_call(session, user, call_id)
        criteria, score = score_form(form)
        if score is None:
            flash(request, "Score at least one scorecard item.", "error")
            return redirect(f"/calls/{call_id}#review")
        review = CallReview(call_id=call.id, reviewer_id=user.id, agent_id=call.agent_id, score=score,
                            criteria=criteria, feedback=form.get("feedback", "").strip())
        session.add(review)
        session.commit()
        if settings.push_scores_to_ctm and form.get("push_to_ctm"):
            try:
                with make_client() as client:
                    client.record_sale(call.id, name="QA review", score=to_ctm_score(score),
                                       conversion=call.sale_conversion, value=call.sale_value)
                review.pushed_to_ctm = True
                call.sale_score = to_ctm_score(score)
                session.commit()
            except CTMError as exc:
                log.warning("Pushing score to CTM failed for call %s: %s", call.id, exc)
                flash(request, "Review saved, but the score could not be sent to CTM.", "error")
                return redirect(f"/calls/{call_id}#reviews")
        flash(request, f"Review saved ({score:.0f}%).")
        return redirect(f"/calls/{call_id}#reviews")

    @app.post("/reviews/{review_id}/delete")
    def delete_review(review_id: int, request: Request, user: User = Depends(manager_user),
                      session: Session = Depends(db.get_db)):
        review = session.get(CallReview, review_id)
        if review is None:
            raise HTTPException(status_code=404)
        call_id = review.call_id
        if review.reviewer_id != user.id and not user.is_admin:
            raise HTTPException(status_code=403, detail="Only the reviewer or an admin can delete a review")
        session.delete(review)
        session.commit()
        flash(request, "Review deleted.")
        return redirect(f"/calls/{call_id}#reviews")

    @app.post("/reviews/{review_id}/ack")
    def acknowledge_review(review_id: int, request: Request, form: dict = Depends(form_data),
                           user: User = Depends(current_user), session: Session = Depends(db.get_db)):
        review = session.get(CallReview, review_id)
        if review is None or not user.agent_id or review.agent_id != user.agent_id:
            raise HTTPException(status_code=404)
        if review.acknowledged_at is None:
            review.acknowledged_at = utcnow()
            session.commit()
        flash(request, "Feedback acknowledged.")
        target = form.get("next", "")
        return redirect(target if target.startswith("/") and not target.startswith("//") else "/reviews")

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews_page(request: Request, user: User = Depends(current_user), session: Session = Depends(db.get_db)):
        filters = parse_filters(request, user, default_days=30)
        lo, hi = utc_bounds(filters.start, filters.end, tz)
        stmt = (
            select(CallReview).where(CallReview.created_at >= lo, CallReview.created_at < hi)
            .options(selectinload(CallReview.call).defer(Call.raw), selectinload(CallReview.reviewer))
            .order_by(CallReview.created_at.desc())
        )
        if filters.agent_id:
            stmt = stmt.where(CallReview.agent_id == filters.agent_id)
        reviews = session.scalars(stmt).all()
        names = {a.id: a.name for a in session.scalars(select(Agent))}
        return render(request, "reviews.html", user, nav="reviews", reviews=reviews, agent_names=names,
                      criteria=dict(CRITERIA), **filter_context(session, filters))

    # -- missed-call callbacks ---------------------------------------------------

    @app.get("/callbacks", response_class=HTMLResponse)
    def callbacks_page(request: Request, user: User = Depends(current_user), session: Session = Depends(db.get_db)):
        filters = parse_filters(request, user)
        if not user.is_manager:
            # Agents see the whole open queue (missed calls have no agent) but only their own follow-ups.
            filters.agent_id = user.agent_id or NO_AGENT
        rows = missed_callbacks(session, filters, tz)
        now = utcnow()
        for row in rows:
            hours = max(0, int((now - row["call"].called_at).total_seconds() // 3600))
            row["waiting"] = f"{hours}h" if hours < 48 else f"{hours // 24}d"
        open_count = sum(1 for r in rows if r["followup"] is None)
        return render(request, "callbacks.html", user, nav="callbacks", rows=rows, open_count=open_count,
                      **filter_context(session, filters))

    # -- admin -----------------------------------------------------------------

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, user: User = Depends(admin_user), session: Session = Depends(db.get_db)):
        users = session.scalars(select(User).order_by(User.role, User.name)).all()
        agents = session.scalars(select(Agent).order_by(Agent.name)).all()
        sync = {k: get_state(session, k) for k in
                ("last_sync_at", "last_sync_result", "last_sync_error", "calls_synced_through")}
        call_count = session.scalar(select(func.count(Call.id))) or 0
        webhook_url = str(request.base_url).rstrip("/") + "/webhooks/ctm?token=" + (
            settings.webhook_token if settings.webhook_token else "<set WEBHOOK_TOKEN>")
        return render(request, "admin.html", user, nav="admin", users=users, agents=agents, roles=ROLES,
                      sync=sync, call_count=call_count, webhook_url=webhook_url)

    @app.post("/admin/users")
    def admin_create_user(request: Request, form: dict = Depends(form_data), user: User = Depends(admin_user),
                          session: Session = Depends(db.get_db)):
        email = form.get("email", "").strip().lower()
        name = form.get("name", "").strip()
        role = form.get("role", "agent")
        password = form.get("password", "")
        if not email or not name or role not in ROLES or len(password) < 8:
            flash(request, "Name, email, a valid role and a password of 8+ characters are required.", "error")
            return redirect("/admin")
        if session.scalar(select(User).where(User.email == email)):
            flash(request, f"{email} already has an account.", "error")
            return redirect("/admin")
        agent_id = form.get("agent_id") or None
        if agent_id and session.get(Agent, agent_id) is None:
            agent_id = None
        session.add(User(email=email, name=name, role=role, agent_id=agent_id, password_hash=hash_password(password)))
        session.commit()
        flash(request, f"Added {name}.")
        return redirect("/admin")

    @app.post("/admin/users/{user_id}")
    def admin_update_user(user_id: int, request: Request, form: dict = Depends(form_data),
                          user: User = Depends(admin_user), session: Session = Depends(db.get_db)):
        target = session.get(User, user_id)
        if target is None:
            raise HTTPException(status_code=404)
        role = form.get("role", target.role)
        if role in ROLES and not (target.id == user.id and role != "admin"):
            target.role = role
        agent_id = form.get("agent_id") or None
        target.agent_id = agent_id if agent_id and session.get(Agent, agent_id) else None
        if target.id != user.id:
            target.active = form.get("active") == "on"
        password = form.get("password", "")
        if password:
            if len(password) < 8:
                flash(request, "Passwords must be at least 8 characters.", "error")
                return redirect("/admin")
            target.password_hash = hash_password(password)
        session.commit()
        flash(request, f"Updated {target.name}.")
        return redirect("/admin")

    @app.post("/admin/sync")
    def admin_sync(request: Request, form: dict = Depends(form_data), user: User = Depends(admin_user)):
        if not settings.ctm_configured:
            flash(request, "Set CTM_ACCESS_KEY, CTM_SECRET_KEY and CTM_ACCOUNT_ID first.", "error")
            return redirect("/admin")
        try:
            days = int(form["days"]) if form.get("days") else None
        except ValueError:
            days = None

        def run():
            try:
                sync_once(make_client, settings.initial_sync_days, days=days)
            except Exception:
                log.exception("Manual CTM sync failed")

        threading.Thread(target=run, name="ctm-sync-manual", daemon=True).start()
        flash(request, "Sync started. Refresh in a minute to see the result.")
        return redirect("/admin")

    # -- CTM webhook -------------------------------------------------------------

    @app.post("/webhooks/ctm")
    async def ctm_webhook(request: Request):
        if not settings.webhook_token:
            raise HTTPException(status_code=404)
        token = request.query_params.get("token") or request.headers.get("x-webhook-token") or ""
        if not hmac.compare_digest(token.encode(), settings.webhook_token.encode()):
            raise HTTPException(status_code=401, detail="Bad token")
        try:
            payload = await request.json()
        except ValueError:
            payload = dict(await request.form())
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        data = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        if not data.get("id"):
            raise HTTPException(status_code=400, detail="Missing call id")
        call_id = await run_in_threadpool(_ingest_webhook_call, data)
        return JSONResponse({"ok": True, "call_id": call_id})

    def _ingest_webhook_call(data: dict[str, Any]) -> int | None:
        raw = data
        if settings.ctm_configured:
            # Prefer the authoritative record from the API over the webhook body.
            try:
                with make_client() as client:
                    raw = client.get_call(data["id"])
            except CTMError as exc:
                log.warning("Webhook: could not fetch call %s from CTM, using payload: %s", data.get("id"), exc)
        session = db.new_session()
        try:
            call = upsert_call(session, raw)
            session.commit()
            return call.id if call else None
        finally:
            session.close()

    return app
