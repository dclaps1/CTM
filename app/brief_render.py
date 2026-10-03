"""Render a brief (from app.brief.build_brief) as email-safe HTML or Markdown.

The HTML uses plain tables and no CSS so it survives Outlook and Gmail.
"""
from __future__ import annotations

from datetime import date
from html import escape

DOT = {"green": "🟢", "yellow": "🟡", "red": "🔴", "none": "⚪"}

SCORECARD = [  # (status key, label, number field, formatter)
    ("answered_live", "Calls answered live", "answered_rate", "pct"),
    ("web_leads_5min", "Web leads called in 5 min", "web_leads_5min_rate", "pct"),
    ("opportunity_conversion", "Opportunity conversion", "opportunity_conversion", "pct"),
    ("jobs_booked", "Jobs booked", "jobs_booked", "int"),
]


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def mmss(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _fmt(v, kind: str) -> str:
    return pct(v) if kind == "pct" else ("—" if v is None else f"{v:g}" if isinstance(v, float) else str(v))


def _target(key: str, targets: dict) -> str:
    t = targets[key]
    return str(t) if key == "jobs_booked" else pct(t)


def _label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.strftime('%a')} {d.month}/{d.day}"


def title(brief: dict) -> str:
    d = date.fromisoformat(brief["day"])
    return f"Daily Call Center Brief — {d.strftime('%A, %b')} {d.day}"


def status_line(brief: dict) -> str:
    counts = {"red": 0, "yellow": 0, "green": 0}
    for s in brief["status"].values():
        if s["status"] in counts:
            counts[s["status"]] += 1
    return f"🔴 {counts['red']} off target · 🟡 {counts['yellow']} watch · 🟢 {counts['green']} on target"


def _rows(brief: dict) -> dict[str, list[list[str]]]:
    """All tables as plain rows of strings, shared by both renderers."""
    n, st, tg = brief["numbers"], brief["status"], brief["targets"]
    day, prior, seven = n["day"], n["prior"], n["seven_day"]
    dl, pl = _label(day["day"]), _label(prior["day"])
    score = [["Metric", dl, pl, "7-day avg", "Target", ""]]
    for key, label, field, kind in SCORECARD:
        avg7 = seven[field] / 7 if kind == "int" else seven[field]
        score.append([label, _fmt(day[field], kind), _fmt(prior[field], kind),
                      f"{avg7:.0f}/day" if kind == "int" else _fmt(avg7, kind), _target(key, tg),
                      DOT[st[key]["status"]]])
    q = brief["follow_through"]["quotes_followed_up"]
    score.append(["Open quotes followed up in 24h", f"{q['done']} of {q['of']}" if q["of"] else "—", "", "",
                  pct(tg["quotes_followed_up"]), DOT[st["quotes_followed_up"]["status"]]])

    def cb_cell(x):
        return f"{x['callback_60s']} ({pct(x['callback_60s_rate'])})"

    callback = [["", dl, pl, "7 days", "Target"],
                ["Missed calls", *(f"{x['missed']} of {x['queued']}" for x in (day, prior, seven)), "—"],
                ["Called back within 60 sec", *(cb_cell(x) for x in (day, prior, seven)), pct(tg["callback_60s"])],
                ["Callback connected", *(f"{x['callback_connected']} of {x['callback_60s']}" for x in (day, prior, seven)), "—"],
                ["Median time to callback", *(mmss(x["median_callback_s"]) for x in (day, prior, seven)), "under 1:00"]]
    handled = [["", dl, pl, "7 days", "Target"],
               ["Answered live", *(f"{x['answered']} ({pct(x['answered_rate'])})" for x in (day, prior, seven)), pct(tg["answered_live"])],
               ["+ Called back within 60 sec", *(f"{x['callback_60s']} ({pct(x['callback_60s'] / x['queued'] if x['queued'] else None)})" for x in (day, prior, seven)), "—"],
               ["= Calls handled", *(f"{x['handled']} of {x['queued']} ({pct(x['handled_rate'])})" for x in (day, prior, seven)), pct(tg["calls_handled"])],
               ["Not handled", *(str(x["queued"] - x["handled"]) for x in (day, prior, seven)), "—"]]
    agents = [["Agent", f"Booked {dl}", f"Opp. conv. {dl}", "Opp. conv. 7 days", "Two-times close (7 days)"]]
    for a in brief["agents"]:
        agents.append([a["agent"], f"{a['booked']} of {a['opps']}", pct(a["conversion"]),
                       f"{pct(a['week_conversion'])} ({a['week_booked']}/{a['week_opps']})", pct(a["two_times_rate"])])
    markets = [["Market", "Leads", "Opportunities", f"Booked ({dl})", "Opp. conv.", "Answered live"]]
    for m in brief["markets"]:
        markets.append([m["market"], str(m["leads"]), str(m["opps"]), f"{m['booked']} ({m['booked_d']})",
                        pct(m["conversion"]), pct(m["answered_rate"])])
    return {"score": score, "callback": callback, "handled": handled, "agents": agents, "markets": markets}


