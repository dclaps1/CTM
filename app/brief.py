"""Daily Call Center Brief: KPIs, action lists and alerts computed from raw CTM activity.

Works on the raw records returned by ``CTMClient.iter_calls`` (calls, texts, forms and chats),
so it needs no database. Every definition lives here, so the numbers are the same every day.

    activity = load_activity(client, report_day, tz)
    brief = build_brief(activity, report_day, tz)
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.ctm_client import CTMClient

HISTORY_DAYS = 90  # how far back we look to decide whether a contact is a new lead
TREND_DAYS = 14  # daily numbers kept for trend charts

# lead_status values set by agents in CTM
OPPORTUNITY_STATUSES = {"Booked", "Bookable-Not Booked", "Unbookable-Price", "Unbookable-Schedule", "Unbookable"}
OPEN_QUOTE_STATUSES = OPPORTUNITY_STATUSES - {"Booked"}
EXCLUDED_STATUSES = {"Spam", "Transferred to Client", "Unbookable-Other", "Unbookable-Service Not Offered"}
LEAD_DIRECTIONS = {"inbound", "form", "chat", "msg_inbound"}

MARKETS = [  # (display name, keyword looked for in location / tracking label / form text)
    ("Greater Boston", "boston"),
    ("Charleston", "charleston"),
    ("South Atlanta", "atlanta"),
    ("Lehigh Valley-Poconos", "lehigh"),
    ("Grand Rapids", "grand rapids"),
    ("Richmond", "richmond"),
    ("Greenville", "greenville"),
    ("Ann Arbor", "ann arbor"),
    ("South Kansas City", "kansas"),
    ("Martinsburg & Winchester", "martinsburg"),
]
OUT_OF_AREA = "Out of Area"
UNASSIGNED = "Unassigned"

_NO_CONVERSATION = re.compile(
    r"voicemail|voice mail|left a message|leave a message|no response|did not respond|didn't respond"
    r"|no one (answered|responded)|before any (substantive )?conversation|no (substantive |further )?conversation"
    r"|automated|recorded message|robocall|no audible|silence|unable to (hear|connect)|disconnected before"
    r"|mailbox|not available to take|press 1|could not hear|hung up (immediately|before)|call screening",
    re.I,
)
_TIME = r"(\d{1,2}(:\d\d)?\s*(am|pm|a\.m\.|p\.m\.)?|noon|nine|ten|eleven|twelve|one|two|three|four)"
_DAY = r"(monday|tuesday|wednesday|thursday|friday|saturday)"
_TWO_TIMES = re.compile(
    rf"\b{_TIME}\b[^.?!]{{0,30}}\bor\b[^.?!]{{0,30}}\b({_TIME}|afternoon|morning)\b"
    rf"|morning or (the )?afternoon|\b{_DAY}\b[^.?!]{{0,30}}\bor\b[^.?!]{{0,30}}\b{_DAY}\b",
    re.I,
)
_STATE_SUFFIX = re.compile(r"\s[A-Z]{2}$")


@dataclass(frozen=True)
class Targets:
    answered_live: float = 1.0
    calls_handled: float = 0.95
    callback_60s: float = 0.80
    web_leads_5min: float = 0.90
    opportunity_conversion: float = 0.60
    jobs_booked: int = 12
    quotes_followed_up: float = 1.0


def status(value: float | None, target: float) -> str:
    """green at/above target; yellow within 15 points of a rate target (or 15% of a count target); else red."""
    if value is None:
        return "none"
    if value >= target:
        return "green"
    margin = 0.15 if target <= 1 else target * 0.15
    return "yellow" if value >= target - margin else "red"


# -- record helpers ------------------------------------------------------------------------------


class Activity:
    """Raw CTM records, indexed for the brief."""

    def __init__(self, records: Iterable[dict[str, Any]], tz: ZoneInfo):
        self.tz = tz
        self.records = sorted({r["id"]: r for r in records}.values(), key=lambda r: r.get("unix_time") or 0)
        self.by_contact: dict[str, list[dict]] = defaultdict(list)
        self.first_touch: dict[str, dict] = {}
        for r in self.records:
            r["_day"] = self.day(r)
            r["_contact"] = contact_key(r)
            r["_conv"] = is_conversation(r)
            if r["_contact"]:
                self.by_contact[r["_contact"]].append(r)
                if r["_contact"] not in self.first_touch and r.get("direction") in LEAD_DIRECTIONS:
                    self.first_touch[r["_contact"]] = r

    def day(self, r: dict) -> date:
        return datetime.fromtimestamp(r.get("unix_time") or 0, timezone.utc).astimezone(self.tz).date()

    def on(self, d: date) -> list[dict]:
        return [r for r in self.records if r["_day"] == d]

    def contact(self, r: dict) -> list[dict]:
        return self.by_contact.get(r["_contact"], [])

    def market(self, r: dict) -> str:
        return market_of(r, self.contact(r))


def contact_key(r: dict) -> str:
    if r.get("direction") == "form":
        n = (r.get("form") or {}).get("customer_number_to_dial")
        if n:
            return str(n)
    return str(r.get("contact_number") or r.get("caller_number") or "")


def is_conversation(r: dict) -> bool:
    if r.get("direction") not in ("inbound", "outbound") or r.get("dial_status") != "answered":
        return False
    if not (r.get("agent") or {}).get("name"):
        return False
    talk = r.get("talk_time") or 0
    summary = r.get("summary") or ""
    if summary:
        return talk >= 30 and not _NO_CONVERSATION.search(summary)
    return talk >= 90


def is_queued_inbound(r: dict) -> bool:
    return r.get("direction") == "inbound" and any(
        (step or {}).get("route_type") == "CallQueue" for step in r.get("call_path") or []
    )


def custom(r: dict) -> dict:
    return r.get("custom_fields") or {}


def latest_status(records: list[dict]) -> str | None:
    statuses = [custom(r).get("lead_status") for r in records if custom(r).get("lead_status")]
    return statuses[-1] if statuses else None


def is_booked_record(r: dict) -> bool:
    return (
        custom(r).get("lead_status") == "Booked"
        or "booked" in (r.get("tag_list") or [])
        or bool((r.get("sale") or {}).get("conversion"))
    )


def market_of(r: dict, contact_records: list[dict]) -> str:
    locations = [
        (custom(x).get("franchise_location") or "").strip()
        for x in contact_records
        if (custom(x).get("franchise_location") or "").strip()
    ]
    location = locations[-1].lower() if locations else ""
    if location == OUT_OF_AREA.lower():
        return OUT_OF_AREA
    for text in (location, (r.get("tracking_label") or "").lower(), str(r.get("form") or "").lower()):
        for name, keyword in MARKETS:
            if keyword in text:
                return name
    return UNASSIGNED


def first_name(r: dict) -> str:
    name = (r.get("name") or (r.get("form") or {}).get("name") or "").strip()
    if not name or (name.isupper() and _STATE_SUFFIX.search(name)):  # caller ID like "MACON GA"
        return "Caller"
    return name.split()[0].title()


def phone(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")[-10:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else (number or "")


def agent_name(r: dict) -> str | None:
    return (r.get("agent") or {}).get("name")


def ratio(num: float, den: float) -> float | None:
    return num / den if den else None


# -- per-day measures ---------------------------------------------------------------------------


@dataclass
class DayNumbers:
    day: str
    queued: int = 0
    answered: int = 0
    missed: int = 0
    callback_60s: int = 0
    callback_connected: int = 0
    median_callback_s: float | None = None
    leads: int = 0
    opportunities: int = 0
    booked_leads: int = 0
    jobs_booked: int = 0
    ticket_total: float = 0.0
    ticket_count: int = 0
    web_leads: int = 0
    web_leads_5min: int = 0
    web_leads_uncalled: int = 0

    @property
    def handled(self) -> int:
        return self.answered + self.callback_60s

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(
            handled=self.handled,
            answered_rate=ratio(self.answered, self.queued),
            handled_rate=ratio(self.handled, self.queued),
            callback_60s_rate=ratio(self.callback_60s, self.missed),
            opportunity_conversion=ratio(self.booked_leads, self.opportunities),
            avg_ticket=ratio(self.ticket_total, self.ticket_count),
            web_leads_5min_rate=ratio(self.web_leads_5min, self.web_leads),
        )
        return d


def lead_outcome(act: Activity, first: dict, upto: float | None = None) -> dict:
    """Where a lead stands: spoke with, opportunity, booked, market."""
    records = [x for x in act.contact(first) if upto is None or (x.get("unix_time") or 0) <= upto]
    booked = any(is_booked_record(x) for x in records)
    st = "Booked" if booked else (latest_status(records) or "No disposition")
    spoke = booked or any(x["_conv"] or x.get("direction") == "chat" for x in records)
    market = market_of(first, records)
    excluded = st in EXCLUDED_STATUSES or market == OUT_OF_AREA
    return {
        "status": st,
        "spoke": spoke,
        "booked": booked,
        "market": market,
        "opportunity": booked or (spoke and not excluded and st in OPPORTUNITY_STATUSES),
    }


def first_outbound_call_after(act: Activity, r: dict, start: float) -> dict | None:
    for x in act.contact(r):
        if x.get("direction") == "outbound" and (x.get("unix_time") or 0) >= start:
            return x
    return None


def day_numbers(act: Activity, d: date) -> DayNumbers:
    out = DayNumbers(day=d.isoformat())
    records = act.on(d)
    callback_delays = []
    for r in records:
        if not is_queued_inbound(r):
            continue
        out.queued += 1
        if r.get("dial_status") == "answered":
            out.answered += 1
            continue
        out.missed += 1
        ended = (r.get("unix_time") or 0) + (r.get("duration") or 0)
        cb = first_outbound_call_after(act, r, r.get("unix_time") or 0)
        if cb:
            delay = (cb.get("unix_time") or 0) - ended
            callback_delays.append(delay)
            if delay <= 60:
                out.callback_60s += 1
                out.callback_connected += cb.get("dial_status") == "answered"
    out.median_callback_s = statistics.median(callback_delays) if callback_delays else None

    for contact, first in act.first_touch.items():
        if first["_day"] != d:
            continue
        o = lead_outcome(act, first)
        out.leads += 1
        out.opportunities += o["opportunity"]
        out.booked_leads += o["booked"] and o["opportunity"]
        if first.get("direction") == "form" and o["market"] not in (OUT_OF_AREA, UNASSIGNED):
            out.web_leads += 1
            cb = first_outbound_call_after(act, first, first.get("unix_time") or 0)
            if cb is None:
                out.web_leads_uncalled += 1
            elif (cb.get("unix_time") or 0) - (first.get("unix_time") or 0) <= 300:
                out.web_leads_5min += 1

    out.jobs_booked = len({r["_contact"] for r in records if r["_conv"] and is_booked_record(r)})
    for r in records:
        sale = r.get("sale") or {}
        value = float(sale.get("value") or 0)
        if sale.get("conversion") and value > 0:
            out.ticket_total += value
            out.ticket_count += 1
    return out


def combine(days: list[DayNumbers], label: str) -> DayNumbers:
    """Sum a run of days (rates are then computed on the totals)."""
    total = DayNumbers(day=label)
    for d in days:
        for field in ("queued", "answered", "missed", "callback_60s", "callback_connected", "leads", "opportunities",
                      "booked_leads", "jobs_booked", "ticket_total", "ticket_count", "web_leads", "web_leads_5min",
                      "web_leads_uncalled"):
            setattr(total, field, getattr(total, field) + getattr(d, field))
    medians = [d.median_callback_s for d in days if d.median_callback_s is not None]
    total.median_callback_s = statistics.median(medians) if medians else None
    return total


# -- action lists -------------------------------------------------------------------------------


def _end_of(act: Activity, d: date) -> float:
    return datetime.combine(d + timedelta(days=1), datetime.min.time(), act.tz).timestamp()


def unreached_missed(act: Activity, d: date, as_of: float | None = None) -> list[dict]:
    """Missed callers on day d (not spam) with no conversation after the miss, as of `as_of`."""
    as_of = as_of if as_of is not None else _end_of(act, d)
    seen, out = set(), []
    for r in act.on(d):
        if not is_queued_inbound(r) or r.get("dial_status") == "answered" or r["_contact"] in seen:
            continue
        seen.add(r["_contact"])
        history = act.contact(r)
        if latest_status(history) == "Spam":
            continue
        later = [x for x in history if (r.get("unix_time") or 0) <= (x.get("unix_time") or 0) <= as_of]
        if any(x["_conv"] for x in later):
            continue
        attempts = [x for x in later if x.get("direction") in ("outbound", "msg_outbound")]
        owner = next((agent_name(x) for x in reversed(attempts) if agent_name(x)), None)
        st = latest_status(history)
        out.append({
            "name": first_name(r), "market": act.market(r), "phone": phone(r["_contact"]),
            "time": datetime.fromtimestamp(r["unix_time"], act.tz).strftime("%-I:%M%p").lower(),
            "attempts": len(attempts), "status": st,
            "owner": "Manager" if st == "Transferred to Client" else (owner or "Unassigned"),
            "contact": r["_contact"],
        })
    return out


def open_quotes(act: Activity, d: date, as_of: float | None = None) -> list[dict]:
    """Contacts we spoke with on day d who aren't booked, and whether they were followed up by `as_of`."""
    out = []
    for contact, history in act.by_contact.items():
        convs = [x for x in history if x["_conv"] and x["_day"] == d]
        if not convs:
            continue
        st = latest_status(history)
        if st not in OPEN_QUOTE_STATUSES or any(is_booked_record(x) for x in history):
            continue
        last = convs[-1]
        follow = [
            x for x in history
            if x.get("direction") in ("outbound", "msg_outbound")
            and (last.get("unix_time") or 0) + 600 < (x.get("unix_time") or 0) <= (as_of or float("inf"))
        ]
        out.append({
            "name": first_name(last), "market": act.market(last), "phone": phone(contact),
            "owner": agent_name(last) or "Unassigned", "status": st,
            "summary": (last.get("summary") or "").strip(), "followed_up": bool(follow),
            "quote": _largest_amount(" ".join(x.get("summary") or "" for x in convs)),
            "contact": contact,
        })
    return out


