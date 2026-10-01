from datetime import date

from app.brief import Activity
from app.pcc import (LOST_LEAD, NO_AGENT, NOT_IN_WORKIZ, OPEN_LEAD, SCHEDULED_LEAD, build_pcc_report,
                     render_pcc_html, render_pcc_markdown)
from tests.test_brief import TZ, inbound, rec, ts
from tests.test_jobs import job

TODAY = date(2026, 10, 1)


def booked(market="Greater Boston", value=None, **extra):
    sale = {"sale": {"conversion": True, "value": value}} if value else {}
    return {"custom_fields": {"lead_status": "Booked", "franchise_location": market}, **sale, **extra}


def activity():
    day = lambda d: ts(9, day=date(2026, 9, d))  # noqa: E731
    return Activity([
        inbound(1, day(10), "+16175550001", **booked(value="200")),   # sold
        inbound(2, day(11), "+16175550002", **booked(value="300")),   # Done at $0
        inbound(3, day(12), "+16175550003", **booked()),              # canceled
        inbound(4, day(13), "+16175550004", **booked()),              # appointment passed, still open
        inbound(5, day(25), "+16175550005", **booked()),              # scheduled ahead
        inbound(6, day(14), "+16175550006", **booked()),              # lead only
        inbound(7, day(15), "+16175550007", **booked()),              # nothing in Workiz
        inbound(8, day(16), "+16175550008", **booked()),              # job created too late to match
        rec(9, day(17), "+16175550009", direction="form", **booked()),  # form booking, no agent: sold
        rec(13, day(16), "+16175550013", direction="form", **booked()),  # form, then the agent books it next day
        inbound(14, day(17), "+16175550013", **booked(agent={"name": "Edgar Avila"})),
        inbound(10, day(10), "+18435550010", **booked("Charleston", value="150")),  # market not read
        inbound(11, day(10), "+16175550011", **booked("Out of Area")),               # not a call-center market
        inbound(12, ts(9, day=date(2026, 5, 1)), "+16175550012", **booked()),       # outside the window
        inbound(15, day(18), "+16175550015", email="Kim@Example.com", **booked()),  # job under another phone
        inbound(16, day(18), "+16175550016", name="Ana Ruiz", **booked()),          # job under her name
        inbound(17, day(20), "+16175550017", **booked()),   # job made 10 days before, appointment after
        inbound(18, day(20), "+16175550018", **booked()),   # old finished job only: no match
        inbound(19, day(21), "+16175550019", **booked()),   # scheduled lead
        inbound(20, day(21), "+16175550020", **booked()),   # lost lead
        inbound(21, day(21), "+16175550021", name="MACON GA", **booked()),  # caller ID is not a name
    ], TZ)


def boston():
    jobs = [
        job(1, "2026-09-10", "2026-09-12", "Done", 260, phone="(617) 555-0001", updated="2026-09-12"),
        job(2, "2026-09-11", "2026-09-13", "Done", 0, due=0, phone="617-555-0002"),
        job(3, "2026-09-12", "2026-09-14", "Canceled", 0, phone="6175550003"),
        job(4, "2026-09-13", "2026-09-20", "Submitted", 0, phone="6175550004"),
        job(5, "2026-09-26", "2026-10-08", "Submitted", 0, phone="6175550005"),
        job(8, "2026-10-01", "2026-10-03", "Submitted", 0, phone="6175550008"),  # 15 days after booking
        job(9, "2026-09-17", "2026-09-18", "Done", 400, phone="6175550009", updated="2026-09-18"),
        job(20, "2026-09-01", "2026-09-02", "Done", 999, phone="6175550099"),   # not a call-center booking
        job(15, "2026-09-18", "2026-09-19", "Done", 300, phone="7815550000", Email="kim@example.com",
            updated="2026-09-19"),
        job(16, "2026-09-19", "2026-10-05", "Submitted", 0, phone="7815550001", FirstName="Ana", LastName="Ruiz"),
        job(17, "2026-09-10", "2026-09-22", "Done", 500, phone="6175550017", updated="2026-09-22"),
        job(18, "2026-08-25", "2026-08-26", "Done", 700, phone="6175550018", updated="2026-08-26"),
        job(21, "2026-09-21", "2026-10-05", "Submitted", 0, phone="7815550002", FirstName="Macon", LastName="Ga"),
    ]
    leads = [{"Phone": "617-555-0006", "Status": "New"},
             {"Phone": "6175550019", "Status": "Scheduled", "SerialId": 77, "CreatedBy": "Sergio Vaca"},
             {"Phone": "6175550020", "Status": "Lost Bid/Lost Job"}]
    return jobs, leads


def report():
    return build_pcc_report(activity(), {"Greater Boston": boston()}, TODAY, days=90)


def test_each_booking_followed_into_workiz():
    r = report()
    got = {row["phone10"][-2:]: row["outcome"] for row in r["rows"]}
    assert got == {"01": "Sold", "02": "Done at $0", "03": "Canceled", "04": "Not closed out", "05": "Scheduled",
                   "06": OPEN_LEAD, "07": NOT_IN_WORKIZ, "08": NOT_IN_WORKIZ, "09": "Sold", "13": NOT_IN_WORKIZ,
                   "15": "Sold", "16": "Scheduled", "17": "Sold", "18": NOT_IN_WORKIZ, "19": SCHEDULED_LEAD,
                   "20": LOST_LEAD, "21": NOT_IN_WORKIZ}
    rows = {row["phone10"][-2:]: row for row in r["rows"]}
    assert rows["09"]["agent"] == NO_AGENT
    assert rows["13"]["agent"] == "Edgar Avila" and rows["13"]["booked"] == date(2026, 9, 17)
    assert "Charleston" in r["not_read"] and "Greater Boston" not in r["not_read"]
    assert (rows["15"]["matched_by"], rows["16"]["matched_by"], rows["17"]["matched_by"]) == ("email", "name", "phone")
    assert rows["19"]["lead"]["serial"] == 77


def test_rates():
    t = report()["total"]
    assert t["bookings"] == 17 and t["reached_workiz"] == 9
    assert t["close_rate"] == 4 / 7            # sold ÷ (sold, Done at $0, canceled, not closed out)
    assert t["sold_revenue"] == 1460
    assert t["matched_by"] == {"phone": 7, "email": 1, "name": 1}
    assert t["quote_pairs"] == 1 and t["sold_vs_quote"] == 260 / 200
    agents = {g["name"]: g["bookings"] for g in report()["by_agent"]}
    assert agents == {"Edgar Avila": 1, "Sergio Vaca": 15, NO_AGENT: 1}


def test_render():
    r = report()
    md = render_pcc_markdown(r)
    assert "# Call-center follow-through" in md and "Scheduled lead, never made a job" in md
    assert "| Greater Boston | 17 | 9 (53%) | 4 |" in md
    assert "Lead #77 Scheduled, by Sergio Vaca" in md and "(matched by email)" not in md  # sold jobs aren't listed
    page = render_pcc_html(r)
    assert page.startswith("<!doctype html>") and "Marked Done at $0 in Workiz" in page
