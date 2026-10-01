"""Jobs & Revenue: one location's Workiz jobs, matched to CTM by phone number, boiled down to what a
leadership team needs: how are we doing, what needs action, and where the money comes from.

    report = build_jobs_report("Greater Boston", jobs, leads, activity, today)

How jobs flow: the call center books a job in Workiz with a quoted price; the franchise owner does the work and
marks it Done with the real revenue, or "done pending approval" while waiting to be paid.

Definitions (the same every run):

- Completed: marked Done or "done pending approval" with a total above $0. Its completion date is the day its
  status last changed. Revenue = completed job totals, counted on the completion date.
- Close rate: of the jobs *booked* (created in Workiz) in the rate window, sold ÷ decided. Sold = completed.
  Decided = sold, canceled, or the appointment date has passed. Future and in-progress jobs are left out.
- Not closed out: the appointment date has passed but the job is still open (not Done, canceled or in
  progress), or it is Done at $0. These count as decided, not sold.
- Owed: open balance on a completed job, aged from the day it was completed.
- Days to first visit: booked → first appointment. Days to done: booked → completed.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from app.brief import MARKETS, Activity, first_name, is_booked_record, phone

UNKNOWN = "(not set)"
FLOOD_IT = "Flood It"
PERIOD_DAYS = 30  # headline revenue and bookings, compared with the 30 days before
RATE_DAYS = 90  # close rate, ticket and speed need more jobs to be steady
HISTORY_DAYS = 400  # money owed and clean-up lists look back this far
OWED_ALERT_DAYS = 30
SLOW_VISIT_DAYS = 7
MIN_JOBS = 5  # a service or source needs this many jobs before the report calls it out


# -- small helpers ---------------------------------------------------------------------------------


def digits10(number: Any) -> str:
    d = re.sub(r"\D", "", str(number or ""))[-10:]
    return d if len(d) == 10 else ""


def amount(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _dt(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _day(v: Any) -> date | None:
    d = _dt(v)
    return d.date() if d else None


def _days_between(a: Any, b: Any) -> float | None:
    x, y = _dt(a), _dt(b)
    return max((y - x).total_seconds() / 86400, 0.0) if x and y else None


def _median(values: list[float | None]) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def job_phones(job: dict) -> set[str]:
    return {p for p in (digits10(job.get("Phone")), digits10(job.get("SecondPhone"))) if p}


def _status(job: dict) -> str:
    return str(job.get("Status") or "").strip().lower()


def _name(job: dict, key: str) -> str:
    return str(job.get(key) or "").strip() or UNKNOWN


def source_family(job: dict) -> str:
    """Lead source, with every "Flood It - …" source rolled up into one "Flood It"."""
    name = _name(job, "JobSource")
    return FLOOD_IT if name.lower().startswith("flood it") else name


def outcome(job: dict, today: date) -> str:
    """sold (completed with revenue) / canceled / missing (not closed out) / pending (future or in progress)."""
    status = _status(job)
    if status.startswith("cancel"):
        return "canceled"
    if status.startswith("done"):
        return "sold" if amount(job.get("JobTotalPrice")) > 0 else "missing"
    if status == "in progress":
        return "pending"
    when = _day(job.get("JobDateTime"))
    return "missing" if when is not None and when < today else "pending"


def completed_on(job: dict) -> date | None:
    """Completion date of a sold job (the day its status last changed), else None."""
    if not _status(job).startswith("done") or amount(job.get("JobTotalPrice")) <= 0:
        return None
    return _day(job.get("LastStatusUpdate")) or _day(job.get("JobEndDateTime"))


def owed(job: dict) -> float:
    due = amount(job.get("JobAmountDue"))
    return due if due >= 1 and completed_on(job) else 0.0  # ignore rounding pennies


def _in(d: date | None, start: date, end: date) -> bool:
    return d is not None and start <= d <= end


def _job_row(job: dict) -> dict:
    team = ", ".join(str(t.get("Name") or "").strip() for t in job.get("Team") or [] if t.get("Name"))
    return {
        "serial": job.get("SerialId"),
        "appt": str(job.get("JobDateTime") or "")[:10],
        "status": " / ".join(x for x in (job.get("Status"), job.get("SubStatus")) if x),
        "service": _name(job, "JobType"),
        "source": _name(job, "JobSource"),
        "customer": (str(job.get("FirstName") or "").strip().split() or ["—"])[0].title(),
        "tech": team or "—",
        "total": amount(job.get("JobTotalPrice")),
    }


# -- measures ------------------------------------------------------------------------------------


def _group_stats(booked: list[dict], completed: list[dict], today: date) -> dict:
    """booked: jobs created in the rate window; completed: jobs completed in it."""
    outs = [outcome(j, today) for j in booked]
    sold, decided = outs.count("sold"), sum(o != "pending" for o in outs)
    revenue = sum(amount(j.get("JobTotalPrice")) for j in completed)
    return {
        "booked": len(booked),
        "sold": sold,
        "decided": decided,
        "close_rate": sold / decided if decided else None,
        "completed": len(completed),
        "revenue": round(revenue, 2),
        "avg_ticket": round(revenue / len(completed), 2) if completed else None,
        "days_to_visit": _median([_days_between(j.get("CreatedDate"), j.get("JobDateTime")) for j in booked]),
        "days_to_done": _median([_days_between(j.get("CreatedDate"), j.get("LastStatusUpdate")) for j in completed]),
    }


def _breakdown(booked: list[dict], completed: list[dict], key, today: date) -> list[dict]:
    names = {key(j) for j in booked + completed}
    rows = [{"name": n, **_group_stats([j for j in booked if key(j) == n], [j for j in completed if key(j) == n], today)}
            for n in names]
    return sorted(rows, key=lambda r: (-r["revenue"], -r["booked"], r["name"]))


def _fold(rows: list[dict], booked: list[dict], completed: list[dict], key, today: date, what: str) -> list[dict]:
    """Keep rows with at least MIN_JOBS bookings or 5% of revenue; fold the rest into one "Other" row."""
    total = sum(r["revenue"] for r in rows) or 1
    keep = [r for r in rows if r["booked"] >= MIN_JOBS or r["revenue"] >= 0.05 * total]
    small = {r["name"] for r in rows} - {r["name"] for r in keep}
    if len(small) < 2:
        return rows
    other = _group_stats([j for j in booked if key(j) in small], [j for j in completed if key(j) in small], today)
    return keep + [{"name": f"Other {what} ({len(small)})", **other, "folded": sorted(small)}]


def _monthly(jobs: list[dict], today: date, months: int = 6) -> list[dict]:
    first = date(today.year, today.month, 1)
    starts = []
    for _ in range(months):
        starts.insert(0, first)
        first = (first - timedelta(days=1)).replace(day=1)
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] - timedelta(days=1) if i + 1 < len(starts) else today
        done = [j for j in jobs if _in(completed_on(j), s, e)]
        out.append({"month": s.isoformat()[:7], "label": s.strftime("%b"), "partial": e == today,
                     "revenue": round(sum(amount(j.get("JobTotalPrice")) for j in done), 2), "jobs": len(done)})
    return out


def _owed(jobs: list[dict], today: date) -> dict:
    rows = []
    for j in jobs:
        if due := owed(j):
            rows.append({**_job_row(j), "completed": completed_on(j).isoformat(),
                         "days": (today - completed_on(j)).days, "owed": round(due, 2),
                         "insurance": bool(j.get("insurance_company1") or j.get("insurance_company"))})
    rows.sort(key=lambda r: -r["days"])
    buckets = []
    for label, lo, hi in (("0–30 days", 0, 30), ("31–60 days", 31, 60), ("61–90 days", 61, 90), ("90+ days", 91, 10**6)):
        b = [r for r in rows if lo <= r["days"] <= hi]
        buckets.append({"label": label, "jobs": len(b), "owed": round(sum(r["owed"] for r in b), 2)})
    late = [r for r in rows if r["days"] > OWED_ALERT_DAYS]
    return {"total": round(sum(r["owed"] for r in rows), 2), "jobs": len(rows), "buckets": buckets,
            "late_total": round(sum(r["owed"] for r in late), 2), "late": late}


def _cleanup(jobs: list[dict], today: date) -> dict:
    not_closed = [{**_job_row(j), "why": "Done at $0" if _status(j).startswith("done") else "Still open"}
                  for j in jobs if outcome(j, today) == "missing" and amount(j.get("JobAmountDue")) > -1]
    paid_no_revenue = [{**_job_row(j), "collected": round(-amount(j.get("JobAmountDue")), 2)}
                       for j in jobs if _status(j).startswith("done") and amount(j.get("JobTotalPrice")) <= 0
                       and amount(j.get("JobAmountDue")) <= -1]
    canceled_balance = [{**_job_row(j), "owed": round(amount(j.get("JobAmountDue")), 2)}
                        for j in jobs if _status(j).startswith("cancel") and amount(j.get("JobAmountDue")) >= 1]
    for rows in (not_closed, paid_no_revenue, canceled_balance):
        rows.sort(key=lambda r: r["appt"])
    return {"not_closed": not_closed, "paid_no_revenue": paid_no_revenue, "canceled_balance": canceled_balance}


def _ctm_bookings(market: str, jobs: list[dict], leads: list[dict], act: Activity, start: date) -> dict:
    job_phones_all = {p for j in jobs for p in job_phones(j)}
    lead_phones_all = {p for x in leads for p in job_phones(x)}
    booked = []
    for contact, records in act.by_contact.items():
        d = digits10(contact)
        hits = [r for r in records if is_booked_record(r) and r["_day"] >= start]
        if not d or not hits or act.market(records[-1]) != market:
            continue
        first = hits[0]
        booked.append({"booked": first["_day"].isoformat(), "name": first_name(first), "phone": phone(contact),
                       "agent": (first.get("agent") or {}).get("name") or "—",
                       "workiz": "job" if d in job_phones_all else "lead only" if d in lead_phones_all else "none"})
    booked.sort(key=lambda b: b["booked"])
    return {"total": len(booked), "missing": [b for b in booked if b["workiz"] != "job"]}


def _actions(r: dict) -> list[str]:
    """Plain-language list of what needs doing, biggest money first."""
    out = []
    o = r["owed"]
    if o["late"]:
        big = max(o["late"], key=lambda x: x["owed"])
        out.append(f"Collect {_money(o['late_total'])} owed for more than {OWED_ALERT_DAYS} days on {len(o['late'])} "
                   f"jobs. Largest: #{big['serial']} {big['service']}, {_money(big['owed'])}, {big['days']} days.")
    c = r["cleanup"]
    if c["paid_no_revenue"]:
        total = sum(x["collected"] for x in c["paid_no_revenue"])
        out.append(f"Enter the revenue on {len(c['paid_no_revenue'])} jobs that were paid ({_money(total)}) "
                   f"but are marked Done at $0.")
    if c["not_closed"]:
        out.append(f"Close out {len(c['not_closed'])} jobs whose appointment has passed. "
                   "Until they are marked Done or canceled, close rate and revenue read low.")
    if c["canceled_balance"]:
        total = sum(x["owed"] for x in c["canceled_balance"])
        out.append(f"Clear the {_money(total)} balance still showing on {len(c['canceled_balance'])} canceled jobs.")
    if r["on_ctm"] and r["ctm"]["missing"]:
        out.append(f"{len(r['ctm']['missing'])} call-center bookings in the last {RATE_DAYS} days never became a "
                   "Workiz job. Check each one.")
    services = [s for s in r["by_service"] if s["booked"] >= MIN_JOBS]
    slow = [s for s in services if (s["days_to_visit"] or 0) > SLOW_VISIT_DAYS]
    if slow:
        out.append("Customers wait over a week for a first visit on "
                   + ", ".join(f"{s['name']} ({s['days_to_visit']:.0f} days)" for s in slow) + ".")
    overall = r["rates"]["close_rate"]
    weak = [s for s in services if s["decided"] >= MIN_JOBS and s["close_rate"] is not None and overall
            and s["close_rate"] < overall * 0.6]
    for s in weak:
        out.append(f"{s['name']} closes {_pct(s['close_rate'])} of jobs vs {_pct(overall)} overall "
                   f"({s['sold']} of {s['decided']}).")
    return out


def build_jobs_report(market: str, jobs: list[dict], leads: list[dict], act: Activity, today: date,
                      period_days: int = PERIOD_DAYS, rate_days: int = RATE_DAYS) -> dict:
    """`jobs` should reach back about HISTORY_DAYS; `act` is CTM activity covering at least `rate_days`."""
    period_start = today - timedelta(days=period_days - 1)
    prior_start, prior_end = period_start - timedelta(days=period_days), period_start - timedelta(days=1)
    rate_start = today - timedelta(days=rate_days - 1)
    rate_prior_start, rate_prior_end = rate_start - timedelta(days=rate_days), rate_start - timedelta(days=1)

    def booked_in(s, e):
        return [j for j in jobs if _in(_day(j.get("CreatedDate")), s, e)]

    def completed_in(s, e):
        return [j for j in jobs if _in(completed_on(j), s, e)]

    rate_booked, rate_done = booked_in(rate_start, today), completed_in(rate_start, today)
    rates, prior_rates = _group_stats(rate_booked, rate_done, today), \
        _group_stats(booked_in(rate_prior_start, rate_prior_end), completed_in(rate_prior_start, rate_prior_end), today)
    period_done, prior_done = completed_in(period_start, today), completed_in(prior_start, prior_end)

    flood_parts = _breakdown([j for j in rate_booked if source_family(j) == FLOOD_IT],
                             [j for j in rate_done if source_family(j) == FLOOD_IT],
                             lambda j: _name(j, "JobSource"), today)
    by_source = _fold(_breakdown(rate_booked, rate_done, source_family, today), rate_booked, rate_done,
                      source_family, today, "sources")
    for s in by_source:
        s["parts"] = flood_parts if s["name"] == FLOOD_IT and len(flood_parts) > 1 else []
    service = lambda j: _name(j, "JobType")  # noqa: E731

    report = {
        "market": market,
        "on_ctm": market in {name for name, _ in MARKETS},
        "today": today.isoformat(),
        "period": {"days": period_days, "start": period_start.isoformat(), "prior_start": prior_start.isoformat()},
        "rate_days": rate_days,
        "headline": {
            "revenue": {"now": round(sum(amount(j.get("JobTotalPrice")) for j in period_done), 2),
                        "prior": round(sum(amount(j.get("JobTotalPrice")) for j in prior_done), 2)},
            "completed": {"now": len(period_done), "prior": len(prior_done)},
            "booked": {"now": len(booked_in(period_start, today)), "prior": len(booked_in(prior_start, prior_end))},
        },
        "rates": rates,
        "prior_rates": prior_rates,
        "monthly": _monthly(jobs, today),
        "by_service": _fold(_breakdown(rate_booked, rate_done, service, today), rate_booked, rate_done, service,
                            today, "services"),
        "by_source": by_source,
        "owed": _owed(jobs, today),
        "cleanup": _cleanup(jobs, today),
        "ctm": _ctm_bookings(market, jobs, leads, act, rate_start),
    }
    report["headline"]["note"] = _lumpy_note(period_done, prior_done, period_days)
    report["actions"] = _actions(report)
    return report


def _lumpy_note(now: list[dict], prior: list[dict], days: int) -> str:
    """Point out when one job is a big share of a period's revenue, so a swing is not misread."""
    notes = []
    for label, js in (("this", now), ("the prior", prior)):
        total = sum(amount(j.get("JobTotalPrice")) for j in js)
        if not js or not total:
            continue
        big = max(js, key=lambda j: amount(j.get("JobTotalPrice")))
        share = amount(big.get("JobTotalPrice")) / total
        if share >= 0.25 and len(js) > 1:
            notes.append(f"one job (#{big.get('SerialId')}, {_name(big, 'JobType')}, "
                         f"{_money(amount(big.get('JobTotalPrice')))}) is {_pct(share)} of {label} {days} days")
    return ("Big jobs swing these numbers: " + "; ".join(notes) + ".") if notes else ""