def _largest_amount(text: str) -> float | None:
    amounts = []
    for m in re.findall(r"\$\s?([\d,]{2,6}(?:\.\d\d)?)", text):
        value = float(m.replace(",", ""))
        if 50 <= value <= 20000:
            amounts.append(value)
    return max(amounts) if amounts else None


# -- agents, markets, alerts --------------------------------------------------------------------


def _agent_lines(r: dict) -> str:
    first = (agent_name(r) or "").split(" ")[0]
    lines = (r.get("transcription_text") or "").splitlines()
    return " ".join(line.split(":", 1)[1] for line in lines if first and line.startswith(first) and ":" in line)


def agent_table(act: Activity, d: date, week: list[date]) -> list[dict]:
    rows: dict[str, Counter] = defaultdict(Counter)
    for contact, history in act.by_contact.items():
        for scope, days in (("d", {d}), ("w", set(week) | {d})):
            convs = [x for x in history if x["_conv"] and x["_day"] in days]
            if not convs:
                continue
            booked = any(is_booked_record(x) for x in convs)
            st = latest_status(history)
            if not (booked or st in OPPORTUNITY_STATUSES):
                continue
            owner = agent_name(max(convs, key=lambda x: x.get("talk_time") or 0))
            rows[owner][f"{scope}_opps"] += 1
            rows[owner][f"{scope}_booked"] += booked
            if scope == "w":
                rows[owner]["w_two_times"] += bool(_TWO_TIMES.search(" ".join(_agent_lines(x) for x in convs)))
    return sorted(
        (
            {
                "agent": name,
                "booked": c["d_booked"], "opps": c["d_opps"], "conversion": ratio(c["d_booked"], c["d_opps"]),
                "week_booked": c["w_booked"], "week_opps": c["w_opps"],
                "week_conversion": ratio(c["w_booked"], c["w_opps"]),
                "two_times_rate": ratio(c["w_two_times"], c["w_opps"]),
            }
            for name, c in rows.items() if name
        ),
        key=lambda row: (-(row["week_conversion"] or 0), row["agent"]),
    )


