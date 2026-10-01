"""Daily dashboard page: one HTML file with an Overview tab, a Call Center tab, and placeholder tabs
for the sources still to come (HubSpot, Reputation, Workiz, QuickBooks).

    html = render_dashboard(brief, notes)

The page is self-contained (inline CSS, SVG charts drawn here, a few lines of JS for tabs and
checklists), so it can be published as-is.
"""
from __future__ import annotations

from datetime import date
from html import escape

from app.brief_render import _action_lines, _follow, _label, _rows, money, pct, status_line, title
from app.jobs import (CLOSE_NOTE, DEFINITIONS, TICKET_NOTE, booking_rows, close_grid_rows, flood_it_summary, group_rows, market_rows,
                      missing_rows, quote_rows, quote_summary, ticket_grid_rows)

STATUS_CLASS = {"green": "g", "yellow": "y", "red": "r", "none": "n"}

UPCOMING = [
    ("franchise", "Franchise Development", "HubSpot",
     "Calls, meetings booked and candidates moving forward, from the HubSpot report that already runs each morning."),
    ("reputation", "Reputation", "Consumer Fusion reviews & NPS",
     "New reviews, NPS by market and detractor alerts, from the Reputation Pulse work."),
    ("jobs", "Jobs & Revenue", "Workiz",
     "Quote vs sold, franchise owner close rate by service, sold amounts missing, schedule capacity. "
     "Starts when the Workiz tokens are added."),
    ("financials", "Financials", "QuickBooks (franchise books)",
     "Profit by market, marketing return by lead source and a royalty check against reported jobs."),
]


def _spark(values: list[float | None], width: int = 120, height: int = 32, ceiling: float | None = None) -> str:
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    top = ceiling if ceiling is not None else max(v for _, v in pts) or 1
    step = width / (len(values) - 1)
    coords = [(round(i * step, 1), round(height - 3 - (v / top) * (height - 6), 1)) for i, v in pts]
    path = " ".join(f"{'M' if n == 0 else 'L'}{x},{y}" for n, (x, y) in enumerate(coords))
    x, y = coords[-1]
    return (f"<svg class='spark' viewBox='0 0 {width} {height}' aria-hidden='true'>"
            f"<path d='{path}' class='spark-line'/><circle cx='{x}' cy='{y}' r='3' class='spark-dot'/></svg>")


def _line_chart(trend: list[dict]) -> str:
    """Answered live % and calls handled % for the last 14 days, one 0–100% axis."""
    w, h, left, bottom, top = 640, 220, 36, 26, 12
    n = len(trend)
    step = (w - left - 12) / max(n - 1, 1)

    def y(v: float) -> float:
        return round(top + (1 - v) * (h - top - bottom), 1)

    parts = [f"<svg viewBox='0 0 {w} {h}' class='chart' role='img' aria-label='Answered live and calls handled, last {n} days'>"]
    for g in (0, 0.25, 0.5, 0.75, 1):
        parts.append(f"<line x1='{left}' x2='{w - 12}' y1='{y(g)}' y2='{y(g)}' class='grid'/>"
                     f"<text x='{left - 6}' y='{y(g) + 4}' text-anchor='end' class='tick'>{int(g * 100)}%</text>")
    for i, d in enumerate(trend):
        if i % 2 == 0 or i == n - 1:
            parts.append(f"<text x='{left + i * step}' y='{h - 8}' text-anchor='middle' class='tick'>"
                         f"{escape(_label(d['day']).split(' ')[1])}</text>")
    for key, cls, label in (("handled_rate", "s2", "Handled"), ("answered_rate", "s1", "Answered live")):
        pts = [(left + i * step, d[key]) for i, d in enumerate(trend) if d[key] is not None and d["queued"] >= 10]
        if len(pts) < 2:
            continue
        path = " ".join(f"{'M' if k == 0 else 'L'}{round(x, 1)},{y(v)}" for k, (x, v) in enumerate(pts))
        parts.append(f"<path d='{path}' class='line {cls}'/>")
        for x, v in pts:
            parts.append(f"<circle cx='{round(x, 1)}' cy='{y(v)}' r='8' class='hit'><title>{label}: {pct(v)}</title></circle>")
        x, v = pts[-1]
        parts.append(f"<circle cx='{round(x, 1)}' cy='{y(v)}' r='4' class='dot {cls}'/>")
    parts.append(f"<line x1='{left}' x2='{w - 12}' y1='{y(0.85)}' y2='{y(0.85)}' class='target'/>"
                 f"<text x='{w - 14}' y='{y(0.85) - 5}' text-anchor='end' class='tick'>Target 85%</text></svg>")
    return "".join(parts)


