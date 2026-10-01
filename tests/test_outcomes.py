from collections import Counter
from datetime import date

from app.outcomes import (AWAITING, COMMERCIAL, LIKELY_SOLD, MOVED, NO_EVIDENCE, NO_EVIDENCE_PRICED, NOT_SOLD, NOTES_DONE,
                          PAID, booked_outcomes, by_client, close_rates, open_job_evidence)
from tests.test_jobs import job

TODAY = date(2026, 10, 1)


def open_job(serial, total=0, due=None, client=1, created="2026-09-10", **extra):
    return job(serial, created, "2026-09-12", "Submitted", total, due=total if due is None else due, ClientId=client,
               **extra)


def evidence(j, others=()):
    return open_job_evidence(j, [j, *others])


def test_evidence_order():
    assert evidence(open_job(1, 300, due=0)) == PAID
    assert evidence(open_job(2, JobNotes="Customer was a no-show, will call back")) == NOT_SOLD
    assert evidence(open_job(3, Tags=["Estimate Pending Customer Approval"])) == AWAITING
    assert evidence(open_job(4, Status="Pending", SubStatus="Waiting on Auth")) == AWAITING
    assert evidence(open_job(5, Comments="Estimates have been written and emailed")) == AWAITING
    assert evidence(open_job(6), [open_job(7, 2500, created="2026-09-14")]) == MOVED
    assert evidence(open_job(8), [open_job(9, 2500, created="2026-09-01")]) == NO_EVIDENCE  # earlier job doesn't count
    unit_jobs = [open_job(20 + i, 95, created=f"2026-09-0{i + 1}") for i in range(5)]
    assert evidence(unit_jobs[0], unit_jobs[1:]) == COMMERCIAL
    assert evidence(open_job(10, JobNotes="Job completed, customer happy")) == NOTES_DONE
    assert evidence(open_job(11, 2070, LineItems=[{"Name": "Basement Extraction"}, {"Name": "Dumpster load"}])) == LIKELY_SOLD
    assert evidence(open_job(12, 165, LineItems=[{"Name": "Minimum Base Cleaning Fee"}])) == NO_EVIDENCE_PRICED
    assert evidence(open_job(13, JobNotes="Two rooms and a hallway, a few stains")) == NO_EVIDENCE


def test_close_rate_range():
    jobs = [
        job(1, "2026-09-01", "2026-09-02", "Done", 200, updated="2026-09-02", ClientId=1),
        job(2, "2026-09-01", "2026-09-02", "Canceled", 0, ClientId=2),
        open_job(3, 300, due=0, client=3),                                   # paid: sold
        open_job(4, 900, client=4, LineItems=[{"Name": "Water extraction"}]),  # likely sold
        open_job(5, 165, client=5),                                          # priced, no evidence
        open_job(6, client=6),                                               # $0, no evidence
        open_job(7, client=7, Tags=["Estimate"], Comments="estimate sent"),   # awaiting: left out
        job(8, "2026-09-20", "2026-10-09", "Submitted", 0, ClientId=8),      # ahead: pending
    ]
    counts = booked_outcomes(jobs, by_client(jobs), TODAY)
    assert counts == Counter({"sold": 1, "canceled": 1, PAID: 1, LIKELY_SOLD: 1, NO_EVIDENCE_PRICED: 1, NO_EVIDENCE: 1,
                              AWAITING: 1, "pending": 1})
    r = close_rates(counts)
    assert r["decided"] == 6 and r["sold"] == 2
    assert (r["low"], r["best"], r["high"]) == (2 / 6, 3 / 6, 4 / 6)