# -- loading -------------------------------------------------------------------------------------


def fetch_workiz(client, today: date) -> tuple[list[dict], list[dict]]:
    """(jobs, leads) from a WorkizClient: jobs for about a year (money owed, clean-up, trend), recent leads."""
    jobs = list(client.iter_jobs(today - timedelta(days=HISTORY_DAYS)))
    leads = list(client.iter_leads(today - timedelta(days=RATE_DAYS + 30)))
    return jobs, leads


# -- formatting ----------------------------------------------------------------------------------


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _days(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}"


def change(now: float | None, prior: float | None, kind: str = "pct") -> str:
    """'▲ 12% vs prior' style text. kind 'pts' compares rates in percentage points."""
    if now is None or not prior:
        return "no prior to compare"
    if kind == "pts":
        d = (now - prior) * 100
        return f"{'▲' if d > 0 else '▼' if d < 0 else '■'} {abs(d):.0f} pts vs {_pct(prior)}"
    d = (now - prior) / prior * 100
    return f"{'▲' if d > 0 else '▼' if d < 0 else '■'} {abs(d):.0f}% vs prior"


def tiles(r: dict) -> list[tuple[str, str, str]]:
    """(label, value, comparison) for the five headline numbers."""
    h, rt, pr, p = r["headline"], r["rates"], r["prior_rates"], r["period"]["days"]
    return [
        (f"Revenue · {p} d", _money(h["revenue"]["now"]),
         f"{change(h['revenue']['now'], h['revenue']['prior'])} ({_money(h['revenue']['prior'])})"),
        (f"Jobs booked · {p} d", str(h["booked"]["now"]),
         f"{change(h['booked']['now'], h['booked']['prior'])} ({h['booked']['prior']})"),
        (f"Close rate · {r['rate_days']} d", _pct(rt["close_rate"]), change(rt["close_rate"], pr["close_rate"], "pts")),
        (f"Avg ticket · {r['rate_days']} d", _money(rt["avg_ticket"]),
         f"{change(rt['avg_ticket'], pr['avg_ticket'])} ({_money(pr['avg_ticket'])})"),
        ("Owed to us", _money(r["owed"]["total"]),
         f"{_money(r['owed']['late_total'])} over {OWED_ALERT_DAYS} days · {r['owed']['jobs']} jobs"),
    ]


