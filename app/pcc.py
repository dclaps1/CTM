"""Call-center follow-through: what happened to every job the call center (PCC) booked.

    report = build_pcc_report(activity, {"Charleston": (jobs, leads), ...}, today, days=90)

The call center does not get every call for its markets, so this looks only at contacts it booked. Each booking is
followed into that market's Workiz account to see whether the job got there, and how it ended.

Definitions (the same every run):

- Booking: a CTM contact in one of the 10 call-center markets with a booked record (lead status Booked, a "booked"
  tag, or a sale marked as a conversion) in the window. CTM keeps the lead status on the contact, so it also shows on
  the web form or missed call that came before the booking; the booking is the first booked record with an agent on
  it (else the first booked record), and its day, agent and quote come from there. The quote falls back to the
  contact's latest sale value. Bookings with no agent anywhere are shown as "No agent".
- Matched job: a Workiz job in that market for the same customer, found by phone, else by email, else by first and
  last name (the report says which). It must be created from EARLY_DAYS before to MATCH_DAYS after the booking; one
  created more than a day before the booking counts only if its appointment is on or after the booking day, so an
  old finished job does not match. The earliest match by the best key wins.
- Outcome of the matched job: Sold (Done with a total above $0), Done at $0, Canceled, Not closed out (appointment
  passed, still open), Scheduled (appointment ahead, or in progress).
- With no job, a Workiz lead for the same customer (same keys, any date) decides the outcome by its status:
  Scheduled lead, no job (scheduled or rescheduled); Lead lost or canceled; Open lead, not worked (anything else).
  With no lead either: Not in Workiz.
- Reached Workiz = bookings with a matched job ÷ bookings. Close rate = Sold ÷ (Sold + Done at $0 + Canceled + Not
  closed out); scheduled jobs are left out. Sold vs quote compares sold totals with PCC's quote on jobs that have both.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from datetime import date, timedelta

from app.brief import _STATE_SUFFIX, MARKETS, Activity, first_name, is_booked_record, phone
from app.jobs import _day, _money, _name, _pct, _status, amount, digits10, job_phones, outcome

PCC_DAYS = 90
MATCH_DAYS = 14
EARLY_DAYS = 30
NO_AGENT = "No agent"
SCHEDULED_LEAD, OPEN_LEAD, LOST_LEAD, NOT_IN_WORKIZ = (
    "Scheduled lead, no job", "Open lead, not worked", "Lead lost or canceled", "Not in Workiz")
OUTCOMES = ["Sold", "Done at $0", "Canceled", "Not closed out", "Scheduled",
            SCHEDULED_LEAD, OPEN_LEAD, LOST_LEAD, NOT_IN_WORKIZ]
DECIDED = ("Sold", "Done at $0", "Canceled", "Not closed out")
NO_JOB = (SCHEDULED_LEAD, OPEN_LEAD, LOST_LEAD, NOT_IN_WORKIZ)


def _quote(records: list[dict], first: dict) -> float | None:
    for r in [first] + records[::-1]:
        value = amount((r.get("sale") or {}).get("value"))
        if value > 0:
            return round(value, 2)
    return None


def _letters(v) -> str:
    return re.sub(r"[^a-z]", "", str(v or "").lower())


def _full_name(records: list[dict]) -> tuple[str, str] | None:
    """(first, last) from the latest real name on the contact; caller IDs like "MACON GA" are skipped."""
    for r in records[::-1]:
        name = str(r.get("name") or (r.get("form") or {}).get("name") or "").strip()
        if name.isupper() and _STATE_SUFFIX.search(name):
            continue
        words = name.split()
        if len(words) >= 2 and len(_letters(words[0])) >= 2 and len(_letters(words[-1])) >= 2:
            return _letters(words[0]), _letters(words[-1])
    return None


def _emails(records: list[dict]) -> set[str]:
    out = set()
    for r in records:
        for e in (r.get("email"), (r.get("form") or {}).get("email")):
            if e and "@" in str(e):
                out.add(str(e).strip().lower())
    return out


def pcc_bookings(act: Activity, start: date, end: date) -> list[dict]:
    """One row per contact the call center booked between start and end, in a call-center market."""
    markets = {name for name, _ in MARKETS}
    out = []
    for contact, records in act.by_contact.items():
        number = digits10(contact)
        hits = [r for r in records if is_booked_record(r) and start <= r["_day"] <= end]
        market = act.market(records[-1])
        if not number or not hits or market not in markets:
            continue
        first = next((r for r in hits if (r.get("agent") or {}).get("name")), hits[0])
        out.append({"phone10": number, "phone": phone(contact), "booked": first["_day"], "market": market,
                    "name": first_name(first), "agent": (first.get("agent") or {}).get("name") or NO_AGENT,
                    "quote": _quote(records, first), "emails": sorted(_emails(records)),
                    "full_name": _full_name(records)})
    return sorted(out, key=lambda b: (b["booked"], b["market"]))


def _job_outcome(job: dict, today: date) -> str:
    o = outcome(job, today)
    if o == "missing":
        return "Done at $0" if _status(job).startswith("done") else "Not closed out"
    return {"sold": "Sold", "canceled": "Canceled", "pending": "Scheduled"}[o]


def _match_key(b: dict, x: dict) -> str | None:
    """How a Workiz job or lead matches the booking's customer: phone, email or name (None if it doesn't)."""
    if b["phone10"] in job_phones(x):
        return "phone"
    if b["emails"] and str(x.get("Email") or "").strip().lower() in b["emails"]:
        return "email"
    if b["full_name"] and (_letters(x.get("FirstName")), _letters(x.get("LastName"))) == tuple(b["full_name"]):
        return "name"
    return None


def _lead_outcome(lead: dict) -> str:
    status = _status(lead)
    if status in ("scheduled", "rescheduled"):
        return SCHEDULED_LEAD
    if "lost" in status or "cancel" in status:
        return LOST_LEAD
    return OPEN_LEAD


def _in_window(b: dict, job: dict) -> bool:
    created = _day(job.get("CreatedDate"))
    if created is None or not b["booked"] - timedelta(days=EARLY_DAYS) <= created <= b["booked"] + timedelta(days=MATCH_DAYS):
        return False
    return created >= b["booked"] - timedelta(days=1) or (_day(job.get("JobDateTime")) or date.min) >= b["booked"]


def match_booking(b: dict, jobs: list[dict], leads: list[dict], today: date) -> dict:
    rank = {"phone": 0, "email": 1, "name": 2}
    hits = [(rank[k], str(j.get("CreatedDate")), k, j) for j in jobs if _in_window(b, j) and (k := _match_key(b, j))]
    if not hits:
        found = sorted((rank[k], str(x.get("CreatedDate")), k, x) for x in leads if (k := _match_key(b, x)))
        if not found:
            return {**b, "outcome": NOT_IN_WORKIZ, "matched_by": None, "job": None, "lead": None}
        _, _, key, lead = max((f for f in found if f[0] == found[0][0]), key=lambda f: f[1])  # best key, latest lead
        return {**b, "outcome": _lead_outcome(lead), "matched_by": key, "job": None,
                "lead": {"serial": lead.get("SerialId"), "status": str(lead.get("Status") or "—"),
                         "created_by": str(lead.get("CreatedBy") or "—"),
                         "when": str(lead.get("LeadDateTime") or "")[:10]}}
    _, _, key, j = min(hits, key=lambda h: (h[0], h[1]))
    return {**b, "outcome": _job_outcome(j, today), "matched_by": key, "lead": None,
            "job": {"serial": j.get("SerialId"), "status": " / ".join(x for x in (j.get("Status"), j.get("SubStatus")) if x),
                    "appt": str(j.get("JobDateTime") or "")[:10], "service": _name(j, "JobType"),
                    "created_by": str(j.get("CreatedBy") or "—"), "total": amount(j.get("JobTotalPrice")),
                    "collected": round(max(amount(j.get("JobTotalPrice")) - amount(j.get("JobAmountDue")), 0), 2)}}


def summarize(rows: list[dict]) -> dict:
    counts = Counter(r["outcome"] for r in rows)
    n = len(rows)
    reached = n - sum(counts[o] for o in NO_JOB)
    decided = sum(counts[o] for o in DECIDED)
    pairs = [(r["quote"], r["job"]["total"]) for r in rows if r["outcome"] == "Sold" and r["quote"]]
    quoted, sold = sum(q for q, _ in pairs), sum(s for _, s in pairs)
    return {
        "bookings": n,
        "outcomes": {o: counts[o] for o in OUTCOMES},
        "reached_workiz": reached,
        "reach_rate": reached / n if n else None,
        "close_rate": counts["Sold"] / decided if decided else None,
        "sold_revenue": round(sum(r["job"]["total"] for r in rows if r["outcome"] == "Sold"), 2),
        "quote_pairs": len(pairs),
        "sold_vs_quote": sold / quoted if quoted else None,
        "matched_by": dict(Counter(r["matched_by"] for r in rows if r["job"])),
    }


def build_pcc_report(act: Activity, workiz: dict[str, tuple[list[dict], list[dict]]], today: date,
                     days: int = PCC_DAYS) -> dict:
    """`workiz` maps each call-center market to (jobs, leads); markets missing from it are listed as not read."""
    start = today - timedelta(days=days - 1)
    rows = [match_booking(b, *workiz[b["market"]], today) for b in pcc_bookings(act, start, today)
            if b["market"] in workiz]
    markets = [name for name, _ in MARKETS]

    def group(key, names):
        return [{"name": n, **summarize([r for r in rows if key(r) == n])} for n in names if any(key(r) == n for r in rows)]

    agents = sorted({r["agent"] for r in rows}, key=lambda a: (a == NO_AGENT, a))
    return {
        "today": today.isoformat(), "days": days, "start": start.isoformat(),
        "total": summarize(rows),
        "by_market": sorted(group(lambda r: r["market"], markets), key=lambda g: -g["bookings"]),
        "by_agent": group(lambda r: r["agent"], agents),
        "not_read": [m for m in markets if m not in workiz],
        "rows": rows,
    }


# -- rendering -----------------------------------------------------------------------------------

DEFINITIONS = (
    "Only bookings the call center made are counted; the call center does not get every call for its markets. "
    "A booking is matched to a Workiz job in its market by phone, else email, else first and last name, created from "
    f"{EARLY_DAYS} days before (if the appointment is not before the booking) to {MATCH_DAYS} days after the booking. "
    "With no job, the customer's Workiz lead status decides the outcome. Close rate = Sold ÷ (Sold + Done at $0 + Canceled + Not closed out); scheduled jobs are left out. "
    "Sold vs quote compares the sold total with the call center's quote on jobs that have both."
)


def summary_rows(groups: list[dict], label: str, total: dict | None = None) -> list[list[str]]:
    head = [label, "Bookings", "Reached Workiz", "Sold", "Done at $0", "Canceled", "Not closed out", "Scheduled",
            "Scheduled lead, no job", "Open lead", "Lead lost", "Not in Workiz", "Close rate", "Sold revenue",
            "Sold vs quote"]
    body = [{"name": g["name"], **g} for g in groups] + ([{"name": "Total", **total}] if total else [])
    return [head] + [[
        g["name"], str(g["bookings"]), f"{g['reached_workiz']} ({_pct(g['reach_rate'])})",
        str(g["outcomes"]["Sold"]), str(g["outcomes"]["Done at $0"]), str(g["outcomes"]["Canceled"]),
        str(g["outcomes"]["Not closed out"]), str(g["outcomes"]["Scheduled"]),
        str(g["outcomes"][SCHEDULED_LEAD]), str(g["outcomes"][OPEN_LEAD]), str(g["outcomes"][LOST_LEAD]),
        str(g["outcomes"][NOT_IN_WORKIZ]), _pct(g["close_rate"]),
        _money(g["sold_revenue"]), _pct(g["sold_vs_quote"]) if g["quote_pairs"] >= 3 else "—",
    ] for g in body]


def work_rows(r: dict, outcomes: tuple[str, ...]) -> list[list[str]]:
    rows = [x for x in r["rows"] if x["outcome"] in outcomes]
    rows.sort(key=lambda x: (x["market"], x["booked"]))
    def workiz(x: dict) -> str:
        how = f" (matched by {x['matched_by']})" if x["matched_by"] not in (None, "phone") else ""
        if x["job"]:
            return f"Job #{x['job']['serial']} {x['job']['status']}, appt {x['job']['appt']}{how}"
        if x["lead"]:
            when = f", {x['lead']['when']}" if x["lead"]["when"] else ""
            return f"Lead #{x['lead']['serial']} {x['lead']['status']}{when}, by {x['lead']['created_by']}{how}"
        return "Nothing under this phone, email or name"

    return [["Booked", "Market", "Customer", "Phone", "Agent", "Quote", "Workiz"]] + [[
        x["booked"].isoformat(), x["market"], x["name"], x["phone"], x["agent"], _money(x["quote"]), workiz(x),
    ] for x in rows]


def work_lists(r: dict) -> list[tuple[str, list[list[str]]]]:
    lists = [
        ("Scheduled lead, never made a job (likeliest lost jobs)", work_rows(r, (SCHEDULED_LEAD,))),
        ("Open lead, not worked", work_rows(r, (OPEN_LEAD,))),
        ("Booked, but nothing in Workiz", work_rows(r, (NOT_IN_WORKIZ,))),
        ("Appointment passed, job still open in Workiz", work_rows(r, ("Not closed out",))),
        ("Marked Done at $0 in Workiz", work_rows(r, ("Done at $0",))),
    ]
    return [(h, rows) for h, rows in lists if len(rows) > 1]


def headline(r: dict) -> list[tuple[str, str, str]]:
    t = r["total"]
    return [
        ("Call-center bookings", str(t["bookings"]), f"last {r['days']} days"),
        ("Reached Workiz", _pct(t["reach_rate"]), f"{t['reached_workiz']} of {t['bookings']}"),
        ("Close rate", _pct(t["close_rate"]), f"{t['outcomes']['Sold']} sold"),
        ("Sold revenue", _money(t["sold_revenue"]), "from call-center bookings"),
    ]


def render_pcc_markdown(r: dict) -> str:
    def table(rows: list[list[str]]) -> str:
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
        return "\n".join(out + ["| " + " | ".join(c.replace("|", "/") for c in row) + " |" for row in rows[1:]])

    parts = ["# Call-center follow-through", "", f"Bookings from {r['start']} to {r['today']}.", ""]
    parts += [f"- **{label}:** {value} ({note})" for label, value, note in headline(r)]
    if r["not_read"]:
        parts += ["", "Workiz not read for: " + ", ".join(r["not_read"]) + "."]
    parts += ["", "## By market", "", table(summary_rows(r["by_market"], "Market", r["total"])),
              "", "## By agent", "", table(summary_rows(r["by_agent"], "Agent"))]
    for heading, rows in work_lists(r):
        parts += ["", f"## {heading}", "", table(rows)]
    return "\n".join(parts + ["", DEFINITIONS]) + "\n"


_CSS = """
@page{size:letter landscape;margin:0.45in}
:root{--ink:#1d2433;--mute:#5d6678;--line:#dde2ea;--accent:#1f5fa8}
body{font:9pt/1.4 "Helvetica Neue",Arial,sans-serif;color:var(--ink);background:#fff;margin:0}
h1{font-size:17pt;margin:0 0 2px}h2{font-size:11pt;margin:16px 0 6px;padding-bottom:3px;border-bottom:2px solid var(--ink)}
.sub{color:var(--mute);margin:0 0 10px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:8px 0}
.tile{border:1px solid var(--line);border-radius:6px;padding:7px 10px}
.tile .k{font-size:7.5pt;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.tile .v{font-size:15pt;font-weight:700}.tile .n{font-size:8pt;color:var(--mute)}
table{width:100%;border-collapse:collapse;break-inside:auto}
th{text-align:left;font-size:7pt;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);border-bottom:1px solid var(--line);padding:4px 5px}
td{padding:4px 5px;border-bottom:1px solid var(--line)}tr:last-child td{font-weight:600}
table.list tr:last-child td{font-weight:400}
.foot{margin-top:12px;font-size:7.5pt;color:var(--mute)}
"""


def render_pcc_html(r: dict) -> str:
    e = html.escape

    def table(rows: list[list[str]], cls: str = "") -> str:
        head = "".join(f"<th>{e(c)}</th>" for c in rows[0])
        body = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in rows[1:])
        return f'<table class="{cls}"><tr>{head}</tr>{body}</table>'

    tiles = "".join(f'<div class="tile"><div class="k">{e(k)}</div><div class="v">{e(v)}</div>'
                    f'<div class="n">{e(n)}</div></div>' for k, v, n in headline(r))
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Call-Center Follow-Through</title>"
             f"<style>{_CSS}</style></head><body>",
             "<h1>Call-center follow-through</h1>",
             f'<p class="sub">What happened to every job the call center booked, {e(r["start"])} to {e(r["today"])}</p>',
             f'<div class="tiles">{tiles}</div>']
    if r["not_read"]:
        parts.append(f'<p class="sub">Workiz not read for: {e(", ".join(r["not_read"]))}.</p>')
    parts += ["<h2>By market</h2>", table(summary_rows(r["by_market"], "Market", r["total"])),
              "<h2>By agent</h2>", table(summary_rows(r["by_agent"], "Agent"), "list")]
    for heading, rows in work_lists(r):
        parts += [f"<h2>{e(heading)}</h2>", table(rows, "list")]
    parts += [f'<p class="foot">{e(DEFINITIONS)}</p></body></html>']
    return "\n".join(parts)
