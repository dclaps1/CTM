from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.brief import Activity, Targets, build_brief, day_numbers, market_of, status
from app.brief_render import render_html, render_markdown

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 9, 30)
QUEUE = [{"route_type": "CallQueue"}]
AGENT = {"name": "Sergio Vaca"}


def ts(hour: int, minute: int = 0, second: int = 0, day: date = DAY) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ).timestamp())


def rec(rid: int, when: int, number: str, **fields) -> dict:
    return {"id": rid, "unix_time": when, "contact_number": number, **fields}


def inbound(rid, when, number, answered=True, **fields):
    base = {"direction": "inbound", "call_path": QUEUE, "dial_status": "answered" if answered else "no answer",
            "duration": 30}
    if answered:
        base.update(agent=AGENT, talk_time=200, summary="Discussed carpet cleaning and quoted $165.")
    return rec(rid, when, number, **{**base, **fields})


def outbound(rid, when, number, answered=True, **fields):
    base = {"direction": "outbound", "dial_status": "answered" if answered else "no-answer", "agent": AGENT,
            "talk_time": 120 if answered else 0, "summary": "Talked about the quote." if answered else ""}
    return rec(rid, when, number, **{**base, **fields})


def booked(**extra):
    return {"custom_fields": {"lead_status": "Booked", "franchise_location": "Charleston"}, **extra}


def scenario() -> Activity:
    return Activity([
        # 1) answered, booked lead in Charleston, $200 sale
        inbound(1, ts(9), "+18435550001", **booked(sale={"conversion": True, "value": 200})),
        # 2) missed, called back 40s after the missed call ended -> counts as handled
        inbound(2, ts(10), "+18435550002", answered=False, duration=20),
        outbound(3, ts(10, 1), "+18435550002",
                 custom_fields={"lead_status": "Bookable-Not Booked", "franchise_location": "Charleston"},
                 summary="Quoted $320 for a sectional; she is comparing quotes."),
        # 3) missed, callback 5 minutes later and only voicemail -> not handled, still unreached
        inbound(4, ts(11), "+16175550003", answered=False, tracking_label="Greater Boston Main Line"),
        outbound(5, ts(11, 5), "+16175550003", summary="Reached voicemail and left a message."),
        # 4) missed spam caller -> excluded from call-back list
        inbound(6, ts(12), "+15555550004", answered=False, custom_fields={"lead_status": "Spam"}),
        # 5) in-market web form called 2 minutes later
        rec(7, ts(13), "", direction="form", form={"customer_number_to_dial": "+19135550005"},
            custom_fields={"franchise_location": "South Kansas City"}),
        outbound(8, ts(13, 2), "+19135550005", summary="Reached voicemail and left a message."),
        # 6) out-of-area web form never called -> not counted as a web lead
        rec(9, ts(14), "", direction="form", form={"customer_number_to_dial": "+13035550006"},
            custom_fields={"franchise_location": "Out of Area"}),
    ], TZ)


def test_handle_rate_counts_callbacks_within_60_seconds():
    n = day_numbers(scenario(), DAY)
    assert (n.queued, n.answered, n.missed) == (4, 1, 3)
    assert n.callback_60s == 1 and n.callback_connected == 1
    assert n.handled == 2


def test_leads_opportunities_and_web_speed_to_lead():
    n = day_numbers(scenario(), DAY)
    assert n.leads == 6
    assert n.opportunities == 2  # booked caller + quoted callback; voicemail-only and spam are not
    assert n.booked_leads == 1 and n.jobs_booked == 1
    assert (n.web_leads, n.web_leads_5min, n.web_leads_uncalled) == (1, 1, 0)
    assert n.as_dict()["avg_ticket"] == 200


def test_brief_action_lists_and_follow_through():
    brief = build_brief(scenario(), DAY, now=ts(23))
    call_back = brief["actions"]["call_back"]
    assert [c["phone"] for c in call_back] == ["(617) 555-0003"]  # spam and the reached caller are left out
    assert call_back[0]["market"] == "Greater Boston" and call_back[0]["attempts"] == 1
    quotes = brief["actions"]["open_quotes"]
    assert len(quotes) == 1 and quotes[0]["quote"] == 320 and quotes[0]["owner"] == "Sergio Vaca"
    assert brief["status"]["jobs_booked"]["status"] == "red"  # 1 job against a target of 12


def test_market_matching_does_not_confuse_south_markets():
    kc = {"tracking_label": "South Atlanta Thumbtack"}
    assert market_of(kc, [{"custom_fields": {"franchise_location": "South Kansas City"}}]) == "South Kansas City"
    assert market_of({"tracking_label": "South Atlanta Main"}, []) == "South Atlanta"
    assert market_of({"tracking_label": "form reactor"}, []) == "Unassigned"


def test_status_bands():
    assert status(0.86, 0.85) == "green"
    assert status(0.50, 0.60) == "yellow"  # within 15 points
    assert status(0.40, 0.60) == "red"
    assert status(11, 12) == "yellow" and status(8, 12) == "red"
    assert status(None, 0.5) == "none"


def test_renderers_include_every_section():
    brief = build_brief(scenario(), DAY, Targets(), now=ts(23))
    md = render_markdown(brief, notes="Strong booking day.")
    for heading in ("Scorecard", "Missed calls called back within 60 seconds", "Calls handled", "Action list",
                    "Did the prior day's follow-ups happen?", "Agents", "Markets (last 7 days)"):
        assert heading in md
    assert "Strong booking day." in md
    html = render_html(brief)
    assert html.startswith("<h2>Daily Call Center Brief — Wednesday, Sep 30</h2>")
    assert "<script" not in html and "(617) 555-0003" in html