def location_rows(reports: list[dict]) -> list[list[str]]:
    rows = [["Location", "Revenue (30 d)", "vs prior", "Close rate (90 d)", "Avg ticket", "Owed",
             f"Owed > {OWED_ALERT_DAYS} d", "Jobs to close out"]]
    for r in reports:
        h, rt = r["headline"]["revenue"], r["rates"]
        rows.append([r["market"], _money(h["now"]), change(h["now"], h["prior"]).replace(" vs prior", ""),
                     _pct(rt["close_rate"]), _money(rt["avg_ticket"]), _money(r["owed"]["total"]),
                     _money(r["owed"]["late_total"]), str(len(r["cleanup"]["not_closed"]))])
    return rows


def service_rows(r: dict) -> list[list[str]]:
    rows = [["Service", "Booked", "Close rate", "Revenue", "Avg ticket", "Days to 1st visit", "Days to done"]]
    for s in r["by_service"]:
        rows.append([s["name"], str(s["booked"]), f"{_pct(s['close_rate'])} ({s['sold']}/{s['decided']})",
                     _money(s["revenue"]), _money(s["avg_ticket"]), _days(s["days_to_visit"]), _days(s["days_to_done"])])
    return rows


def source_rows(r: dict) -> list[list[str]]:
    rows = [["Lead source", "Booked", "Close rate", "Revenue", "Avg ticket"]]

    def line(s: dict, name: str) -> list[str]:
        return [name, str(s["booked"]), f"{_pct(s['close_rate'])} ({s['sold']}/{s['decided']})",
                _money(s["revenue"]), _money(s["avg_ticket"])]

    for s in r["by_source"]:
        rows.append(line(s, s["name"]))
        rows += [line(p, "   ↳ " + p["name"].split(" - ", 1)[-1]) for p in s["parts"]]
    return rows


