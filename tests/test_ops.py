from datetime import date

from app.brief import Activity, coverage
from app.ops import (AHEAD, DAY_BEFORE, DAY_OF, LATER, NO_REASON, build_ops_report, cancel_reason, cancel_timing,
                     render_ops_html, render_ops_markdown)
from tests.test_brief import AGENT, TZ, inbound, outbound, ts
from tests.test_jobs import job

TODAY = date(2026, 10, 1)  # a Thursday


def jobs():
    return [
        job(1, "2026-09-20", "2026-09-21", "Done", 300, ClientId="a"),
        job(2, "2026-09-20", "2026-09-22", "Canceled", 200, updated="2026-09-22", ClientId="b"),   # day of
        job(3, "2026-09-10", "2026-09-23", "Canceled", 0, updated="2026-09-20", ClientId="c",      # 2+ days ahead
            SubStatus="Too Expensive"),
        job(4, "2026-09-24", "2026-09-26", "Done", 400, ClientId="c"),                              # rebooks #3
        job(5, "2026-09-22", "2026-09-24", "Canceled", 0, updated="2026-09-23", ClientId="d",      # day before
            JobNotes="Customer called to cancel, will call back to reschedule"),
        job(6, "2026-09-01", "2026-09-05", "Canceled", 0, updated="2026-09-28", ClientId="e"),     # marked later
        job(7, "2026-09-25", "2026-09-25", "Canceled", 0, ClientId="f", SubStatus="Duplicate Job"),  # left out
        job(8, "2026-09-25", "2026-10-02", "Submitted", 250, ClientId="g", phone="6175550008"),    # confirm
        job(9, "2026-10-01", "2026-10-02", "Submitted", 250, ClientId="h"),                        # booked 1 day ahead
        job(10, "2026-09-28", "2026-10-06", "Submitted", 0, ClientId="i"),
    ]


def act():
    lost = {"custom_fields": {"lead_status": "Unbookable-Schedule", "franchise_location": "Greater Boston"}}
    return Activity([
        inbound(1, ts(9, day=date(2026, 9, 29)), "+16175550001", **lost),
        inbound(2, ts(9, day=date(2026, 9, 29)), "+16175550002",
                custom_fields={"lead_status": "Booked", "franchise_location": "Greater Boston"}),
    ], TZ)


def report():
    return build_ops_report(act(), {"Greater Boston": (jobs(), [])}, TODAY)


def test_timing_and_reason():
    js = {j["SerialId"]: j for j in jobs()}
    assert [cancel_timing(js[i]) for i in (2, 3, 5, 6)] == [DAY_OF, AHEAD, DAY_BEFORE, LATER]
    assert cancel_reason(js[3]) == "Price" and cancel_reason(js[5]) == "Wants to reschedule"
    assert cancel_reason(js[2]) == NO_REASON


def test_market_numbers():
    m = report()["markets"][0]
    assert (m["appts"], m["canceled"]) == (6, 4)   # #7 duplicate left out; #8-10 still ahead
    assert m["timing"] == {AHEAD: 1, DAY_BEFORE: 1, DAY_OF: 1, LATER: 1}
    assert m["rebooked"] == 1 and m["quoted_lost"] == 200
    assert m["scheduled_ahead"] == 3 and m["wait_days"] == 2   # booked since 9/17: 1, 2, 2, 2, 7, 1, 8 days ahead
    assert (m["lost_to_slot"], m["opps"]) == (1, 2)


def test_actions_and_render():
    r = report()
    assert [x["serial"] for x in r["actions"]["confirm"]] == [8]
    assert [x["serial"] for x in r["actions"]["rebook"]] == [2, 5]  # #3 rebooked, #6 marked later
    assert "Lost to no slot" in render_ops_markdown(r)
    assert render_ops_html(r).startswith("<!doctype html>")


def test_coverage_missed_while_free():
    day = date(2026, 9, 30)
    a = Activity([
        inbound(1, ts(9, day=day), "+16175550001", duration=300),                       # shift starts 9:00
        inbound(2, ts(9, 10, day=day), "+16175550002", answered=False, duration=30),   # agent free -> counts
        inbound(3, ts(10, day=day), "+16175550003", duration=600),
        inbound(4, ts(10, 2, day=day), "+16175550004", answered=False, duration=30),   # agent on a call
        outbound(5, ts(11, day=day), "+16175550005", duration=60, agent=AGENT),       # shift ends 11:01
        inbound(6, ts(12, day=day), "+16175550006", answered=False, duration=30),      # after shift
    ], TZ)
    c = coverage(a, [day])
    assert (c["missed"], c["missed_free"]) == (3, 1)
    nine = next(h for h in c["hours"] if h["hour"] == 9)
    assert nine["agents"] == 1.0 and round(nine["busy"], 3) == round(300 / 3600, 3)
