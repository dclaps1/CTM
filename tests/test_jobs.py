from datetime import date

from app.brief import Activity, build_brief
from app.jobs import build_jobs_report, outcome, render_jobs_markdown
from tests.test_brief import DAY, TZ, inbound, scenario, ts

TODAY = date(2026, 10, 1)
START = date(2026, 9, 1)
BOSTON = {"custom_fields": {"lead_status": "Booked", "franchise_location": "Greater Boston"}}


def job(serial, created, appt, status="Submitted", total=0, due=0, phone="", service="Water Damage", source="Google"):
    return {"UUID": f"U{serial}", "SerialId": serial, "CreatedDate": f"{created} 09:00:00",
            "JobDateTime": f"{appt} 10:00:00", "Status": status, "JobTotalPrice": total, "JobAmountDue": due,
            "Phone": phone, "JobType": service, "JobSource": source, "FirstName": "pat", "Team": [{"Name": "Dan"}]}


def activity():
    return Activity([
        # booked in CTM with a $500 sale value -> has job 1 (sold $650)
        inbound(1, ts(9, day=date(2026, 9, 10)), "+16175550001", sale={"conversion": True, "value": 500}, **BOSTON),
        # booked in CTM, only a Workiz lead
        inbound(2, ts(9, day=date(2026, 9, 12)), "+16175550002", **BOSTON),
        # booked in CTM, nothing in Workiz
        inbound(3, ts(9, day=date(2026, 9, 14)), "+16175550003", **BOSTON),
        # booked in another market -> not Boston's problem
        inbound(4, ts(9, day=date(2026, 9, 14)), "+18435550004",
                custom_fields={"lead_status": "Booked", "franchise_location": "Charleston"}),
    ], TZ)


def jobs():
    return [
        job(1, "2026-09-10", "2026-09-15", "Done", 650, 150, phone="(617) 555-0001"),
        job(2, "2026-09-11", "2026-09-16", "Canceled", service="Mold Mitigation"),
        job(3, "2026-09-12", "2026-09-20", "Done", 0, service="Mold Mitigation", source=""),   # done, $0
        job(4, "2026-09-13", "2026-09-21", "Submitted", 0),                               # passed, no amount
        job(5, "2026-09-20", "2026-10-05", "Submitted", 0),                               # future -> pending
        job(6, "2026-08-01", "2026-08-05", "Done", 999),                                  # created before window
        job(7, "2026-09-14", "2026-09-18", "In progress", 0),                             # on site -> pending
        job(8, "2026-09-15", "2026-09-19", "Done", 3000, source="Flood It - Service Direct"),
        job(9, "2026-09-16", "2026-09-20", "Canceled", source="Flood It - Inquirly"),
        job(10, "2026-09-17", "2026-09-22", "Done", 1000, service="Mold Mitigation", source="Flood It - Inquirly"),
    ]


def test_quoted_price_is_not_sold_until_done():
    assert outcome(job(11, "2026-09-20", "2026-10-05", "In progress", 699), TODAY) == "pending"  # booked, future
    assert outcome(job(12, "2026-09-20", "2026-10-05", "Submitted", 249), TODAY) == "pending"
    assert outcome(job(13, "2026-09-01", "2026-09-10", "Submitted", 249), TODAY) == "missing"   # never closed out
    assert outcome(job(14, "2026-09-01", "2026-09-10", "done pending approval", 900), TODAY) == "sold"
    assert outcome(job(15, "2026-09-01", "2026-09-10", "Canceled", 1928), TODAY) == "canceled"


def test_outcomes():
    out = [outcome(j, TODAY) for j in jobs()]
    assert out == ["sold", "canceled", "missing", "missing", "pending", "sold", "pending", "sold", "canceled", "sold"]


def test_report_close_rate_missing_quotes_and_bookings():
    r = build_jobs_report("Greater Boston", jobs()[:7], [{"Phone": "617-555-0002"}], activity(), START, TODAY)
    t = r["totals"]
    assert (t["jobs"], t["sold"], t["decided"], t["pending"], t["missing"]) == (6, 1, 4, 2, 2)
    assert t["close_rate"] == 0.25 and t["sold_amount"] == 650 and t["collected"] == 500 and t["outstanding"] == 150
    assert t["ctm_matched"] == 1
    assert [m["serial"] for m in r["missing_amounts"]] == [3, 4]
    assert r["missing_amounts"][0]["reason"] == "Done at $0"
    services = {g["name"]: g for g in r["by_service"]}
    assert services["Mold Mitigation"]["close_rate"] == 0.0 and services["Water Damage"]["sold"] == 1
    assert "(not set)" in {g["name"] for g in r["by_source"]}
    q = r["quote_vs_sold"]
    assert (q["jobs"], q["quoted"], q["sold"], q["above"]) == (1, 500, 650, 1)
    b = r["ctm_bookings"]
    assert (b["total"], b["with_job"], b["lead_only"], b["none"]) == (3, 1, 1, 1)
    assert [x["phone"] for x in b["without_job"]] == ["(617) 555-0002", "(617) 555-0003"]


def test_flood_it_rollup_and_ticket_grid():
    r = build_jobs_report("Greater Boston", jobs(), [], activity(), START, TODAY)
    sources = {g["name"]: g for g in r["by_source"]}
    assert sources["Flood It"]["jobs"] == 3 and not any(n.startswith("Flood It -") for n in sources)
    f = r["flood_it"]
    assert (f["totals"]["sold"], f["totals"]["decided"], f["totals"]["sold_amount"]) == (2, 3, 4000)
    assert {g["name"]: g["jobs"] for g in f["by_source"]} == {"Flood It - Inquirly": 2, "Flood It - Service Direct": 1}
    assert {g["name"]: g["avg_ticket"] for g in f["by_service"]} == {"Water Damage": 3000, "Mold Mitigation": 1000}
    grid = r["ticket_grid"]
    assert grid["columns"] == ["Flood It", "Google"]
    water = next(x for x in grid["rows"] if x["service"] == "Water Damage")
    assert water["avg_ticket"] == 1825 and water["cells"]["Google"] == {"sold": 1, "avg_ticket": 650}
    assert water["cells"]["Flood It"] == {"sold": 1, "avg_ticket": 3000}
    close = {x["service"]: x for x in r["close_grid"]["rows"]}
    assert close["Water Damage"]["cells"]["Flood It"] == {"sold": 1, "decided": 2, "close_rate": 0.5}
    assert close["Mold Mitigation"]["cells"]["Flood It"]["close_rate"] == 1.0
    assert close["Water Damage"]["cells"]["Google"]["close_rate"] == 0.5  # sold, missing; two pending left out


def test_markdown_and_dashboard_tab():
    from app.dashboard import render_dashboard

    r = build_jobs_report("Greater Boston", jobs(), [], activity(), START, TODAY)
    md = render_jobs_markdown([r])
    for heading in ("Close rate by service", "Flood It", "Average ticket by service and source",
                    "Close rate by service and source", "Quote vs sold", "Sold amount missing", "CTM bookings with no Workiz job"):
        assert heading in md
    brief = build_brief(scenario(), DAY, now=ts(23))
    page = render_dashboard(brief, jobs=[r])
    assert "id='tab-jobs'" in page and "Close rate" in page and "(617) 555-0003" in page
    assert page.count("id='tab-jobs'") == 1
    assert "id='tab-jobs' hidden><div class='placeholder'>" in render_dashboard(brief)
