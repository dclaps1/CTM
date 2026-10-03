"""Cancellations and capacity for the call-center markets: are booked jobs holding, and is there room to book more?

    report = build_ops_report(activity, {"Charleston": (jobs, leads), ...}, today)

Call-center capacity (CTM, last 7 days, from app.brief.coverage): by hour, queue calls, answered, agents on shift,
how busy they were, and missed calls that rang while an agent was on shift and free.

Definitions (the same every run). Jobs marked as a duplicate are left out everywhere.

Cancellations (Workiz)
- Cancel rate: jobs with an appointment in the last `days` that are now canceled ÷ all jobs with an appointment then.
  Compared with the `days` before that.
- Call-center bookings canceled: CTM bookings in the window followed into Workiz (app.pcc), canceled ÷ matched to a job.
- When: the day the job was marked canceled against the appointment: 2+ days ahead, the day before, the day of
  (or the day after: late cancels and no-shows), or later (records closed out after the fact).
- Rebooked: the same client has another job, not canceled, created on or after the canceled one.
- Reason: from the substatus, comments and any "cancel…" sentence in the notes; most jobs record none.
- Cancel rate by days booked ahead uses the last 90 days of appointments, all markets together, to have enough jobs.

Capacity (Workiz + CTM)
- Busy-day capacity: jobs a market has shown it can run in a day, the 90th percentile of weekday job counts (not
  canceled) over the last 60 days, at least 1.
- Next 5 workdays: jobs scheduled Mon–Fri from tomorrow; open = capacity × 5 − scheduled; fill = scheduled ÷ that.
- Wait for appointment: median days from booking to appointment, jobs booked in the last 14 days. Same/next day =
  share of those scheduled within a day.
- Techs: people on the team of a job in the last 30 days. Jobs per tech per week over those 30 days.
- Lost to no slot: CTM opportunities in the window marked Unbookable-Schedule (customer wanted a time we didn't have).
"""
from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

from app.brief import MARKETS, Activity, _hour, coverage, lead_outcome, phone
from app.jobs import _day, _median, _money, _pct, _status, amount
from app.pcc import match_booking, pcc_bookings

DAYS = 30
BUCKET_DAYS = 90
CAPACITY_DAYS = 60
LEAD_DAYS = 14
AHEAD_WORKDAYS = 5
REBOOK_DAYS = 14
COVERAGE_DAYS = 7
LEAD_BUCKETS = [(0, 1, "Same or next day"), (2, 3, "2–3 days"), (4, 7, "4–7 days"), (8, 14, "8–14 days"),
                (15, 10_000, "15+ days")]
AHEAD, DAY_BEFORE, DAY_OF, LATER = "2+ days ahead", "Day before", "Day of", "Marked later"
TIMINGS = (AHEAD, DAY_BEFORE, DAY_OF, LATER)
NO_REASON = "No reason recorded"
_REASONS = [
    ("Price", re.compile(r"too expensive|price|cost|financ|afford|cheaper|budget")),
    ("Went elsewhere / lost job", re.compile(r"lost job|went with|another company|someone else|hired|competitor")),
    ("Wants to reschedule", re.compile(r"reschedul|call back|cb to|different day|another day|availability")),
    ("No longer needed", re.compile(r"no longer|not needed|don.?t need|fixed it|resolved|bought a new|handled it")),
    ("Personal / emergency", re.compile(r"emergency|sick|family|personal|persona issues|out of town")),
    ("No-show / not reached", re.compile(r"no.?show|not home|no answer|couldn.?t reach|unable to reach")),
    ("Insurance", re.compile(r"insurance|adjuster|claim")),
]


def is_duplicate(job: dict) -> bool:
    return "duplicate" in f"{job.get('SubStatus') or ''} {job.get('Comments') or ''}".lower()


def is_canceled(job: dict) -> bool:
    return _status(job).startswith("cancel")


def cancel_reason(job: dict) -> str:
    sub = str(job.get("SubStatus") or "").lower()
    text = " ".join([sub, str(job.get("Comments") or "").lower(),
                     *re.findall(r"cancel\w*[^.\n]{0,120}", str(job.get("JobNotes") or "").lower())])
    for label, pattern in _REASONS:
        if pattern.search(text):
            return label
    return NO_REASON


