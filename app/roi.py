"""Call-center ROI statement: what the call center did for each market in a month, against its monthly fee.

    report = build_roi_report(activity, {"Charleston": (jobs, leads), ...}, date(2026, 9, 1), today, fee=1500)

Definitions (the same every run):

- Leads: people whose first call, form, chat or text in the month reached the market (out-of-area and unassigned
  left out). Spoke with: had a conversation with an agent. Opportunities and bookings follow the Daily Brief.
- Calls answered: queue calls in the month answered live by an agent.
- Bookings: contacts the call center booked in the month (app.pcc). Each is followed into Workiz:
  sold (a sold job, or an open job with a payment) and its revenue; still ahead (a job or a lead with the
  appointment still to come), valued at the call center's quote; lost (canceled, lost, Done at $0, not sold).
- Revenue delivered = sold revenue from the month's bookings so far. Pipeline = quotes on bookings still ahead.
- Return = revenue delivered ÷ fee. Gross-profit return uses an assumed gross margin (default 60%).
"""
from __future__ import annotations

import html
from collections import Counter
from datetime import date, timedelta

from app.brief import MARKETS, OUT_OF_AREA, UNASSIGNED, Activity, is_queued_inbound, lead_outcome
from app.jobs import _money, _pct
from app.outcomes import PAID, by_client
from app.pcc import SCHEDULED_LEAD, match_booking, pcc_bookings

FEE = 1500.0
MARGIN = 0.60


def month_end(start: date) -> date:
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _market_funnel(act: Activity, start: date, end: date) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for _, first in act.first_touch.items():
        if not start <= first["_day"] <= end:
            continue
        o = lead_outcome(act, first)
        if o["market"] in (OUT_OF_AREA, UNASSIGNED):
            continue
        c = out.setdefault(o["market"], Counter())
        c["leads"] += 1
        c["spoke"] += o["spoke"]
        c["opps"] += o["opportunity"]
    for r in act.records:
        if start <= r["_day"] <= end and is_queued_inbound(r):
            c = out.setdefault(act.market(r), Counter())
            c["queued"] += 1
            c["answered"] += r.get("dial_status") == "answered"
    return out


def build_roi_report(act: Activity, workiz: dict[str, tuple[list[dict], list[dict]]], start: date, today: date,
                     fee: float = FEE, margin: float = MARGIN) -> dict:
    end = min(month_end(start), today)
    funnel = _market_funnel(act, start, end)
    clients = {m: by_client(jobs) for m, (jobs, _) in workiz.items()}
    rows = {m: Counter() for m, _ in MARKETS}
    for b in pcc_bookings(act, start, end):
        if b["market"] not in workiz:
            continue
        r = match_booking(b, *workiz[b["market"]], today, clients[b["market"]])
        c = rows[b["market"]]
        c["booked"] += 1
        job = r["job"]
        if r["outcome"] == "Sold" or (r["outcome"] == "Not closed out" and r.get("evidence") == PAID):
            c["sold"] += 1
            c["revenue"] += job["total"] or job["collected"]
        elif r["outcome"] == "Scheduled" or r["outcome"] == SCHEDULED_LEAD:
            c["ahead"] += 1
            c["pipeline"] += r["quote"] or 0
        elif r["outcome"] in ("Canceled", "Done at $0", "Lead lost or canceled") or r.get("evidence") == "not_sold":
            c["lost"] += 1
        else:
            c["open"] += 1  # not in Workiz, open lead, or open job with no evidence yet
    markets = []
    for name, _ in MARKETS:
        f, c = funnel.get(name, Counter()), rows[name]
        if name not in workiz and not f:
            continue
        markets.append({
            "market": name, "read": name in workiz,
            "leads": f["leads"], "spoke": f["spoke"], "opps": f["opps"],
            "calls": f["queued"], "answered": f["answered"], "answer_rate": f["answered"] / f["queued"] if f["queued"] else None,
            "booked": c["booked"], "sold": c["sold"], "ahead": c["ahead"], "lost": c["lost"], "open": c["open"],
            "revenue": round(c["revenue"], 2), "pipeline": round(c["pipeline"], 2),
            "return": c["revenue"] / fee if fee else None,
            "gross_return": c["revenue"] * margin / fee if fee else None,
            "book_rate": c["booked"] / f["opps"] if f["opps"] else None,
        })
    markets.sort(key=lambda m: -m["revenue"])
    total = Counter()
    for m in markets:
        total.update({k: m[k] for k in ("leads", "spoke", "opps", "calls", "answered", "booked", "sold", "ahead", "lost",
                                         "open", "revenue", "pipeline")})
    return {"start": start.isoformat(), "end": end.isoformat(), "today": today.isoformat(), "fee": fee, "margin": margin,
            "partial": end < month_end(start), "markets": markets, "total": dict(total),
            "clears_fee": [m["market"] for m in markets if m["revenue"] * margin >= fee]}


# -- rendering -----------------------------------------------------------------------------------

def _x(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}×"


def summary_rows(r: dict) -> list[list[str]]:
    head = ["Market", "Leads", "Calls answered", "Booked", "Sold so far", "Still ahead", "Revenue delivered",
            "Pipeline (quotes)", "Return on fee", "Gross-profit return"]
    return [head] + [[
        m["market"], str(m["leads"]), _pct(m["answer_rate"]), str(m["booked"]), str(m["sold"]), str(m["ahead"]),
        _money(m["revenue"]), _money(m["pipeline"]), _x(m["return"]), _x(m["gross_return"]),
    ] for m in r["markets"]]