def market_table(act: Activity, d: date, week: list[date]) -> tuple[list[dict], int]:
    days = set(week) | {d}
    rows: dict[str, Counter] = defaultdict(Counter)
    for contact, first in act.first_touch.items():
        if first["_day"] not in days:
            continue
        o = lead_outcome(act, first)
        c = rows[o["market"]]
        c["leads"] += 1
        c["opps"] += o["opportunity"]
        c["booked"] += o["booked"] and o["opportunity"]
        c["booked_d"] += o["booked"] and o["opportunity"] and first["_day"] == d
    for r in act.records:
        if r["_day"] in days and is_queued_inbound(r):
            c = rows[act.market(r)]
            c["queued"] += 1
            c["answered"] += r.get("dial_status") == "answered"
    out_of_area = rows.pop(OUT_OF_AREA, Counter())["leads"]
    rows.pop(UNASSIGNED, None)
    table = [
        {"market": name, "leads": c["leads"], "opps": c["opps"], "booked": c["booked"], "booked_d": c["booked_d"],
         "conversion": ratio(c["booked"], c["opps"]), "answered_rate": ratio(c["answered"], c["queued"])}
        for name, _ in MARKETS if (c := rows.get(name))
    ]
    return sorted(table, key=lambda row: -row["leads"]), out_of_area