def cancel_timing(job: dict) -> str:
    appt, when = _day(job.get("JobDateTime")), _day(job.get("LastStatusUpdate"))
    if appt is None or when is None:
        return LATER
    ahead = (appt - when).days
    return AHEAD if ahead >= 2 else DAY_BEFORE if ahead == 1 else DAY_OF if ahead >= -1 else LATER


def lead_days(job: dict) -> int | None:
    appt, made = _day(job.get("JobDateTime")), _day(job.get("CreatedDate"))
    return (appt - made).days if appt and made and appt >= made else None


def rebooked(job: dict, client_jobs: list[dict]) -> bool:
    made = _day(job.get("CreatedDate")) or date.min
    return any(o.get("UUID") != job.get("UUID") and not is_canceled(o) and not is_duplicate(o)
               and (_day(o.get("CreatedDate")) or date.min) >= made for o in client_jobs)


def _clients(jobs: list[dict]) -> dict:
    out: dict = defaultdict(list)
    for j in jobs:
        out[j.get("ClientId") or j.get("UUID")].append(j)
    return out


def _workdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bucket(days: int) -> str:
    return next(label for lo, hi, label in LEAD_BUCKETS if lo <= days <= hi)


def _customer(job: dict) -> str:
    return (str(job.get("FirstName") or "").strip().split() or ["—"])[0].title()


def _row(market: str, job: dict) -> dict:
    return {"market": market, "serial": job.get("SerialId"), "customer": _customer(job),
            "phone": phone(str(job.get("Phone") or "")), "appt": str(job.get("JobDateTime") or "")[:16],
            "service": str(job.get("JobType") or "—"), "total": amount(job.get("JobTotalPrice")),
            "booked_ahead": lead_days(job)}


# -- per market ----------------------------------------------------------------------------------


def market_ops(name: str, jobs: list[dict], act: Activity, today: date, days: int = DAYS) -> dict:
    jobs = [j for j in jobs if not is_duplicate(j)]
    clients = _clients(jobs)
    start, prior = today - timedelta(days=days), today - timedelta(days=2 * days)

    def appt_in(j, a, b):
        d = _day(j.get("JobDateTime"))
        return d is not None and a <= d < b

    window = [j for j in jobs if appt_in(j, start, today)]
    before = [j for j in jobs if appt_in(j, prior, start)]
    canceled = [j for j in window if is_canceled(j)]
    timing = Counter(cancel_timing(j) for j in canceled)

    live = [j for j in jobs if not is_canceled(j)]
    per_day = Counter(_day(j.get("JobDateTime")) for j in live)
    weekdays = [today - timedelta(days=i) for i in range(1, CAPACITY_DAYS + 1)
                if (today - timedelta(days=i)).weekday() < 5]
    counts = sorted(per_day.get(d, 0) for d in weekdays)
    capacity = max(1, counts[int(len(counts) * 0.9)] if counts else 1)
    ahead_days = _workdays(today + timedelta(days=1), AHEAD_WORKDAYS)
    scheduled = sum(per_day.get(d, 0) for d in ahead_days)
    recent = [lead_days(j) for j in jobs if (_day(j.get("CreatedDate")) or date.min) >= today - timedelta(days=LEAD_DAYS)]
    recent = [x for x in recent if x is not None]
    team = [t.get("Name") for j in live if appt_in(j, today - timedelta(days=30), today)
            for t in j.get("Team") or [] if t.get("Name")]
    techs = len(set(team))
    done30 = sum(1 for j in live if appt_in(j, today - timedelta(days=30), today))

    lost_slot = opps = 0
    for first in act.first_touch.values():
        if start <= first["_day"] < today:
            o = lead_outcome(act, first)
            if o["market"] == name and o["opportunity"]:
                opps += 1
                lost_slot += o["status"] == "Unbookable-Schedule"

    return {
        "market": name,
        "appts": len(window), "canceled": len(canceled), "cancel_rate": _ratio(len(canceled), len(window)),
        "prior_cancel_rate": _ratio(sum(is_canceled(j) for j in before), len(before)),
        "timing": {k: timing.get(k, 0) for k in TIMINGS},
        "rebooked": sum(rebooked(j, clients[j.get("ClientId") or j.get("UUID")]) for j in canceled),
        "quoted_lost": round(sum(amount(j.get("JobTotalPrice")) for j in canceled), 2),
        "reasons": dict(Counter(cancel_reason(j) for j in canceled).most_common()),
        "capacity_day": capacity, "scheduled_ahead": scheduled, "open_ahead": max(capacity * AHEAD_WORKDAYS - scheduled, 0),
        "fill_ahead": _ratio(scheduled, capacity * AHEAD_WORKDAYS),
        "wait_days": _median(recent), "same_next_day": _ratio(sum(x <= 1 for x in recent), len(recent)),
        "techs": techs, "jobs_per_tech_week": round(done30 / techs / (30 / 7), 1) if techs else None,
        "lost_to_slot": lost_slot, "opps": opps, "lost_to_slot_rate": _ratio(lost_slot, opps),
    }


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