def _action_lines(brief: dict) -> list[tuple[str, list[str]]]:
    a = brief["actions"]
    groups = []
    groups.append((f"1 · Call back now: missed callers not reached ({len(a['call_back'])})", [
        f"{x['name']} · {x['market']} · {x['phone']} — missed {x['time']}, {x['attempts']} attempts → {x['owner']}"
        for x in a["call_back"]
    ]))
    groups.append((f"2 · Follow up open quotes from {_label(brief['day'])} ({len(a['open_quotes'])})", [
        f"{x['name']} · {x['market']} · {x['phone']} — {money(x['quote']) + ', ' if x['quote'] else ''}"
        f"{x['status']}: {_short(x['summary'])} → {x['owner']}"
        for x in a["open_quotes"]
    ]))
    by_owner: dict[str, list[dict]] = {}
    for x in a["carryover"]:
        by_owner.setdefault(x["owner"], []).append(x)
    groups.append((f"3 · Carryover: prior day's quotes never followed up ({len(a['carryover'])})", [
        f"{owner}: {len(items)} — " + ", ".join(
            f"{x['name']} ({x['market']}{', ' + money(x['quote']) if x['quote'] else ''})"
            for x in sorted(items, key=lambda i: -(i["quote"] or 0))[:4])
        for owner, items in sorted(by_owner.items(), key=lambda kv: -len(kv[1]))
    ]))
    return groups


