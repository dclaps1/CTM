"""Jobs & Revenue: Workiz jobs for one franchise market, matched to CTM contacts by phone number.

    report = build_jobs_report("Greater Boston", jobs, leads, activity, start, today)

Definitions (the same every run):

- A job counts in the window when it was *created* in Workiz on or after `start`.
- Sold: not canceled and the job total is above $0.
- Decided: sold, canceled, or the appointment date has passed. Close rate = sold ÷ decided.
  Future appointments and jobs "In progress" with no amount yet are "pending" and left out.
- Sold amount missing: not canceled or in progress, $0 total, and the appointment date has passed.
- Quote: what the call center booked in CTM (sale value), else the largest dollar amount in that
  contact's call summaries. Quote vs sold uses jobs that have both.
- CTM booking: a contact in this market marked Booked in CTM during the window. It "has a Workiz job"
  when a Workiz job carries the same phone number (last 10 digits).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from app.brief import Activity, _largest_amount, first_name, is_booked_record, phone

UNKNOWN = "(not set)"


def digits10(number: Any) -> str:
    d = re.sub(r"\D", "", str(number or ""))[-10:]
    return d if len(d) == 10 else ""


def amount(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(v: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(v)).date()
    except ValueError:
        return None


def job_phones(job: dict) -> set[str]:
    return {p for p in (digits10(job.get("Phone")), digits10(job.get("SecondPhone"))) if p}


def outcome(job: dict, today: date) -> str:
    """sold / canceled / missing (past date, no amount) / pending (future or in progress, no amount)."""
    status = str(job.get("Status") or "").lower()
    if status.startswith("cancel"):
        return "canceled"
    if amount(job.get("JobTotalPrice")) > 0:
        return "sold"
    if status == "in progress":
        return "pending"
    when = _day(job.get("JobDateTime"))
    return "missing" if when is not None and when < today else "pending"


FLOOD_IT = "Flood It"
TICKET_COLUMNS = 4  # sources shown as their own column in the avg-ticket grid; the rest go under "Other"


def _name(job: dict, key: str) -> str:
    return str(job.get(key) or "").strip() or UNKNOWN


def source_family(job: dict) -> str:
    """Lead source, with every "Flood It - …" source rolled up into one "Flood It"."""
    name = _name(job, "JobSource")
    return FLOOD_IT if name.lower().startswith("flood it") else name


def _group(jobs: list[dict], key, today: date) -> list[dict]:
    """`key`: a Workiz field name, or a function of the job."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for j in jobs:
        groups[key(j) if callable(key) else _name(j, key)].append(j)
    rows = [{"name": name, **_totals(js, today)} for name, js in groups.items()]
    return sorted(rows, key=lambda r: (-r["jobs"], r["name"]))


def _totals(jobs: list[dict], today: date) -> dict:
    outs = [outcome(j, today) for j in jobs]
    sold = [j for j, o in zip(jobs, outs) if o == "sold"]
    decided = sum(o != "pending" for o in outs)
    sold_amount = sum(amount(j.get("JobTotalPrice")) for j in sold)
    due = sum(amount(j.get("JobAmountDue")) for j in sold)
    return {
        "jobs": len(jobs),
        "sold": len(sold),
        "canceled": outs.count("canceled"),
        "missing": outs.count("missing"),
        "pending": outs.count("pending"),
        "decided": decided,
        "close_rate": len(sold) / decided if decided else None,
        "sold_amount": round(sold_amount, 2),
        "collected": round(sold_amount - due, 2),
        "outstanding": round(due, 2),
        "avg_ticket": round(sold_amount / len(sold), 2) if sold else None,
    }


def _ticket_grid(jobs: list[dict], today: date) -> dict:
    """Average sold ticket by service (rows) and lead source (columns)."""
    sold = [j for j in jobs if outcome(j, today) == "sold"]
    by_source = _group(sold, source_family, today)
    by_source.sort(key=lambda g: (-g["sold"], -g["sold_amount"], g["name"]))
    columns = [g["name"] for g in by_source[:TICKET_COLUMNS]]
    if len(by_source) > TICKET_COLUMNS:
        columns.append("Other")

    def column(j: dict) -> str:
        return source_family(j) if source_family(j) in columns else "Other"

    rows = []
    for g in _group(sold, "JobType", today):
        service_jobs = [j for j in sold if _name(j, "JobType") == g["name"]]
        cells = {c["name"]: {"sold": c["sold"], "avg_ticket": c["avg_ticket"]}
                 for c in _group(service_jobs, column, today)}
        rows.append({"service": g["name"], "sold": g["sold"], "avg_ticket": g["avg_ticket"], "cells": cells})
    rows.sort(key=lambda r: (-r["sold"], r["service"]))
    return {"columns": columns, "rows": rows}