# -- report --------------------------------------------------------------------------------------


def build_ops_report(act: Activity, workiz: dict[str, tuple[list[dict], list[dict]]], today: date,
                     days: int = DAYS) -> dict:
    markets = [market_ops(name, workiz[name][0], act, today, days) for name, _ in MARKETS if name in workiz]

    buckets: dict[str, Counter] = {label: Counter() for _, _, label in LEAD_BUCKETS}
    confirm, rebook = [], []
    for name, (jobs, _) in workiz.items():
        jobs = [j for j in jobs if not is_duplicate(j)]
        clients = _clients(jobs)
        for j in jobs:
            appt, ahead = _day(j.get("JobDateTime")), lead_days(j)
            if appt and ahead is not None and today - timedelta(days=BUCKET_DAYS) <= appt < today:
                b = buckets[_bucket(ahead)]
                b["appts"] += 1
                b["canceled"] += is_canceled(j)
            if is_canceled(j):
                when = _day(j.get("LastStatusUpdate"))
                if (when and when >= today - timedelta(days=REBOOK_DAYS) and cancel_timing(j) != LATER
                        and cancel_reason(j) not in
                        ("Went elsewhere / lost job", "No longer needed")
                        and not rebooked(j, clients[j.get("ClientId") or j.get("UUID")])):
                    rebook.append({**_row(name, j), "canceled_on": when.isoformat(), "reason": cancel_reason(j)})
            elif appt and today < appt <= today + timedelta(days=2) and (ahead or 0) >= 2 \
                    and not _status(j).startswith("done"):
                confirm.append(_row(name, j))

    booked = canceled = 0
    for b in pcc_bookings(act, today - timedelta(days=days), today - timedelta(days=1)):
        if b["market"] in workiz:
            r = match_booking(b, *workiz[b["market"]], today)
            if r["job"]:
                booked += 1
                canceled += r["outcome"] == "Canceled"

    total = Counter()
    reasons, timing = Counter(), Counter()
    for m in markets:
        total.update({k: m[k] for k in ("appts", "canceled", "rebooked", "quoted_lost", "scheduled_ahead",
                                         "open_ahead", "lost_to_slot", "opps", "techs")})
        total["capacity5"] += m["capacity_day"] * AHEAD_WORKDAYS
        reasons.update(m["reasons"])
        timing.update(m["timing"])
    return {
        "today": today.isoformat(), "days": days,
        "markets": markets,
        "total": {**total, "cancel_rate": _ratio(total["canceled"], total["appts"]),
                  "rebooked_rate": _ratio(total["rebooked"], total["canceled"]),
                  "fill_ahead": _ratio(total["scheduled_ahead"], total["capacity5"]),
                  "lost_to_slot_rate": _ratio(total["lost_to_slot"], total["opps"]),
                  "reasons": dict(reasons.most_common()), "timing": dict(timing),
                  "pcc_booked": booked, "pcc_canceled": canceled, "pcc_cancel_rate": _ratio(canceled, booked)},
        "by_lead_time": [{"bucket": label, "appts": buckets[label]["appts"], "canceled": buckets[label]["canceled"],
                          "rate": _ratio(buckets[label]["canceled"], buckets[label]["appts"])}
                         for _, _, label in LEAD_BUCKETS],
        "coverage": coverage(act, [today - timedelta(days=i) for i in range(1, COVERAGE_DAYS + 1)]),
        "actions": {"confirm": sorted(confirm, key=lambda x: x["appt"]),
                    "rebook": sorted({(x["market"], x["phone"] or x["serial"]): x
                                      for x in sorted(rebook, key=lambda x: x["canceled_on"])}.values(),
                                     key=lambda x: -x["total"])},
    }