def _short(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _follow(brief: dict) -> list[str]:
    f = brief["follow_through"]
    m, q, w = f["missed_reached"], f["quotes_followed_up"], f["web_leads_called"]
    return [
        f"Missed callers unreached at end of day, reached since: {m['done']} of {m['of']}",
        f"Open quotes followed up within 24h: {q['done']} of {q['of']}",
        f"In-market web leads called: {w['done']} of {w['of']} ({w['within_5min']} within 5 min)",
    ]


def render_markdown(brief: dict, notes: str = "") -> str:
    rows = _rows(brief)

    def table(r: list[list[str]]) -> str:
        lines = ["| " + " | ".join(r[0]) + " |", "|" + "---|" * len(r[0])]
        lines += ["| " + " | ".join(row) + " |" for row in r[1:]]
        return "\n".join(lines)

    n = brief["numbers"]
    out = [f"# {title(brief)}", status_line(brief), ""]
    if notes:
        out += [notes.strip(), ""]
    out += ["## Scorecard", table(rows["score"]),
            f"Calls handled: {pct(n['day']['handled_rate'])} (prior {pct(n['prior']['handled_rate'])}, 7-day "
            f"{pct(n['seven_day']['handled_rate'])}) · Avg ticket {money(n['day']['avg_ticket'])} "
            f"(prior {money(n['prior']['avg_ticket'])}, 7-day {money(n['seven_day']['avg_ticket'])})", "",
            "## Missed calls called back within 60 seconds", table(rows["callback"]), "",
            "## Calls handled: answered + called back within 60 sec", table(rows["handled"]),
            "ProNexis answers 88.7% live.", coverage_line(brief), "", "## Action list"]
    for heading, items in _action_lines(brief):
        out += [f"**{heading}**"] + [f"- [ ] {i}" for i in items] + [""]
    out += ["## Did the prior day's follow-ups happen?"] + [f"- {x}" for x in _follow(brief)] + [""]
    if brief["alerts"]:
        out += ["## Alerts"] + [f"- {DOT[a['level']]} **{a['title']}** — {a['detail']}" for a in brief["alerts"]] + [""]
    out += ["## Agents", table(rows["agents"]), "",
            "## Markets (last 7 days)", table(rows["markets"]),
            f"Plus {brief['out_of_area_leads']} out-of-area leads, not worked.", "", _footnote(brief)]
    return "\n".join(out)


def render_html(brief: dict, notes: str = "") -> str:
    rows = _rows(brief)

    def table(r: list[list[str]]) -> str:
        head = "".join(f"<th align='left'>{escape(c)}</th>" for c in r[0])
        body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>" for row in r[1:])
        return f"<table border='1' cellpadding='6' cellspacing='0'><tr>{head}</tr>{body}</table>"

    def ul(items: list[str], check: bool = False) -> str:
        box = "☐ " if check else ""
        return "<ul>" + "".join(f"<li>{box}{escape(i)}</li>" for i in items) + "</ul>" if items else "<p>None.</p>"

    n = brief["numbers"]
    parts = [f"<h2>{escape(title(brief))}</h2>", f"<p>{escape(status_line(brief))}</p>"]
    if notes:
        parts.append("".join(f"<p>{escape(p)}</p>" for p in notes.strip().split("\n\n")))
    parts += ["<h3>Scorecard</h3>", table(rows["score"]),
              f"<p>Calls handled: {pct(n['day']['handled_rate'])} (prior {pct(n['prior']['handled_rate'])}, "
              f"7-day {pct(n['seven_day']['handled_rate'])}) · Avg ticket {money(n['day']['avg_ticket'])} "
              f"(prior {money(n['prior']['avg_ticket'])}, 7-day {money(n['seven_day']['avg_ticket'])})</p>",
              "<h3>Missed calls called back within 60 seconds</h3>", table(rows["callback"]),
              "<h3>Calls handled: answered + called back within 60 sec</h3>", table(rows["handled"]),
              "<p>ProNexis answers 88.7% live.</p>", f"<p>{escape(coverage_line(brief))}</p>",
              "<h3>Action list</h3>"]
    for heading, items in _action_lines(brief):
        parts += [f"<p><b>{escape(heading)}</b></p>", ul(items, check=True)]
    parts += ["<h3>Did the prior day's follow-ups happen?</h3>", ul(_follow(brief))]
    if brief["alerts"]:
        parts += ["<h3>Alerts</h3>",
                  ul([f"{DOT[a['level']]} {a['title']} — {a['detail']}" for a in brief["alerts"]])]
    parts += ["<h3>Agents</h3>", table(rows["agents"]),
              "<h3>Markets (last 7 days)</h3>", table(rows["markets"]),
              f"<p>Plus {brief['out_of_area_leads']} out-of-area leads, not worked.</p>",
              f"<p><small>{escape(_footnote(brief))}</small></p>"]
    return "\n".join(parts)


def coverage_line(brief: dict) -> str:
    """One line on call-center capacity: missed calls that rang while an agent was free, how busy agents were."""
    cov = brief.get("coverage")
    if not cov:
        return ""

    def part(c: dict) -> str:
        worst = f", most missed at {_hour(c['worst_hour'])}" if c.get("worst_hour") is not None else ""
        return (f"{c['missed_free']} of {c['missed']} missed calls rang with an agent free, agents on calls "
                f"{pct(c['busy'])} of their shift{worst}")

    return f"Capacity: {_label(brief['day'])} {part(cov['day'])}. Last 7 days: {part(cov['seven_day'])}."


def _hour(h: int) -> str:
    return f"{(h - 1) % 12 + 1}{'am' if h < 12 else 'pm'}"


def _footnote(brief: dict) -> str:
    t = brief["targets"]
    return (f"Targets: answered live {pct(t['answered_live'])}, calls handled {pct(t['calls_handled'])}, "
            f"missed calls called back within 60 sec {pct(t['callback_60s'])}, web leads called in 5 min "
            f"{pct(t['web_leads_5min'])}, opportunity conversion {pct(t['opportunity_conversion'])}, "
            f"{t['jobs_booked']} jobs/day, every quote followed up within 24h. Green = at target, yellow = within "
            f"15 points, red = further off. Opportunity = a lead we spoke with who wants a service we offer in a market we "
            f"serve. Generated {brief['generated_at']}.")
