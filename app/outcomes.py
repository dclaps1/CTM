"""What most likely happened to a Workiz job that is still open after its appointment.

Locations often leave jobs open after the visit instead of marking them Done or Canceled, and we cannot change how
they use Workiz. So an open job is judged from the evidence already on it, in this order:

1. Paid: a payment is recorded against it (amount due below the total). Counted as sold.
2. Not sold: its notes, comments, tags or substatus say canceled, no-show, declined, lost, and the like.
3. Commercial account: the client has 5 or more jobs from 120 days before this one on (apartment units, property
   managers). Routine unit jobs; left out of close rates.
4. Awaiting decision: an estimate or proposal was sent, or it waits on approval, insurance or an adjuster (or the
   job status is Pending). Left out of close rates.
5. Moved to a later job: the same client has a priced job created on or after this one, so this was an inspection,
   estimate or duplicate and the sale sits on the other job. Left out of close rates.
6. Notes say done: the notes say the job was completed or paid in full. Counted as sold.
7. Likely sold: priced, with line items for on-site work (extraction, demo, equipment, labor hours, ...).
8. No evidence, priced: priced (often the call center's quote), nothing else.
9. No evidence, $0.

These rules were checked against a blind review of 120 open jobs (Oct 2026): estimate, commercial and not-sold calls
agreed 14-15 of 15. Close rate is given as a range: low counts only proven sales (Done with a price, paid, notes say
done); best adds likely sold; high also adds priced jobs with no other evidence. $0 jobs with no evidence count as not
sold in all three.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from app.jobs import _day, _status, amount, outcome

PAID, NOT_SOLD, COMMERCIAL, AWAITING, MOVED, NOTES_DONE, LIKELY_SOLD, NO_EVIDENCE_PRICED, NO_EVIDENCE = (
    "paid", "not_sold", "commercial", "awaiting", "moved", "notes_done", "likely_sold", "no_evidence_priced",
    "no_evidence")
LABELS = {
    PAID: "Paid, not closed out",
    NOT_SOLD: "Notes say canceled / no-show / declined",
    COMMERCIAL: "Commercial account unit job",
    AWAITING: "Estimate sent / awaiting approval",
    MOVED: "Work moved to a later job",
    NOTES_DONE: "Notes say job completed",
    LIKELY_SOLD: "On-site work line items, unpaid",
    NO_EVIDENCE_PRICED: "Priced, no other evidence",
    NO_EVIDENCE: "$0, no evidence",
}
LEFT_OUT = (COMMERCIAL, AWAITING, MOVED)
COMMERCIAL_JOBS = 5
COMMERCIAL_DAYS = 120

_NOT = re.compile(r"no.?show|\bcancel|declin|not interested|too (expensive|high|much)|went with|\blost\b|won.?t (need|be)"
                  r"|didn.?t want|refus|not home|postpon|chose another|no work (needed|done)")
_AWAITING = re.compile(r"estimate (sent|emailed|written|pending|submitted|delivered|uploaded)|estimates have been"
                       r"|waiting (for|on) (estimate|approval|auth|adjuster|insurance|work auth)|pending (customer )?approval"
                       r"|proposal sent|quote sent|bid sent|awaiting (approval|auth)|adjuster")
_DONE = re.compile(r"job (was |is )?completed|work (was |is )?completed|completed (the|this) job|paid in full"
                   r"|payment (received|collected)|finished the job")
_ONSITE = re.compile(r"extract|demo|dumpster|debris|tear ?out|remov(e|al) of|dehumid|air mover|air scrubber|equipment"
                     r"|monitor|anti-?microbial|containment|labor|hour|after.?hours|emergency service|drying"
                     r"|moisture map|haul|disposal|pack ?out|board ?up")


def _text(job: dict) -> str:
    tags = " ".join(str(t) for t in job.get("Tags") or [])
    return " ".join(str(job.get(k) or "") for k in ("JobNotes", "Comments", "SubStatus")).lower() + " " + tags.lower()


def open_job_evidence(job: dict, client_jobs: list[dict]) -> str:
    """Evidence key for an open job; `client_jobs` are all of the client's jobs in that account (this one included)."""
    total = amount(job.get("JobTotalPrice"))
    if total - amount(job.get("JobAmountDue")) >= 1:
        return PAID
    text = _text(job)
    if _NOT.search(text):
        return NOT_SOLD
    created = _day(job.get("CreatedDate")) or date.min
    since = created - timedelta(days=COMMERCIAL_DAYS)
    if sum((_day(o.get("CreatedDate")) or date.min) >= since for o in client_jobs) >= COMMERCIAL_JOBS:
        return COMMERCIAL
    if _AWAITING.search(text) or _status(job) == "pending":
        return AWAITING
    if any(o.get("UUID") != job.get("UUID") and (_day(o.get("CreatedDate")) or date.min) >= created
           and amount(o.get("JobTotalPrice")) > 0 for o in client_jobs):
        return MOVED
    if _DONE.search(text):
        return NOTES_DONE
    lines = " ".join(str(li.get("Name") or "") for li in job.get("LineItems") or [] if isinstance(li, dict)).lower()
    if total > 0 and _ONSITE.search(lines):
        return LIKELY_SOLD
    return NO_EVIDENCE_PRICED if total > 0 else NO_EVIDENCE


def by_client(jobs: list[dict]) -> dict:
    out: dict = {}
    for j in jobs:
        out.setdefault(j.get("ClientId") or j.get("UUID"), []).append(j)
    return out


def booked_outcomes(booked: list[dict], clients: dict, today: date) -> Counter:
    """Count booked jobs by outcome: sold, canceled, done0 (Done at $0), pending, or an evidence key for open jobs."""
    out: Counter = Counter()
    for j in booked:
        o = outcome(j, today)
        if o == "missing":
            o = "done0" if _status(j).startswith("done") else \
                open_job_evidence(j, clients.get(j.get("ClientId") or j.get("UUID"), [j]))
        out[o] += 1
    return out


def close_rates(counts: Counter) -> dict:
    """Low / best / high close rate from booked_outcomes counts (see module notes)."""
    proven = counts["sold"] + counts[PAID] + counts[NOTES_DONE]
    decided = proven + sum(counts[k] for k in ("canceled", "done0", NOT_SOLD, LIKELY_SOLD, NO_EVIDENCE_PRICED,
                                                NO_EVIDENCE))
    if not decided:
        return {"low": None, "best": None, "high": None, "decided": 0, "sold": proven}
    return {"low": proven / decided, "best": (proven + counts[LIKELY_SOLD]) / decided,
            "high": (proven + counts[LIKELY_SOLD] + counts[NO_EVIDENCE_PRICED]) / decided,
            "decided": decided, "sold": proven}