def alerts(act: Activity, d: date, markets: list[dict]) -> list[dict]:
    out = []
    hours: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for r in act.on(d):
        if is_queued_inbound(r):
            h = datetime.fromtimestamp(r["unix_time"], act.tz).hour
            hours[h][0] += 1
            hours[h][1] += r.get("dial_status") == "answered"
    weak = [(h, a, n) for h, (n, a) in sorted(hours.items()) if n >= 3 and a / n < 0.6]
    if weak:
        detail = ", ".join(f"{_hour(h)} {a}/{n}" for h, a, n in weak)
        out.append({"level": "red", "title": "Hours below 60% answered", "detail": detail})

    slow = []
    for contact, first in act.first_touch.items():
        if first["_day"] != d or first.get("direction") != "form":
            continue
        if not 6 <= datetime.fromtimestamp(first["unix_time"], act.tz).hour < 20:  # overnight forms wait by design
            continue
        if market_of(first, act.contact(first)) in (OUT_OF_AREA, UNASSIGNED):
            continue
        cb = first_outbound_call_after(act, first, first.get("unix_time") or 0)
        wait = ((cb.get("unix_time") or 0) - first["unix_time"]) / 60 if cb else None
        if wait is None or wait > 15:
            slow.append((first, wait))
    if slow:
        parts = [
            f"{first_name(f)} ({act.market(f)}) {datetime.fromtimestamp(f['unix_time'], act.tz).strftime('%-I:%M%p').lower()}"
            f" {'never called' if w is None else f'waited {w:.0f} min'}"
            for f, w in slow[:6]
        ]
        out.append({"level": "red", "title": f"{len(slow)} web leads (6am–8pm) waited over 15 minutes", "detail": "; ".join(parts)})

    cold = [m["market"] for m in markets if m["booked"] == 0 and m["leads"] >= 5]
    if cold:
        out.append({"level": "red", "title": "No bookings in 7 days", "detail": ", ".join(cold)})

    spam_dials = sum(
        1 for r in act.on(d)
        if r.get("direction") == "outbound" and r["_conv"] and latest_status(act.contact(r)) == "Spam"
    )
    if spam_dials:
        out.append({"level": "yellow", "title": f"{spam_dials} outbound conversations with spam/vendor numbers",
                    "detail": "Mark these numbers as spam so the dialer skips them."})
    return out