# -- rendering -----------------------------------------------------------------------------------


def _days(v: float | None) -> str:
    return "—" if v is None else f"{v:g} d"


def cancel_rows(r: dict) -> list[list[str]]:
    head = ["Market", "Appointments", "Canceled", "Cancel rate", f"Prior {r['days']} d", "2+ days ahead",
            "Day before", "Day of", "Marked later", "Rebooked", "Quoted value lost"]
    rows = [[m["market"], str(m["appts"]), str(m["canceled"]), _pct(m["cancel_rate"]), _pct(m["prior_cancel_rate"]),
             *(str(m["timing"][k]) for k in TIMINGS), str(m["rebooked"]), _money(m["quoted_lost"])]
            for m in sorted(r["markets"], key=lambda m: -(m["cancel_rate"] or 0))]
    t = r["total"]
    rows.append(["All markets", str(t["appts"]), str(t["canceled"]), _pct(t["cancel_rate"]), "",
                 *(str(t["timing"].get(k, 0)) for k in TIMINGS), f"{t['rebooked']} ({_pct(t['rebooked_rate'])})",
                 _money(t["quoted_lost"])])
    return [head] + rows


def capacity_rows(r: dict) -> list[list[str]]:
    head = ["Market", "Busy-day capacity", "Booked next 5 workdays", "Open slots", "Calendar fill",
            "Wait for appointment", "Same/next day", "Techs", "Jobs/tech/week", "Lost to no slot"]
    rows = [[m["market"], f"{m['capacity_day']}/day", str(m["scheduled_ahead"]), str(m["open_ahead"]),
             _pct(m["fill_ahead"]), _days(m["wait_days"]), _pct(m["same_next_day"]), str(m["techs"]),
             "—" if m["jobs_per_tech_week"] is None else f"{m['jobs_per_tech_week']:g}",
             f"{m['lost_to_slot']} of {m['opps']}" if m["opps"] else "—"]
            for m in sorted(r["markets"], key=lambda m: -(m["wait_days"] or 0))]
    return [head] + rows


def lead_rows(r: dict) -> list[list[str]]:
    return [["Booked ahead", "Appointments", "Canceled", "Cancel rate"]] + [
        [b["bucket"], str(b["appts"]), str(b["canceled"]), _pct(b["rate"])] for b in r["by_lead_time"]]


def headline(r: dict) -> list[tuple[str, str, str]]:
    t = r["total"]
    timing = t["timing"]
    late = timing.get(DAY_BEFORE, 0) + timing.get(DAY_OF, 0)
    worst = max(r["by_lead_time"], key=lambda b: b["rate"] or 0)
    waits = sorted((m for m in r["markets"] if m["wait_days"] is not None), key=lambda m: -m["wait_days"])[:2]
    return [
        ("Cancel rate", _pct(t["cancel_rate"]), f"{t['canceled']} of {t['appts']} appointments, last {r['days']} days"),
        ("Call-center bookings canceled", _pct(t["pcc_cancel_rate"]),
         f"{t['pcc_canceled']} of {t['pcc_booked']} that reached a Workiz job"),
        ("Canceled day before or day of", _pct(_ratio(late, t["canceled"])),
         f"{late} of {t['canceled']}; {timing.get(LATER, 0)} more marked later"),
        ("Cancellations rebooked", _pct(t["rebooked_rate"]), f"{t['rebooked']} of {t['canceled']}"),
        ("Highest-risk bookings", worst["bucket"], f"{_pct(worst['rate'])} cancel, last {BUCKET_DAYS} days"),
        ("Booked, next 5 workdays", _pct(t["fill_ahead"]),
         f"{t['scheduled_ahead']} jobs of about {t['capacity5']} busy-day slots"),
        ("Lost to no slot", _pct(t["lost_to_slot_rate"]), f"{t['lost_to_slot']} of {t['opps']} opportunities"),
        ("Longest wait for appointment", _days(waits[0]["wait_days"]) if waits else "—",
         ", ".join(f"{m['market']} {_days(m['wait_days'])}" for m in waits)),
    ]