def _bar_chart(trend: list[dict], target: int) -> str:
    w, h, left, bottom, top = 640, 180, 36, 26, 12
    n = len(trend)
    peak = max([d["jobs_booked"] for d in trend] + [target]) or 1
    slot = (w - left - 12) / n
    bw = slot * 0.6

    def y(v: float) -> float:
        return round(top + (1 - v / peak) * (h - top - bottom), 1)

    parts = [f"<svg viewBox='0 0 {w} {h}' class='chart' role='img' aria-label='Jobs booked per day'>"]
    for g in sorted({0, target} | ({peak} if peak - target > peak * 0.15 else set())):
        parts.append(f"<line x1='{left}' x2='{w - 12}' y1='{y(g)}' y2='{y(g)}' class='{'target' if g == target else 'grid'}'/>"
                     f"<text x='{left - 6}' y='{y(g) + 4}' text-anchor='end' class='tick'>{g}</text>")
    for i, d in enumerate(trend):
        x = left + i * slot + (slot - bw) / 2
        v = d["jobs_booked"]
        bh = max(h - bottom - y(v), 1)
        parts.append(f"<g><title>{escape(_label(d['day']))}: {v} jobs</title>"
                     f"<rect x='{round(x, 1)}' y='{y(v)}' width='{round(bw, 1)}' height='{round(bh, 1)}' rx='3' class='bar'/>"
                     f"<text x='{round(x + bw / 2, 1)}' y='{h - 8}' text-anchor='middle' class='tick'>"
                     f"{escape(_label(d['day']).split(' ')[1])}</text></g>")
    return "".join(parts) + "</svg>"


