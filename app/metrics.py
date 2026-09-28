"""Call filtering and KPI calculations for dashboards and reports."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models import Agent, Call, CallReview


@dataclass
class CallFilters:
    start: date
    end: date
    agent_id: str | None = None
    direction: str | None = None  # inbound | outbound
    outcome: str | None = None  # answered | missed | voicemail
    source: str | None = None
    tag: str | None = None
    q: str | None = None

    def as_params(self) -> dict[str, str]:
        params = {k: v for k, v in asdict(self).items() if v not in (None, "")}
        params["start"], params["end"] = self.start.isoformat(), self.end.isoformat()
        if "agent_id" in params:
            params["agent"] = params.pop("agent_id")
        return {k: str(v) for k, v in params.items()}


def utc_bounds(start: date, end: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local [start 00:00, end+1 00:00) expressed as naive UTC."""
    lo = datetime.combine(start, time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return lo, hi


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def call_query(filters: CallFilters, tz: ZoneInfo) -> Select[tuple[Call]]:
    lo, hi = utc_bounds(filters.start, filters.end, tz)
    stmt = select(Call).where(Call.called_at >= lo, Call.called_at < hi)
    if filters.agent_id:
        stmt = stmt.where(Call.agent_id == filters.agent_id)
    if filters.direction in ("inbound", "outbound"):
        stmt = stmt.where(Call.direction == filters.direction)
    if filters.outcome == "answered":
        stmt = stmt.where(Call.answered.is_(True))
    elif filters.outcome == "missed":
        stmt = stmt.where(Call.direction == "inbound", Call.answered.is_(False))
    elif filters.outcome == "voicemail":
        stmt = stmt.where(Call.voicemail.is_(True))
    if filters.source:
        stmt = stmt.where(Call.source == filters.source)
    if filters.tag:
        stmt = stmt.where(Call.tags.contains(filters.tag))
    if filters.q:
        text = filters.q.strip()
        digits = re.sub(r"\D", "", text)
        conditions = [Call.caller_name.ilike(f"%{text}%"), Call.notes.ilike(f"%{text}%")]
        if len(digits) >= 3:
            conditions.append(Call.caller_number.contains(digits))
        stmt = stmt.where(or_(*conditions))
    return stmt.order_by(Call.called_at.desc())


def _avg(total: float, count: int) -> float | None:
    return total / count if count else None


def summarize(calls: Sequence[Call], filters: CallFilters, tz: ZoneInfo) -> dict:
    inbound = [c for c in calls if c.is_inbound]
    answered = [c for c in calls if c.answered]
    answered_in = [c for c in inbound if c.answered]
    conversions = [c for c in calls if c.sale_conversion]

    days: dict[date, dict[str, int]] = {}
    d = filters.start
    while d <= filters.end:
        days[d] = {"answered": 0, "missed": 0, "outbound": 0}
        d += timedelta(days=1)
    hours = [{"answered": 0, "missed": 0} for _ in range(24)]
    sources: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "answered": 0, "conversions": 0})

    for c in calls:
        local = to_local(c.called_at, tz)
        bucket = days.get(local.date())
        if c.is_inbound:
            key = "answered" if c.answered else "missed"
            hours[local.hour][key] += 1
            if bucket is not None:
                bucket[key] += 1
            src = sources[c.source or "(no source)"]
            src["calls"] += 1
            src["answered"] += int(c.answered)
            src["conversions"] += int(bool(c.sale_conversion))
        elif bucket is not None:
            bucket["outbound"] += 1

    return {
        "total": len(calls),
        "inbound": len(inbound),
        "outbound": len(calls) - len(inbound),
        "answered": len(answered),
        "missed": len(inbound) - len(answered_in),
        "voicemail": sum(1 for c in inbound if c.voicemail),
        "answer_rate": _avg(len(answered_in), len(inbound)),
        "avg_talk": _avg(sum(c.talk_time for c in answered), len(answered)),
        "total_talk": sum(c.talk_time for c in answered),
        "avg_ring": _avg(sum(c.ring_time for c in answered_in), len(answered_in)),
        "new_callers": sum(1 for c in inbound if c.is_new_caller),
        "conversions": len(conversions),
        "revenue": sum(c.sale_value or 0 for c in conversions),
        "recordings": sum(1 for c in calls if c.audio_url),
        "by_day": [{"date": k.isoformat(), **v} for k, v in days.items()],
        "by_hour": [{"hour": h, **v} for h, v in enumerate(hours)],
        "by_source": sorted(
            ({"source": k, **v} for k, v in sources.items()), key=lambda r: r["calls"], reverse=True
        )[:10],
    }