def owed_bucket_rows(r: dict) -> list[list[str]]:
    rows = [["Waiting since completed", "Jobs", "Owed"]]
    rows += [[b["label"], str(b["jobs"]), _money(b["owed"])] for b in r["owed"]["buckets"]]
    return rows


def owed_rows(r: dict) -> list[list[str]]:
    rows = [["Job #", "Service", "Customer", "Completed", "Days waiting", "Owed", "Insurance"]]
    rows += [[str(x["serial"]), x["service"], x["customer"], x["completed"], str(x["days"]), _money(x["owed"]),
              "yes" if x["insurance"] else ""] for x in r["owed"]["late"]]
    return rows


def not_closed_rows(r: dict) -> list[list[str]]:
    rows = [["Job #", "Appt", "Status", "Why", "Service", "Source", "Customer", "Tech"]]
    rows += [[str(x["serial"]), x["appt"], x["status"], x["why"], x["service"], x["source"], x["customer"], x["tech"]]
             for x in r["cleanup"]["not_closed"]]
    return rows


def paid_no_revenue_rows(r: dict) -> list[list[str]]:
    rows = [["Job #", "Appt", "Service", "Customer", "Collected"]]
    rows += [[str(x["serial"]), x["appt"], x["service"], x["customer"], _money(x["collected"])]
             for x in r["cleanup"]["paid_no_revenue"]]
    return rows