def _close_grid(jobs: list[dict], today: date) -> dict:
    """Close rate by service (rows) and lead source (columns), over every job in the window."""
    by_source = _group(jobs, source_family, today)
    by_source.sort(key=lambda g: (-g["decided"], -g["jobs"], g["name"]))
    columns = [g["name"] for g in by_source[:TICKET_COLUMNS]]
    if len(by_source) > TICKET_COLUMNS:
        columns.append("Other")

    def column(j: dict) -> str:
        return source_family(j) if source_family(j) in columns else "Other"

    rows = []
    for g in _group(jobs, "JobType", today):
        service_jobs = [j for j in jobs if _name(j, "JobType") == g["name"]]
        cells = {c["name"]: {"sold": c["sold"], "decided": c["decided"], "close_rate": c["close_rate"]}
                 for c in _group(service_jobs, column, today)}
        rows.append({"service": g["name"], "sold": g["sold"], "decided": g["decided"],
                     "close_rate": g["close_rate"], "cells": cells})
    rows.sort(key=lambda r: (-r["decided"], r["service"]))
    return {"columns": columns, "rows": rows}


def _ctm_index(act: Activity) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = defaultdict(list)
    for contact, records in act.by_contact.items():
        if d := digits10(contact):
            index[d].extend(records)
    return index


def ctm_quote(records: list[dict]) -> float | None:
    values = [amount((r.get("sale") or {}).get("value")) for r in records]
    values = [v for v in values if v > 0]
    if values:
        return values[-1]
    return _largest_amount(" ".join(r.get("summary") or "" for r in records))


def _tech(job: dict) -> str:
    return ", ".join(str(t.get("Name") or "").strip() for t in job.get("Team") or [] if t.get("Name")) or "—"


def _job_row(job: dict) -> dict:
    return {
        "serial": job.get("SerialId"),
        "job_date": str(job.get("JobDateTime") or "")[:10],
        "status": " / ".join(x for x in (job.get("Status"), job.get("SubStatus")) if x),
        "service": str(job.get("JobType") or "").strip() or UNKNOWN,
        "source": str(job.get("JobSource") or "").strip() or UNKNOWN,
        "customer": (str(job.get("FirstName") or "").strip().split() or ["—"])[0].title(),
        "tech": _tech(job),
    }


def build_jobs_report(market: str, jobs: list[dict], leads: list[dict], act: Activity,
                      start: date, today: date) -> dict:
    """`jobs` may reach back before `start` (they are used to match CTM bookings); metrics use jobs created
    from `start` on."""
    window = [j for j in jobs if (_day(j.get("CreatedDate")) or date.min) >= start]
    window.sort(key=lambda j: str(j.get("JobDateTime") or ""))
    ctm = _ctm_index(act)
    matched = [j for j in window if any(p in ctm for p in job_phones(j))]

    missing = [{**_job_row(j), "reason": "Done at $0" if str(j.get("Status") or "").lower().startswith("done")
                else "Not closed out"} for j in window if outcome(j, today) == "missing"]

    quotes = []
    for j in window:
        if outcome(j, today) != "sold":
            continue
        records = [r for p in job_phones(j) for r in ctm.get(p, [])]
        q = ctm_quote(records) if records else None
        if q:
            quotes.append({**_job_row(j), "quote": q, "sold": amount(j.get("JobTotalPrice"))})
    quoted, sold_q = sum(q["quote"] for q in quotes), sum(q["sold"] for q in quotes)

    job_phone_set = {p for j in jobs for p in job_phones(j)}
    lead_phone_set = {p for x in leads for p in job_phones(x)}
    booked = []
    for contact, records in act.by_contact.items():
        d = digits10(contact)
        hits = [r for r in records if is_booked_record(r) and r["_day"] >= start]
        if not d or not hits or act.market(records[-1]) != market:
            continue
        workiz = "job" if d in job_phone_set else "lead only" if d in lead_phone_set else "none"
        first = hits[0]
        booked.append({"booked_day": first["_day"].isoformat(), "name": first_name(first), "phone": phone(contact),
                       "agent": (first.get("agent") or {}).get("name") or "—", "quote": ctm_quote(records),
                       "workiz": workiz})
    booked.sort(key=lambda b: b["booked_day"])
    flood = [j for j in window if source_family(j) == FLOOD_IT]

    return {
        "market": market,
        "start": start.isoformat(),
        "today": today.isoformat(),
        "totals": {**_totals(window, today), "ctm_matched": len(matched),
                   "ctm_match_rate": len(matched) / len(window) if window else None},
        "by_service": _group(window, "JobType", today),
        "by_source": _group(window, source_family, today),
        "flood_it": {
            "totals": _totals(flood, today),
            "by_source": _group(flood, "JobSource", today),
            "by_service": _group(flood, "JobType", today),
        },
        "ticket_grid": _ticket_grid(window, today),
        "close_grid": _close_grid(window, today),
        "missing_amounts": missing,
        "quote_vs_sold": {
            "jobs": len(quotes), "quoted": round(quoted, 2), "sold": round(sold_q, 2),
            "ratio": sold_q / quoted if quoted else None,
            "above": sum(q["sold"] > q["quote"] for q in quotes),
            "below": sum(q["sold"] < q["quote"] for q in quotes),
            "rows": sorted(quotes, key=lambda q: q["sold"] - q["quote"]),
        },
        "ctm_bookings": {
            "total": len(booked),
            "with_job": sum(b["workiz"] == "job" for b in booked),
            "lead_only": sum(b["workiz"] == "lead only" for b in booked),
            "none": sum(b["workiz"] == "none" for b in booked),
            "without_job": [b for b in booked if b["workiz"] != "job"],
        },
    }