def _table(rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
    return f"<div class='tbl'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _tiles(brief: dict) -> str:
    n, st, trend = brief["numbers"], brief["status"], brief["trend"]
    specs = [
        ("answered_live", "Answered live", "answered_rate", pct, 1.0),
        ("calls_handled", "Calls handled", "handled_rate", pct, 1.0),
        ("opportunity_conversion", "Opportunity conversion", "opportunity_conversion", pct, 1.0),
        ("jobs_booked", "Jobs booked", "jobs_booked", str, None),
        ("web_leads_5min", "Web leads called in 5 min", "web_leads_5min_rate", pct, 1.0),
    ]
    out = []
    for key, label, field, fmt, ceiling in specs:
        s = st[key]
        target = s["target"]
        target_txt = str(target) if key == "jobs_booked" else pct(target)
        prior = n["prior"][field]
        out.append(
            f"<div class='tile {STATUS_CLASS[s['status']]}'><span class='lbl'>{label}</span>"
            f"<span class='val'>{escape(fmt(n['day'][field]) if n['day'][field] is not None else '—')}</span>"
            f"{_spark([d[field] for d in trend], ceiling=ceiling)}"
            f"<span class='cmp'>Prior {escape(fmt(prior) if prior is not None else '—')} · Target {target_txt}</span></div>"
        )
    return "<div class='tiles'>" + "".join(out) + "</div>"


def _actions(brief: dict, limit: int | None = None, prefix: str = "a") -> str:
    out = []
    for g, (heading, items) in enumerate(_action_lines(brief)):
        shown = items[:limit] if limit else items
        lis = "".join(
            f"<li><input type='checkbox' id='{prefix}{g}-{i}' aria-label='Done'><span>{escape(item)}</span></li>"
            for i, item in enumerate(shown)
        ) or "<li class='none'>Nothing here.</li>"
        more = f"<p class='more'>+{len(items) - len(shown)} more on the Call Center tab</p>" if len(items) > len(shown) else ""
        out.append(f"<div class='group'><h4>{escape(heading)}</h4><ul class='todo'>{lis}</ul>{more}</div>")
    return "<div class='card'>" + "".join(out) + "</div>"


def _alerts(brief: dict) -> str:
    if not brief["alerts"]:
        return "<p class='fine'>No alerts.</p>"
    return "<div class='alerts'>" + "".join(
        f"<div class='alert {STATUS_CLASS[a['level']]}'><b>{escape(a['title'])}</b><span>{escape(a['detail'])}</span></div>"
        for a in brief["alerts"]
    ) + "</div>"


def _jobs_tiles(reports: list[dict]) -> str:
    totals = [r["totals"] for r in reports]
    sold = sum(t["sold"] for t in totals)
    decided = sum(t["decided"] for t in totals)
    sold_amount = sum(t["sold_amount"] for t in totals)
    no_job = sum(r["ctm_bookings"]["total"] - r["ctm_bookings"]["with_job"] for r in reports)
    booked = sum(r["ctm_bookings"]["total"] for r in reports)
    missing = sum(t["missing"] for t in totals)
    specs = [
        ("Sold", money(sold_amount), f"{sold} jobs"),
        ("Close rate", pct(sold / decided if decided else None), f"{sold} of {decided} decided"),
        ("Avg ticket", money(sold_amount / sold if sold else None), f"Outstanding {money(sum(t['outstanding'] for t in totals))}"),
        ("Sold amount missing", str(missing), "past appointments at $0"),
        ("CTM bookings, no job", str(no_job), f"of {booked} booked in CTM"),
    ]
    return "<div class='tiles'>" + "".join(
        f"<div class='tile{' r' if warn else ''}'><span class='lbl'>{escape(label)}</span>"
        f"<span class='val'>{escape(val)}</span><span class='cmp'>{escape(cmp)}</span></div>"
        for (label, val, cmp), warn in zip(specs, (False, False, False, missing > 0, no_job > 0))
    ) + "</div>"


def _jobs_panel(reports: list[dict]) -> str:
    start, today = reports[0]["start"], reports[0]["today"]
    parts = [f"<p class='meta'>Workiz jobs created {escape(start)} to {escape(today)}, matched to CTM by phone number.</p>",
             _jobs_tiles(reports), "<h2>Markets</h2>", _table(market_rows(reports))]
    for r in reports:
        parts += [f"<h2>{escape(r['market'])}: close rate by service</h2>", _table(group_rows(r["by_service"], "Service")),
                  f"<h2>{escape(r['market'])}: close rate by service and source</h2>",
                  f"<p class='fine'>{escape(CLOSE_NOTE)}</p>", _table(close_grid_rows(r)),
                  f"<h2>{escape(r['market'])}: average ticket by service and source</h2>",
                  f"<p class='fine'>{escape(TICKET_NOTE)}</p>", _table(ticket_grid_rows(r)),
                  f"<h2>{escape(r['market'])}: close rate by lead source</h2>", _table(group_rows(r["by_source"], "Source")),
                  f"<h2>{escape(r['market'])}: Flood It</h2>", f"<p class='fine'>{escape(flood_it_summary(r))}</p>"]
        if r["flood_it"]["totals"]["jobs"]:
            parts += [_table(group_rows(r["flood_it"]["by_source"], "Flood It source")),
                      _table(group_rows(r["flood_it"]["by_service"], "Service"))]
        parts += [f"<h2>{escape(r['market'])}: quote vs sold</h2>", f"<p class='fine'>{escape(quote_summary(r))}</p>"]
        for heading, rows in ((None, quote_rows(r)), ("sold amount missing", missing_rows(r)),
                              ("CTM bookings with no Workiz job", booking_rows(r))):
            if heading:
                parts.append(f"<h2>{escape(r['market'])}: {escape(heading)}</h2>")
            parts.append(_table(rows) if len(rows) > 1 else "<p class='fine'>None.</p>")
    parts.append(f"<p class='fine'>{escape(DEFINITIONS)}</p>")
    return "<section class='panel' id='tab-jobs' hidden>" + "".join(parts) + "</section>"


def render_dashboard(brief: dict, notes: str = "", jobs: list[dict] | None = None) -> str:
    """`jobs`: Jobs & Revenue reports from app.jobs (one per Workiz market); without them the tab is a placeholder."""
    rows = _rows(brief)
    n = brief["numbers"]
    d = date.fromisoformat(brief["day"])
    notes_html = "".join(f"<p>{escape(p)}</p>" for p in notes.strip().splitlines() if p.strip())
    live = {"jobs": _jobs_panel(jobs)} if jobs else {}
    upcoming_tabs = "".join(
        f"<button role='tab' data-tab='{key}' aria-selected='false'>{escape(name)}"
        f"{'' if key in live else '<span class=soon>soon</span>'}</button>"
        for key, name, _, _ in UPCOMING
    )
    upcoming_panels = "".join(
        live.get(key) or
        f"<section class='panel' id='tab-{key}' hidden><div class='placeholder'><h2>{escape(name)}</h2>"
        f"<p class='src'>Source: {escape(src)}</p><p>{escape(desc)}</p></div></section>"
        for key, name, src, desc in UPCOMING
    )
    return f"""<title>Voda Daily Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
<main>
<header>
  <div class="eyebrow">Voda Cleaning &amp; Restoration · Daily dashboard</div>
  <h1>{escape(d.strftime('%A, %B'))} {d.day}</h1>
  <p class="meta">{escape(title(brief))} · updated {escape(brief['generated_at'].replace('T', ' '))}</p>
  <p class="status">{escape(status_line(brief))}</p>
</header>
<nav role="tablist" aria-label="Dashboard sections">
  <button role="tab" data-tab="overview" aria-selected="true">Overview</button>
  <button role="tab" data-tab="calls" aria-selected="false">Call Center</button>
  {upcoming_tabs}
</nav>

<section class="panel" id="tab-overview">
  {f"<div class='notes'>{notes_html}</div>" if notes_html else ""}
  {_tiles(brief)}
  <p class="fine">Avg ticket {money(n['day']['avg_ticket'])} (prior {money(n['prior']['avg_ticket'])}, 7-day {money(n['seven_day']['avg_ticket'])}) · Missed calls called back within 60 sec: {n['day']['callback_60s']} of {n['day']['missed']} ({pct(n['day']['callback_60s_rate'])})</p>
  <h2>Top actions</h2>
  {_actions(brief, limit=3, prefix="o")}
  <h2>Alerts</h2>
  {_alerts(brief)}
</section>

<section class="panel" id="tab-calls" hidden>
  <h2>Scorecard</h2>{_table(rows['score'])}
  <div class="charts">
    <figure><figcaption><b>Answered live vs calls handled</b><span class="legend"><i class="s1"></i>Answered live <i class="s2"></i>Handled (answered + called back within 60 sec)</span></figcaption>{_line_chart(brief['trend'])}</figure>
    <figure><figcaption><b>Jobs booked per day</b><span class="legend">Dashed line: target {brief['targets']['jobs_booked']}</span></figcaption>{_bar_chart(brief['trend'], brief['targets']['jobs_booked'])}</figure>
  </div>
  <h2>Missed calls called back within 60 seconds</h2>{_table(rows['callback'])}
  <h2>Calls handled: answered + called back within 60 sec</h2>{_table(rows['handled'])}
  <p class="fine">ProNexis answers 88.7% live.</p>
  <h2>Action list</h2>{_actions(brief, prefix="c")}
  <h2>Did the prior day's follow-ups happen?</h2><ul class="plain">{''.join(f'<li>{escape(x)}</li>' for x in _follow(brief))}</ul>
  <h2>Alerts</h2>{_alerts(brief)}
  <h2>Agents</h2>{_table(rows['agents'])}
  <h2>Markets (last 7 days)</h2>{_table(rows['markets'])}
  <p class="fine">Plus {brief['out_of_area_leads']} out-of-area leads, not worked.</p>
</section>
{upcoming_panels}
</main>
<script>{JS.replace("__DAY__", brief["day"])}</script>
"""


CSS = """
:root{--bg:#f5f6f5;--surface:#fff;--sunk:#eef1ef;--ink:#111614;--ink-2:#4a5450;--ink-3:#7b8580;--line:#dfe4e1;
--accent:#0f6b5c;--accent-bg:#e2f1ed;--good:#0b7a3e;--good-bg:#e3f4ea;--warn:#9a5b00;--warn-bg:#fdf0d8;--bad:#b3261e;--bad-bg:#fbe5e3;
--s1:#2a78d6;--s2:#eb6834;--sans:"Instrument Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121614;--surface:#1a1f1d;--sunk:#151a18;--ink:#eef2f0;--ink-2:#b5c0bb;--ink-3:#86918c;--line:#2d3532;--accent:#4fc3a8;--accent-bg:#17302a;--good:#6fd39a;--good-bg:#16311f;--warn:#f0b454;--warn-bg:#35280f;--bad:#ff8a80;--bad-bg:#3a1a18;--s1:#3987e5;--s2:#d95926}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121614;--surface:#1a1f1d;--sunk:#151a18;--ink:#eef2f0;--ink-2:#b5c0bb;--ink-3:#86918c;--line:#2d3532;--accent:#4fc3a8;--accent-bg:#17302a;--good:#6fd39a;--good-bg:#16311f;--warn:#f0b454;--warn-bg:#35280f;--bad:#ff8a80;--bad-bg:#3a1a18;--s1:#3987e5;--s2:#d95926}
*{box-sizing:border-box}body{background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);padding:0 16px;margin:0}
main{max-width:1040px;margin:0 auto;padding-block:28px 64px;display:flex;flex-direction:column;gap:18px}
h1,h2,h4{margin:0;line-height:1.2;text-wrap:balance}h1{font-size:clamp(24px,3.6vw,32px);font-weight:700}h2{font-size:18px;font-weight:600;margin-top:10px}h4{font-size:14px;font-weight:600}
p{margin:0}.eyebrow{font:500 12px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
header{display:flex;flex-direction:column;gap:6px}.meta{color:var(--ink-2)}.status{font-weight:600}
nav{display:flex;gap:6px;overflow-x:auto;border-bottom:1px solid var(--line);padding-bottom:1px}
nav button{font:600 14px var(--sans);background:none;border:0;border-bottom:3px solid transparent;color:var(--ink-2);padding:10px 12px;cursor:pointer;white-space:nowrap}
nav button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent)}nav button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.soon{font:500 10.5px var(--mono);color:var(--ink-3);margin-left:6px;text-transform:uppercase;letter-spacing:.05em}
.panel{display:flex;flex-direction:column;gap:12px}.panel[hidden]{display:none}
.notes{background:var(--accent-bg);border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:4px}
.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}@media(max-width:900px){.tiles{grid-template-columns:repeat(2,1fr)}}@media(max-width:420px){.tiles{grid-template-columns:1fr}}
.tile{background:var(--surface);border:1px solid var(--line);border-top:4px solid var(--line);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:4px}
.tile.r{border-top-color:var(--bad)}.tile.y{border-top-color:var(--warn)}.tile.g{border-top-color:var(--good)}
.lbl{font-size:13px;color:var(--ink-2);font-weight:500}.val{font:700 28px/1 var(--sans);font-variant-numeric:tabular-nums}
.cmp{font:400 12px var(--mono);color:var(--ink-3)}.spark{width:100%;height:32px}.spark-line{fill:none;stroke:var(--s1);stroke-width:2}.spark-dot{fill:var(--s1);stroke:var(--surface);stroke-width:2}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.group{border-top:1px solid var(--line);padding:10px 14px}.group:first-child{border-top:0}
ul.todo{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
ul.todo li{display:grid;grid-template-columns:auto 1fr;gap:10px;font-size:14px}ul.todo li.done span{color:var(--ink-3);text-decoration:line-through}
ul.todo input{width:17px;height:17px;margin-top:2px;accent-color:var(--accent)}li.none{color:var(--ink-3)}
.more{font-size:12.5px;color:var(--ink-3);margin-top:6px}
.alerts{display:flex;flex-direction:column;gap:8px}.alert{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:0 10px 10px 0;padding:10px 14px;display:flex;flex-direction:column;gap:2px}
.alert.r{border-left-color:var(--bad)}.alert span{font-size:13.5px;color:var(--ink-2)}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{padding:8px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap;font-variant-numeric:tabular-nums}
th{font:500 11px var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3)}tbody tr:last-child td{border-bottom:0}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.charts{grid-template-columns:1fr}}
figure{margin:0;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:8px}
figcaption{display:flex;flex-direction:column;gap:2px;font-size:14px}.legend{font-size:12.5px;color:var(--ink-2)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 5px 0 8px;vertical-align:-1px}.legend i.s1{background:var(--s1)}.legend i.s2{background:var(--s2)}
.chart{width:100%;height:auto}.grid{stroke:var(--line)}.target{stroke:var(--ink-3);stroke-dasharray:4 4}
.tick{font:11px var(--mono);fill:var(--ink-3)}.line{fill:none;stroke-width:2}.line.s1{stroke:var(--s1)}.line.s2{stroke:var(--s2)}
.dot{stroke:var(--surface);stroke-width:2}.dot.s1{fill:var(--s1)}.dot.s2{fill:var(--s2)}.hit{fill:transparent}.bar{fill:var(--s1)}
ul.plain{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px}.fine{font-size:13px;color:var(--ink-2)}
.placeholder{background:var(--surface);border:1px dashed var(--line);border-radius:12px;padding:28px 22px;display:flex;flex-direction:column;gap:8px;max-width:640px}
.src{font:500 12px var(--mono);color:var(--accent)}
"""

JS = """
(function(){
  const tabs=[...document.querySelectorAll('[role=tab]')];
  function show(name){
    tabs.forEach(t=>t.setAttribute('aria-selected',String(t.dataset.tab===name)));
    document.querySelectorAll('.panel').forEach(p=>{p.hidden=p.id!=='tab-'+name;});
  }
  tabs.forEach(t=>t.addEventListener('click',()=>{show(t.dataset.tab);try{history.replaceState(null,'','#'+t.dataset.tab);}catch(e){}}));
  const start=(location.hash||'').slice(1);
  if(tabs.some(t=>t.dataset.tab===start)) show(start);
  const key='voda-dash-__DAY__';let state={};
  try{state=JSON.parse(localStorage.getItem(key)||'{}');}catch(e){}
  document.querySelectorAll('ul.todo input').forEach(cb=>{
    const id=cb.id.slice(1);
    if(state[id]){cb.checked=true;cb.closest('li').classList.add('done');}
    cb.addEventListener('change',()=>{
      state[id]=cb.checked;
      document.querySelectorAll('ul.todo input').forEach(o=>{if(o.id.slice(1)===id){o.checked=cb.checked;o.closest('li').classList.toggle('done',cb.checked);}});
      try{localStorage.setItem(key,JSON.stringify(state));}catch(e){}
    });
  });
})();
"""