def review_stats(session: Session, filters: CallFilters, tz: ZoneInfo) -> dict[str | None, dict]:
    """QA score average/count per agent for reviews created in the date range."""
    lo, hi = utc_bounds(filters.start, filters.end, tz)
    stmt = (
        select(CallReview.agent_id, func.avg(CallReview.score), func.count(CallReview.id))
        .where(CallReview.created_at >= lo, CallReview.created_at < hi)
        .group_by(CallReview.agent_id)
    )
    if filters.agent_id:
        stmt = stmt.where(CallReview.agent_id == filters.agent_id)
    return {agent_id: {"avg": avg, "count": count} for agent_id, avg, count in session.execute(stmt)}


def leaderboard(session: Session, calls: Sequence[Call], filters: CallFilters, tz: ZoneInfo) -> list[dict]:
    """Per-agent stats. Missed calls have no agent, so only handled calls are attributed."""
    names = {a.id: a.name for a in session.scalars(select(Agent))}
    rows: dict[str, dict] = {}

    def row_for(agent_id: str, fallback_name: str = "") -> dict:
        return rows.setdefault(agent_id, {
            "agent_id": agent_id, "name": names.get(agent_id) or fallback_name or agent_id,
            "calls": 0, "inbound": 0, "outbound": 0, "answered": 0, "answered_in": 0,
            "talk": 0, "ring": 0, "conversions": 0, "revenue": 0.0,
        })

    for c in calls:
        if not c.agent_id:
            continue
        row = row_for(c.agent_id, c.agent_name)
        row["calls"] += 1
        row["inbound" if c.is_inbound else "outbound"] += 1
        if c.answered:
            row["answered"] += 1
            row["talk"] += c.talk_time
            if c.is_inbound:
                row["answered_in"] += 1
                row["ring"] += c.ring_time
        if c.sale_conversion:
            row["conversions"] += 1
            row["revenue"] += c.sale_value or 0

    qa = review_stats(session, filters, tz)
    for agent_id in qa:
        if agent_id:
            row_for(agent_id)
    for row in rows.values():
        row["avg_talk"] = _avg(row["talk"], row["answered"])
        row["avg_ring"] = _avg(row["ring"], row["answered_in"])
        stats = qa.get(row["agent_id"])
        row["qa_avg"] = stats["avg"] if stats else None
        row["qa_count"] = stats["count"] if stats else 0
    return sorted(rows.values(), key=lambda r: r["calls"], reverse=True)


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")[-10:]


def missed_callbacks(session: Session, filters: CallFilters, tz: ZoneInfo) -> list[dict]:
    """Missed inbound calls in range, each paired with the first later call to/from that number (if any)."""
    missed_filters = CallFilters(filters.start, filters.end, source=filters.source, q=filters.q,
                                 outcome="missed", direction="inbound")
    missed = session.scalars(call_query(missed_filters, tz)).all()
    if not missed:
        return []
    earliest = min(c.called_at for c in missed)
    later = session.scalars(
        select(Call).where(Call.called_at > earliest).order_by(Call.called_at.asc())
    ).all()
    followups: dict[str, list[Call]] = defaultdict(list)
    for c in later:
        # A follow-up is an answered inbound call or any outbound call placed to the number.
        if c.is_inbound and not c.answered:
            continue
        numbers = {_digits(c.caller_number)}
        if not c.is_inbound:
            numbers.add(_digits(c.receiving_number))
        for n in numbers - {""}:
            followups[n].append(c)

    rows = []
    for c in missed:
        number = _digits(c.caller_number)
        followup = next((f for f in followups.get(number, []) if f.called_at > c.called_at), None)
        if filters.agent_id and followup is not None and followup.agent_id != filters.agent_id:
            continue
        rows.append({"call": c, "followup": followup})
    # Open callbacks first, then newest first.
    rows.sort(key=lambda r: (r["followup"] is not None, -r["call"].called_at.timestamp()))
    return rows