def coverage(act: Activity, days: list[date]) -> dict:
    """Call-center capacity over `days`, by hour of day: queue calls, answered, average agents on shift, how busy
    they were, and missed calls that rang while an agent was on shift and not on a call.

    An agent is on shift from the start of their first call of the day to the end of their last (inbound or
    outbound), and busy while on a call. Busy share = call time ÷ time on shift.
    """
    wanted = set(days)
    shifts: dict[tuple[date, str], list[float]] = {}
    busy: dict[tuple[date, str], list[tuple[float, float]]] = defaultdict(list)
    for r in act.records:
        name = agent_name(r)
        if r["_day"] not in wanted or not name or r.get("direction") not in ("inbound", "outbound"):
            continue
        start = r.get("unix_time") or 0
        end = start + (r.get("duration") or 0)
        s = shifts.setdefault((r["_day"], name), [start, end])
        s[0], s[1] = min(s[0], start), max(s[1], end)
        busy[(r["_day"], name)].append((start, end))
    hours: dict[int, Counter] = defaultdict(Counter)

    def hour_of(t: float) -> int:
        return datetime.fromtimestamp(t, act.tz).hour

    for key, (start, end) in shifts.items():
        t = start
        while t < end:  # split the shift across clock hours
            nxt = min(end, (t // 3600 + 1) * 3600)
            hours[hour_of(t)]["staffed"] += nxt - t
            t = nxt
        for b0, b1 in busy[key]:
            hours[hour_of(b0)]["busy"] += min(b1, end) - b0
    for r in act.records:
        if r["_day"] not in wanted or not is_queued_inbound(r):
            continue
        t = r.get("unix_time") or 0
        c = hours[hour_of(t)]
        c["queued"] += 1
        if r.get("dial_status") == "answered":
            c["answered"] += 1
            continue
        c["missed"] += 1
        on = [k for k, (s0, s1) in shifts.items() if k[0] == r["_day"] and s0 <= t <= s1]
        c["missed_free"] += any(not any(b0 <= t <= b1 for b0, b1 in busy[k]) for k in on)
    n = len(days) or 1
    rows = [{"hour": h, "queued": c["queued"], "answered": c["answered"], "missed": c["missed"],
             "missed_free": c["missed_free"], "answered_rate": ratio(c["answered"], c["queued"]),
             "agents": round(c["staffed"] / 3600 / n, 1), "busy": ratio(c["busy"], c["staffed"]),
             "calls_per_agent_hour": ratio(c["queued"], c["staffed"] / 3600)}
            for h, c in sorted(hours.items()) if c["queued"] or c["staffed"]]
    total = Counter()
    for c in hours.values():
        total.update(c)
    worst = max(rows, key=lambda x: (x["missed"], x["queued"]), default=None)
    return {"days": len(days), "hours": rows, "worst_hour": worst["hour"] if worst and worst["missed"] else None,
            "missed": total["missed"], "missed_free": total["missed_free"],
            "missed_free_rate": ratio(total["missed_free"], total["missed"]),
            "busy": ratio(total["busy"], total["staffed"]), "agent_hours": round(total["staffed"] / 3600, 1)}


def _hour(h: int) -> str:
    return f"{(h - 1) % 12 + 1}{'am' if h < 12 else 'pm'}"


# -- assembly -----------------------------------------------------------------------------------


def build_brief(act: Activity, d: date, targets: Targets = Targets(), now: float | None = None) -> dict:
    prior = d - timedelta(days=1)
    week = [d - timedelta(days=i) for i in range(1, 8)]  # the 7 days before d, for the averages
    recent = [d - timedelta(days=i) for i in range(1, 7)]  # d plus these = "last 7 days" for agents/markets
    numbers = {x: day_numbers(act, x) for x in [d - timedelta(days=i) for i in range(TREND_DAYS)]}
    today, yesterday = numbers[d], numbers[prior]
    seven = combine([numbers[x] for x in week], "7-day")

    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    end_prior = _end_of(act, prior)

    prior_unreached = unreached_missed(act, prior, as_of=end_prior)
    prior_contacts = {x["contact"] for x in prior_unreached}
    reached_since = sum(
        1 for contact in prior_contacts
        if any(x["_conv"] and (x.get("unix_time") or 0) > end_prior for x in act.by_contact[contact])
    )
    prior_quotes_24h = open_quotes(act, prior, as_of=end_prior + 86400)
    prior_quotes_now = open_quotes(act, prior, as_of=now)
    quotes_d = open_quotes(act, d, as_of=now)

    markets, out_of_area = market_table(act, d, recent)
    scores = {
        "answered_live": (today.as_dict()["answered_rate"], targets.answered_live),
        "calls_handled": (today.as_dict()["handled_rate"], targets.calls_handled),
        "callback_60s": (today.as_dict()["callback_60s_rate"], targets.callback_60s),
        "web_leads_5min": (today.as_dict()["web_leads_5min_rate"], targets.web_leads_5min),
        "opportunity_conversion": (today.as_dict()["opportunity_conversion"], targets.opportunity_conversion),
        "jobs_booked": (today.jobs_booked, targets.jobs_booked),
        "quotes_followed_up": (
            ratio(sum(q["followed_up"] for q in prior_quotes_24h), len(prior_quotes_24h)), targets.quotes_followed_up),
    }
    return {
        "day": d.isoformat(),
        "generated_at": datetime.fromtimestamp(now, act.tz).isoformat(timespec="minutes"),
        "targets": asdict(targets),
        "numbers": {"day": today.as_dict(), "prior": yesterday.as_dict(), "seven_day": seven.as_dict()},
        "trend": [numbers[x].as_dict() for x in sorted(numbers)],
        "status": {k: {"value": v, "target": t, "status": status(v, t)} for k, (v, t) in scores.items()},
        "actions": {
            "call_back": unreached_missed(act, d, as_of=now),
            "open_quotes": [q for q in quotes_d if not q["followed_up"]],
            "carryover": [q for q in prior_quotes_now if not q["followed_up"]],
        },
        "follow_through": {
            "missed_reached": {"done": reached_since, "of": len(prior_contacts)},
            "quotes_followed_up": {"done": sum(q["followed_up"] for q in prior_quotes_24h), "of": len(prior_quotes_24h)},
            "web_leads_called": {"done": yesterday.web_leads - yesterday.web_leads_uncalled, "of": yesterday.web_leads,
                                 "within_5min": yesterday.web_leads_5min},
        },
        "alerts": alerts(act, d, markets),
        "agents": agent_table(act, d, recent),
        "markets": markets,
        "out_of_area_leads": out_of_area,
        "coverage": {"day": coverage(act, [d]), "seven_day": coverage(act, week)},
    }


def load_activity(client: CTMClient, d: date, tz: ZoneInfo, history_days: int = HISTORY_DAYS) -> Activity:
    """Pull CTM activity from `history_days` before `d` through today (later records show follow-ups)."""
    start = d - timedelta(days=history_days)
    end = datetime.now(tz).date() + timedelta(days=1)
    return Activity(client.iter_calls(start, end, per_page=150), tz)