def canceled_rows(r: dict) -> list[list[str]]:
    rows = [["Job #", "Appt", "Service", "Customer", "Balance showing"]]
    rows += [[str(x["serial"]), x["appt"], x["service"], x["customer"], _money(x["owed"])]
             for x in r["cleanup"]["canceled_balance"]]
    return rows


def ctm_rows(r: dict) -> list[list[str]]:
    rows = [["Booked in CTM", "Name", "Phone", "Agent", "In Workiz"]]
    rows += [[b["booked"], b["name"], b["phone"], b["agent"], "Lead only" if b["workiz"] == "lead only" else "Not found"]
             for b in r["ctm"]["missing"]]
    return rows


def work_lists(r: dict) -> list[tuple[str, list[list[str]]]]:
    """(heading, rows) for the lists people work through; empty lists are left out."""
    lists = [
        (f"Owed for more than {OWED_ALERT_DAYS} days", owed_rows(r)),
        ("Jobs to close out (still open after the appointment, or Done at $0)", not_closed_rows(r)),
        ("Paid, but revenue never entered", paid_no_revenue_rows(r)),
        ("Canceled, but a balance still shows", canceled_rows(r)),
    ]
    if r["on_ctm"]:
        lists.append((f"Call-center bookings with no Workiz job (last {r['rate_days']} days)", ctm_rows(r)))
    return [(h, rows) for h, rows in lists if len(rows) > 1]