def action_rows(r: dict) -> list[tuple[str, list[list[str]]]]:
    a = r["actions"]
    confirm = [["Market", "Job", "Customer", "Phone", "Appointment", "Service", "Booked ahead"]] + [
        [x["market"], f"#{x['serial']}", x["customer"], x["phone"], x["appt"], x["service"], _days(x["booked_ahead"])]
        for x in a["confirm"]]
    rebook = [["Market", "Job", "Customer", "Phone", "Was for", "Canceled", "Reason", "Quote"]] + [
        [x["market"], f"#{x['serial']}", x["customer"], x["phone"], x["appt"][:10], x["canceled_on"], x["reason"],
         _money(x["total"]) if x["total"] else "—"]
        for x in a["rebook"]]
    return [(f"Confirm: appointments in the next 2 days booked 2+ days ago ({len(a['confirm'])})", confirm),
            (f"Rebook: canceled in the last {REBOOK_DAYS} days, no new job yet ({len(a['rebook'])})", rebook)]


DEFINITIONS = (
    "Cancel rate = jobs with an appointment in the period now canceled ÷ all jobs with an appointment then "
    "(duplicates left out). Rebooked = the client has a later job that wasn't canceled. Busy-day capacity = the "
    "jobs a market ran on its busier weekdays (90th percentile, last 60 days). Calendar fill = jobs booked for the next "
    "5 workdays ÷ 5 busy days. Wait for appointment = median days from booking to the appointment, jobs booked in the "
    "last 14 days. Lost to no slot = CTM opportunities marked Unbookable-Schedule. Call-center capacity: an agent is "
    "on shift from their first to their last call of the day; busy share = time on calls ÷ time on shift."
)


def _coverage_rows(cov: dict) -> list[list[str]]:
    head = ["Hour", "Queue calls/day", "Answered", "Agents on shift", "Busy share", "Missed", "Missed, agent free"]
    n = cov["days"] or 1
    return [head] + [[_hour(h["hour"]), f"{h['queued'] / n:.1f}", _pct(h["answered_rate"]), f"{h['agents']:g}",
                      _pct(h["busy"]), str(h["missed"]), str(h["missed_free"])]
                     for h in cov["hours"] if h["queued"]]


def render_ops_markdown(r: dict) -> str:
    cov = r.get("coverage")
    def table(rows):
        return "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])] +
                         ["| " + " | ".join(x) + " |" for x in rows[1:]])

    out = [f"# Cancellations & capacity, {r['today']}", ""]
    out += [f"- **{k}:** {v} ({note})" for k, v, note in headline(r)] + [""]
    out += [f"## Cancellations, last {r['days']} days", table(cancel_rows(r)), "",
            "Reasons recorded: " + ", ".join(f"{k} {v}" for k, v in r["total"]["reasons"].items()), "",
            "## Cancel rate by how far ahead the job was booked", table(lead_rows(r)), "",
            "## Tech capacity", table(capacity_rows(r)), ""]
    if cov:
        out += [f"## Call-center capacity, last {cov['days']} days",
                f"{cov['missed_free']} of {cov['missed']} missed calls ({_pct(cov['missed_free_rate'])}) rang while an "
                f"agent was on shift and free. Agents were on calls {_pct(cov['busy'])} of their shift.", "",
                table(_coverage_rows(cov)), ""]
    for heading, rows in action_rows(r):
        out += [f"## {heading}", table(rows) if len(rows) > 1 else "None.", ""]
    return "\n".join(out + [DEFINITIONS]) + "\n"