# -- loading -------------------------------------------------------------------------------------

JOBS_DAYS = 60  # default report window
MATCH_MARGIN_DAYS = 30  # older jobs still count when matching a CTM booking to a Workiz job


def fetch_workiz(client, start: date) -> tuple[list[dict], list[dict]]:
    """(jobs, leads) for a report window starting at `start`, from a WorkizClient."""
    jobs = list(client.iter_jobs(start - timedelta(days=MATCH_MARGIN_DAYS)))
    leads = list(client.iter_leads(start - timedelta(days=MATCH_MARGIN_DAYS)))
    return jobs, leads


# -- tables (shared by the Markdown output and the dashboard) -------------------------------------


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def market_rows(reports: list[dict]) -> list[list[str]]:
    rows = [["Market", "Jobs", "Sold", "Close rate", "Sold $", "Avg ticket", "Collected", "Outstanding",
             "Amount missing", "Pending", "CTM bookings, no job", "Jobs found in CTM"]]
    for r in reports:
        t, b = r["totals"], r["ctm_bookings"]
        rows.append([r["market"], str(t["jobs"]), str(t["sold"]), f"{_pct(t['close_rate'])} ({t['sold']}/{t['decided']})",
                     _money(t["sold_amount"]), _money(t["avg_ticket"]), _money(t["collected"]), _money(t["outstanding"]),
                     str(t["missing"]), str(t["pending"]), f"{b['total'] - b['with_job']} of {b['total']}",
                     f"{t['ctm_matched']} ({_pct(t['ctm_match_rate'])})"])
    return rows


def group_rows(groups: list[dict], label: str) -> list[list[str]]:
    rows = [[label, "Jobs", "Decided", "Sold", "Close rate", "Sold $", "Avg ticket"]]
    for g in groups:
        rows.append([g["name"], str(g["jobs"]), str(g["decided"]), str(g["sold"]), _pct(g["close_rate"]),
                     _money(g["sold_amount"]), _money(g["avg_ticket"])])
    return rows


def ticket_grid_rows(report: dict) -> list[list[str]]:
    grid = report["ticket_grid"]
    rows = [["Service", "All sources"] + grid["columns"]]
    for r in grid["rows"]:
        cells = [r["cells"].get(c) for c in grid["columns"]]
        rows.append([r["service"], f"{_money(r['avg_ticket'])} ({r['sold']})"]
                    + [f"{_money(c['avg_ticket'])} ({c['sold']})" if c else "—" for c in cells])
    return rows


def close_grid_rows(report: dict) -> list[list[str]]:
    grid = report["close_grid"]

    def cell(c: dict | None) -> str:
        return f"{_pct(c['close_rate'])} ({c['sold']}/{c['decided']})" if c and c["decided"] else "—"

    rows = [["Service", "All sources"] + grid["columns"]]
    for r in grid["rows"]:
        rows.append([r["service"], cell(r)] + [cell(r["cells"].get(c)) for c in grid["columns"]])
    return rows