DEFINITIONS = (
    "Revenue = jobs marked Done (or done, waiting for payment) with a total above $0, counted on the day they were "
    "completed. Close rate = of jobs booked in the window, sold ÷ decided (sold, canceled, or appointment passed); "
    "future and in-progress jobs are left out. Owed = open balance on completed jobs, aged from completion. "
    "Days are medians, from the day the job was booked in Workiz. Flood It sources are combined, with each "
    "shown underneath; small sources and services are grouped as Other. Call-center bookings are matched to Workiz by phone number."
)


def render_jobs_markdown(reports: list[dict]) -> str:
    def table(rows: list[list[str]]) -> str:
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
        return "\n".join(out + ["| " + " | ".join(c.replace("|", "/") for c in row) + " |" for row in rows[1:]])

    parts = ["# Jobs & Revenue", ""]
    if len(reports) > 1:
        parts += [table(location_rows(reports)), ""]
    for r in reports:
        parts += [f"## {r['market']}", "", f"As of {r['today']}.", ""]
        parts += [f"- **{label}:** {value} ({cmp})" for label, value, cmp in tiles(r)]
        if r["headline"]["note"]:
            parts += ["", r["headline"]["note"]]
        parts += ["", "### Needs attention", ""] + [f"{i}. {a}" for i, a in enumerate(r["actions"], 1)]
        parts += ["", "### Revenue by month", "", table([["Month", "Revenue", "Jobs"]] + [
            [m["month"] + (" (so far)" if m["partial"] else ""), _money(m["revenue"]), str(m["jobs"])]
            for m in r["monthly"]])]
        parts += ["", f"### By service (last {r['rate_days']} days)", "", table(service_rows(r)),
                  "", f"### By lead source (last {r['rate_days']} days)", "", table(source_rows(r)),
                  "", "### Money owed", "", table(owed_bucket_rows(r))]
        for heading, rows in work_lists(r):
            parts += ["", f"### {heading}", "", table(rows)]
        parts.append("")
    parts += [DEFINITIONS]
    return "\n".join(parts) + "\n"