DEFINITIONS = (
    "Revenue delivered = sold revenue so far from jobs the call center booked in the month (a sold job, or an open "
    "job with a payment). Still ahead = bookings whose appointment hasn't happened yet; pipeline values them at the "
    "call center's quote. Return on fee = revenue delivered ÷ fee. Gross-profit return applies the assumed margin. "
    "Only contacts the call center handled are counted; owners and lead services also book work directly."
)


def render_roi_markdown(r: dict) -> str:
    rows = summary_rows(r)
    table = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])] + \
            ["| " + " | ".join(row) + " |" for row in rows[1:]]
    t = r["total"]
    parts = [f"# Call-center ROI, {r['start']} to {r['end']}" + (" (month in progress)" if r["partial"] else ""), "",
             f"Fee {_money(r['fee'])} a month per market; assumed gross margin {_pct(r['margin'])}.", "",
             f"- **Bookings:** {t.get('booked', 0)} · sold so far {t.get('sold', 0)} · still ahead {t.get('ahead', 0)}",
             f"- **Revenue delivered:** {_money(t.get('revenue', 0))} · pipeline {_money(t.get('pipeline', 0))}",
             f"- **Markets clearing the fee on gross profit:** {len(r['clears_fee'])} of {len(r['markets'])}"
             + (f" ({', '.join(r['clears_fee'])})" if r["clears_fee"] else ""), "", *table, "", DEFINITIONS]
    return "\n".join(parts) + "\n"


_CSS = """
@page{size:letter;margin:0.5in 0.55in}
:root{--ink:#17232b;--mute:#566673;--line:#dbe3e8;--accent:#0f6e8c;--good:#2c7a4b;--goodbg:#e5f3ea;--bad:#a4551a;--badbg:#fbeee3}
body{font:9.5pt/1.45 "Helvetica Neue",Arial,sans-serif;color:var(--ink);background:#fff;margin:0}
h1{font-size:18pt;margin:2px 0}h2{font-size:12pt;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 10px}
.eyebrow{font-size:8pt;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:700}
table{width:100%;border-collapse:collapse;font-size:8.6pt;margin:6px 0 4px}
th{text-align:left;font-size:7pt;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);border-bottom:1px solid var(--line);padding:4px 5px}
td{padding:4px 5px;border-bottom:1px solid var(--line)}td:first-child{white-space:nowrap}td.r,th.r{text-align:right;white-space:nowrap}
.stmt{border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:10px 0;break-inside:avoid}
.stmt .big{font-size:20pt;font-weight:700}.stmt .big.ok{color:var(--good)}.stmt .big.no{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:6px}.grid .k{font-size:7pt;color:var(--mute);text-transform:uppercase}
.grid .v{font-size:12pt;font-weight:600}
.foot{font-size:7.5pt;color:var(--mute);margin-top:10px}
.pb{break-before:page}
"""


def render_roi_html(r: dict) -> str:
    e = html.escape
    rows = summary_rows(r)
    head = "".join(f'<th class="{"r" if i else ""}">{e(c)}</th>' for i, c in enumerate(rows[0]))
    body = "".join("<tr>" + "".join(f'<td class="{"r" if i else ""}">{e(c)}</td>' for i, c in enumerate(row)) + "</tr>"
                   for row in rows[1:])
    t = r["total"]
    stmts = []
    for m in r["markets"]:
        ok = m["gross_return"] is not None and m["gross_return"] >= 1
        cells = [("Leads", m["leads"]), ("Calls answered", _pct(m["answer_rate"])), ("Booked", m["booked"]),
                 ("Sold so far", m["sold"]), ("Still ahead", f'{m["ahead"]} · {_money(m["pipeline"])}')]
        grid = "".join(f'<div><div class="k">{e(k)}</div><div class="v">{e(str(v))}</div></div>' for k, v in cells)
        stmts.append(f'<div class="stmt"><h2>{e(m["market"])}</h2><div class="big {"ok" if ok else "no"}">'
                     f'{_money(m["revenue"])} <span style="font-size:11pt;color:var(--mute)">delivered · '
                     f'{_x(m["return"])} the fee · {_x(m["gross_return"])} on gross profit</span></div>'
                     f'<div class="grid">{grid}</div></div>')
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Call-Center ROI</title><style>{_CSS}</style>"
            f"</head><body><div class='eyebrow'>Voda · Playbook Call Center · ROI statement</div>"
            f"<h1>What the call center delivered, {e(r['start'])} to {e(r['end'])}</h1>"
            f"<p class='sub'>Fee {_money(r['fee'])} a month per market · assumed gross margin {_pct(r['margin'])}"
            f"{' · month in progress' if r['partial'] else ''} · {len(r['clears_fee'])} of {len(r['markets'])} markets "
            f"clear the fee on gross profit · {t.get('booked', 0)} bookings, {_money(t.get('revenue', 0))} delivered, "
            f"{_money(t.get('pipeline', 0))} still ahead</p>"
            f"<table><tr>{head}</tr>{body}</table><p class='foot'>{e(DEFINITIONS)}</p>"
            f"<div class='pb'></div>{''.join(stmts)}</body></html>")