_CSS = """
@page{size:letter;margin:0.45in 0.5in}
:root{--ink:#17232b;--mute:#566673;--line:#dbe3e8;--accent:#0f6e8c;--warn:#a4551a}
body{font:9pt/1.4 "Helvetica Neue",Arial,sans-serif;color:var(--ink);background:#fff;margin:0}
h1{font-size:17pt;margin:2px 0 4px}h2{font-size:11pt;margin:12px 0 3px}.sub{color:var(--mute);margin:0 0 8px}
.eyebrow{font-size:7.5pt;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:700}
table{width:100%;border-collapse:collapse;font-size:8.2pt;margin:4px 0}
th{text-align:left;font-size:6.8pt;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);border-bottom:1px solid var(--line);padding:3px 4px}
td{padding:3px 4px;border-bottom:1px solid var(--line)}td:first-child{white-space:nowrap}td.r,th.r{text-align:right;white-space:nowrap}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:6px 0}
.tile{border:1px solid var(--line);border-radius:6px;padding:6px 8px}.tile .k{font-size:6.8pt;color:var(--mute);text-transform:uppercase}
.tile .v{font-size:14pt;font-weight:700}.tile .n{font-size:7.5pt;color:var(--mute)}
.note{color:var(--mute);font-size:8pt;margin:2px 0}.foot{font-size:7pt;color:var(--mute);margin-top:8px}
.pb{break-before:page}
"""


def render_ops_html(r: dict) -> str:
    cov = r.get("coverage")
    e = html.escape

    def table(rows):
        if len(rows) < 2:
            return "<p class='note'>None.</p>"
        head = "".join(f'<th class="{"r" if i else ""}">{e(c)}</th>' for i, c in enumerate(rows[0]))
        body = "".join("<tr>" + "".join(f'<td class="{"r" if i else ""}">{e(c)}</td>' for i, c in enumerate(x))
                       + "</tr>" for x in rows[1:])
        return f"<table><tr>{head}</tr>{body}</table>"

    tiles = "".join(f"<div class='tile'><div class='k'>{e(k)}</div><div class='v'>{e(v)}</div>"
                    f"<div class='n'>{e(n)}</div></div>" for k, v, n in headline(r))
    reasons = ", ".join(f"{k} {v}" for k, v in r["total"]["reasons"].items())
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Cancellations &amp; Capacity</title>"
             f"<style>{_CSS}</style></head><body>",
             "<div class='eyebrow'>Voda · Call-center markets · Operations</div>",
             f"<h1>Cancellations &amp; capacity</h1><p class='sub'>As of {e(r['today'])} · the {len(r['markets'])} "
             f"markets on the call center · Workiz jobs and CTM calls</p>",
             f"<div class='tiles'>{tiles}</div>",
             f"<h2>Cancellations, last {r['days']} days</h2>", table(cancel_rows(r)),
             f"<p class='note'>Reasons recorded: {e(reasons)}.</p>",
             "<h2>Cancel rate by how far ahead the job was booked</h2>", table(lead_rows(r)),
             "<h2>Tech capacity</h2>", table(capacity_rows(r))]
    if cov:
        parts += [f"<h2>Call-center capacity, last {cov['days']} days</h2>",
                  f"<p class='note'>{cov['missed_free']} of {cov['missed']} missed calls "
                  f"({_pct(cov['missed_free_rate'])}) rang while an agent was on shift and free. Agents were on calls "
                  f"{_pct(cov['busy'])} of their shift.</p>", table(_coverage_rows(cov))]
    parts.append("<div class='pb'></div>")
    for heading, rows in action_rows(r):
        parts += [f"<h2>{e(heading)}</h2>", table(rows)]
    parts.append(f"<p class='foot'>{e(DEFINITIONS)}</p></body></html>")
    return "".join(parts)