def flood_it_summary(report: dict) -> str:
    t = report["flood_it"]["totals"]
    if not t["jobs"]:
        return "No Flood It jobs in this window."
    return (f"Flood It: {t['jobs']} jobs, {t['sold']} sold, close rate {_pct(t['close_rate'])} "
            f"({t['sold']}/{t['decided']}), sold {_money(t['sold_amount'])}, avg ticket {_money(t['avg_ticket'])}, "
            f"{t['missing']} with the amount missing, {t['pending']} pending.")


def quote_rows(report: dict) -> list[list[str]]:
    rows = [["Job #", "Appt", "Service", "Customer", "Quote (CTM)", "Sold (Workiz)", "Difference"]]
    for q in report["quote_vs_sold"]["rows"]:
        diff = q["sold"] - q["quote"]
        rows.append([str(q["serial"]), q["job_date"], q["service"], q["customer"], _money(q["quote"]),
                     _money(q["sold"]), ("+" if diff > 0 else "−" if diff < 0 else "") + _money(abs(diff))])
    return rows


def missing_rows(report: dict) -> list[list[str]]:
    rows = [["Job #", "Appt", "Status", "Service", "Source", "Customer", "Tech", "Why"]]
    for m in report["missing_amounts"]:
        rows.append([str(m["serial"]), m["job_date"], m["status"], m["service"], m["source"], m["customer"],
                     m["tech"], m["reason"]])
    return rows


def booking_rows(report: dict) -> list[list[str]]:
    rows = [["Booked in CTM", "Name", "Phone", "Agent", "Quote", "In Workiz"]]
    for b in report["ctm_bookings"]["without_job"]:
        rows.append([b["booked_day"], b["name"], b["phone"], b["agent"], _money(b["quote"]),
                     "Lead only, no job" if b["workiz"] == "lead only" else "Not found"])
    return rows


def quote_summary(report: dict) -> str:
    q = report["quote_vs_sold"]
    if not q["jobs"]:
        return "No sold job has a CTM quote to compare yet."
    return (f"{q['jobs']} sold jobs have a call-center quote: quoted {_money(q['quoted'])}, sold {_money(q['sold'])} "
            f"({_pct(q['ratio'])} of quote). Sold above quote: {q['above']} · below: {q['below']}.")


def render_jobs_markdown(reports: list[dict]) -> str:
    def table(rows: list[list[str]]) -> str:
        if len(rows) == 1:
            return "_None._"
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
        out += ["| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in rows[1:]]
        return "\n".join(out)

    parts = ["# Jobs & Revenue (Workiz)", ""]
    if reports:
        parts += [f"Jobs created {reports[0]['start']} to {reports[0]['today']}.", "", table(market_rows(reports))]
    for r in reports:
        parts += ["", f"## {r['market']}", "", "### Close rate by service", "", table(group_rows(r["by_service"], "Service")),
                  "", "### Close rate by service and source", "", CLOSE_NOTE, "", table(close_grid_rows(r)),
                  "", "### Close rate by lead source", "", table(group_rows(r["by_source"], "Source")),
                  "", "### Flood It", "", flood_it_summary(r), "",
                  table(group_rows(r["flood_it"]["by_source"], "Flood It source")), "",
                  table(group_rows(r["flood_it"]["by_service"], "Service")),
                  "", "### Average ticket by service and source", "", TICKET_NOTE, "", table(ticket_grid_rows(r)),
                  "", "### Quote vs sold", "", quote_summary(r), "", table(quote_rows(r)),
                  "", "### Sold amount missing", "", table(missing_rows(r)),
                  "", "### CTM bookings with no Workiz job", "", table(booking_rows(r))]
    parts += ["", DEFINITIONS]
    return "\n".join(parts) + "\n"


CLOSE_NOTE = "Close rate, with sold ÷ decided jobs in brackets. Flood It sources are combined."
TICKET_NOTE = "Average sold ticket, with the number of sold jobs in brackets. Flood It sources are combined."

DEFINITIONS = (
    "Sold = not canceled and job total above $0. Close rate = sold ÷ decided (sold, canceled, or appointment "
    "date passed); future appointments and in-progress jobs without an amount are pending and left out. "
    "Amount missing = not canceled or in progress, $0, appointment passed. Quote = the call center's booked amount in CTM, else the largest $ in the call summaries. "
    "Matching is by phone number (last 10 digits)."
)
