from datetime import date

from app.brief import Activity, build_brief
from app.jobs import build_jobs_report, completed_on, outcome, render_jobs_markdown
from tests.test_brief import DAY, TZ, inbound, scenario, ts

TODAY = date(2026, 10, 1)
BOSTON = {"custom_fields": {"lead_status": "Booked", "franchise_location": "Greater Boston"}}


def job(serial, created, appt, status="Submitted", total=0, due=None, phone="", service="Water Damage",
        source="Google", updated=None, **extra):
    return {"UUID": f"U{serial}", "SerialId": serial, "CreatedDate": f"{created} 09:00:00",
            "JobDateTime": f"{appt} 10:00:00", "Status": status, "JobTotalPrice": total,
            "JobAmountDue": total if due is None and not status.startswith("Done") else (due or 0),
            "LastStatusUpdate": f"{updated or appt} 12:00:00", "Phone": phone, "JobType": service,
            "JobSource": source, "FirstName": "pat", "Team": [{"Name": "Dan"}], **extra}


def activity():
    return Activity([
        inbound(1, ts(9, day=date(2026, 9, 10)), "+16175550001", **BOSTON),  # booked -> has a job
        inbound(2, ts(9, day=date(2026, 9, 12)), "+16175550002", **BOSTON),  # booked -> lead only
        inbound(3, ts(9, day=date(2026, 9, 14)), "+16175550003", **BOSTON),  # booked -> nothing in Workiz
        inbound(4, ts(9, day=date(2026, 9, 14)), "+18435550004",
                custom_fields={"lead_status": "Booked", "franchise_location": "Charleston"}),
    ], TZ)


def jobs():
    return [
        # completed in the last 30 days
        job(1, "2026-09-10", "2026-09-11", "Done", 1000, phone="(617) 555-0001", updated="2026-09-20"),
        job(2, "2026-09-05", "2026-09-15", "done pending approval", 3000, due=3000, service="Mold Mitigation",
            source="Flood It - Service Direct", updated="2026-09-16", insurance_company1="Acme"),
        # completed in the 30 days before
        job(3, "2026-08-01", "2026-08-02", "Done", 2000, updated="2026-08-20"),
        # owed for a long time
        job(4, "2026-06-01", "2026-06-02", "done pending approval", 5000, due=5000, updated="2026-06-10",
            source="Flood It - Inquirly"),
        job(5, "2026-09-12", "2026-09-14", "Canceled", 400, due=400),        # canceled, balance still showing
        job(6, "2026-09-13", "2026-09-20", "Submitted", 0),                 # appointment passed, still open
        job(7, "2026-09-20", "2026-10-09", "Submitted", 249, service="Carpet Cleaning"),  # future: pending
        job(8, "2026-09-01", "2026-09-02", "Done", 0, due=-1323, updated="2026-09-03"),  # paid, $0 revenue
    ]


def report():
    return build_jobs_report("Greater Boston", jobs(), [{"Phone": "617-555-0002"}], activity(), TODAY)


def test_outcome_and_completion():
    js = {j["SerialId"]: j for j in jobs()}
    assert [outcome(j, TODAY) for j in jobs()] == [
        "sold", "sold", "sold", "sold", "canceled", "missing", "pending", "missing"]
    assert completed_on(js[2]) == date(2026, 9, 16) and completed_on(js[6]) is None
    assert outcome(job(9, "2026-09-20", "2026-10-05", "In progress", 699), TODAY) == "pending"


def test_headline_rates_and_owed():
    r = report()
    h = r["headline"]
    assert h["revenue"] == {"now": 4000, "prior": 2000} and h["completed"] == {"now": 2, "prior": 1}
    assert h["booked"]["now"] == 5  # jobs 1, 2, 5, 6, 7 created since Sep 2 (job 8 on Sep 1 is just before)
    rt = r["rates"]
    assert (rt["sold"], rt["decided"]) == (3, 6)  # 1, 2, 3 sold; 5 canceled; 6, 8 not closed out; 7 pending
    assert rt["avg_ticket"] == 2000 and rt["days_to_visit"] is not None
    o = r["owed"]
    assert o["total"] == 8000 and o["late_total"] == 5000 and [x["serial"] for x in o["late"]] == [4]
    assert r["monthly"][-1] == {"month": "2026-10", "label": "Oct", "partial": True, "revenue": 0, "jobs": 0}
    assert r["monthly"][-2]["revenue"] == 4000
    assert h["note"].startswith("Big jobs swing these numbers: one job (#2, Mold Mitigation, $3,000) is 75%")


def test_breakdowns_cleanup_and_actions():
    r = report()
    sources = {s["name"]: s for s in r["by_source"]}
    # job 4 (Inquirly) is older than 90 days, so Flood It has one source and no sub-rows
    assert sources["Flood It"]["booked"] == 1 and sources["Flood It"]["parts"] == []
    two = build_jobs_report("Greater Boston", jobs() + [job(9, "2026-09-25", "2026-09-26", "Canceled",
                                                            source="Flood It - Inquirly")], [], activity(), TODAY)
    parts = next(s for s in two["by_source"] if s["name"] == "Flood It")["parts"]
    assert sorted(p["name"] for p in parts) == ["Flood It - Inquirly", "Flood It - Service Direct"]
    c = r["cleanup"]
    assert [x["serial"] for x in c["not_closed"]] == [6]
    assert [x["serial"] for x in c["paid_no_revenue"]] == [8] and c["paid_no_revenue"][0]["collected"] == 1323
    assert [x["serial"] for x in c["canceled_balance"]] == [5]
    assert [b["phone"] for b in r["ctm"]["missing"]] == ["(617) 555-0002", "(617) 555-0003"]
    text = " ".join(r["actions"])
    assert "Collect $5,000" in text and "paid ($1,323)" in text and "Close out 1 jobs" in text
    assert "canceled" in text and "2 call-center bookings" in text


def test_markdown_dashboard_and_print_page():
    from app.dashboard import render_dashboard, render_jobs_report

    r = report()
    md = render_jobs_markdown([r])
    for heading in ("Needs attention", "Revenue by month", "By service", "By lead source", "Money owed"):
        assert heading in md
    page = render_dashboard(build_brief(scenario(), DAY, now=ts(23)), jobs=[r])
    assert page.count("id='tab-jobs'") == 1 and "Needs attention" in page and "(617) 555-0003" in page
    printable = render_jobs_report([r, {**r, "market": "Charleston"}])
    assert "All locations" in printable and "@page" in printable and "<svg" in printable


def test_small_sources_fold_into_other():
    extra = [job(20 + i, "2026-09-25", "2026-09-26", "Canceled", source=s) for i, s in enumerate(("Angi", "Yelp"))]
    r = build_jobs_report("Greater Boston", jobs() + extra, [], activity(), TODAY)
    names = [s["name"] for s in r["by_source"]]
    assert "Other sources (2)" in names and "Angi" not in names
    other = next(s for s in r["by_source"] if s["name"] == "Other sources (2)")
    assert other["folded"] == ["Angi", "Yelp"] and other["booked"] == 2
