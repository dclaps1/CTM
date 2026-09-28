from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app.metrics import CallFilters, leaderboard, missed_callbacks, summarize, call_query
from app.models import Agent, Call
from app.sync import get_state, normalize_call, run_sync, upsert_call
from tests.conftest import ctm_call, json_response, make_client

TZ = ZoneInfo("America/New_York")


def test_normalize_call_maps_fields():
    data = normalize_call(ctm_call(7))
    assert data["id"] == 7
    assert data["answered"] is True and data["voicemail"] is False
    assert data["agent_id"] == "501"
    assert data["tags"] == "new lead,franchise"
    assert data["sale_conversion"] is True and data["sale_value"] == 250.0
    assert data["called_at"] == datetime(2025, 9, 16, 5, 27)  # unix_time wins, stored as UTC

    no_unix = normalize_call(ctm_call(8, unix_time=None, called_at="2025-09-16 01:20 PM -04:00"))
    assert no_unix["called_at"] == datetime(2025, 9, 16, 17, 20)

    vm = normalize_call(ctm_call(9, dial_status="voicemail", talk_time=30, agent=None))
    assert vm["answered"] is False and vm["voicemail"] is True and vm["agent_id"] is None

    assert normalize_call(ctm_call(10, direction="msg_inbound")) is None
    assert normalize_call({"no": "id"}) is None


def test_upsert_is_idempotent_and_creates_agents(session):
    upsert_call(session, ctm_call(1))
    upsert_call(session, ctm_call(1, notes="called back"))
    upsert_call(session, ctm_call(2, agent={"id": 501, "name": "Alex A."}))
    session.commit()
    assert session.query(Call).count() == 2
    assert session.get(Call, 1).notes == "called back"
    assert session.get(Agent, "501").name == "Alex A."


def test_run_sync_moves_cursor(session):
    requested = []

    def handler(request):
        if request.url.path.endswith("users.json"):
            return json_response({"users": [{"id": 501, "first_name": "Alex", "last_name": "Agent"}], "total_pages": 1})
        requested.append((request.url.params["start_date"], request.url.params["end_date"]))
        return json_response({"calls": [ctm_call(1), ctm_call(2, direction="sms")], "total_pages": 1})

    with make_client(handler) as client:
        result = run_sync(session, client, initial_days=30, today=date(2025, 9, 20))
        assert result["calls"] == 1 and result["agents"] == 1
        assert requested[-1] == ("2025-08-21", "2025-09-21")
        assert get_state(session, "calls_synced_through") == "2025-09-20"
        run_sync(session, client, today=date(2025, 9, 22))
        assert requested[-1] == ("2025-09-18", "2025-09-23")  # cursor minus overlap


def _add(session, call_id, when, **kw):
    upsert_call(session, ctm_call(call_id, unix_time=int(when.timestamp()), **kw))


def test_summary_leaderboard_and_callbacks(session):
    day = datetime(2025, 9, 16, 14, 0, tzinfo=TZ)
    _add(session, 1, day)  # answered by Alex
    _add(session, 2, day.replace(hour=15), dial_status="no answer", talk_time=0, agent=None,
         caller_number="+15555550111", sale=None)  # missed, never returned
    _add(session, 3, day.replace(hour=16), dial_status="no answer", talk_time=0, agent=None,
         caller_number="+15555550122", sale=None)  # missed, then called back
    _add(session, 4, day.replace(hour=17), direction="outbound", caller_number="+1 (555) 555-0122",
         agent={"id": 502, "name": "Sam"}, sale=None)
    session.commit()

    filters = CallFilters(date(2025, 9, 16), date(2025, 9, 16))
    calls = session.scalars(call_query(filters, TZ)).all()
    s = summarize(calls, filters, TZ)
    assert (s["total"], s["inbound"], s["outbound"], s["missed"]) == (4, 3, 1, 2)
    assert round(s["answer_rate"], 3) == round(1 / 3, 3)
    assert s["by_hour"][14]["answered"] == 1 and s["by_hour"][15]["missed"] == 1
    assert s["conversions"] == 1 and s["revenue"] == 250

    board = {r["agent_id"]: r for r in leaderboard(session, calls, filters, TZ)}
    assert board["501"]["answered"] == 1 and board["502"]["outbound"] == 1

    rows = missed_callbacks(session, filters, TZ)
    status = {r["call"].id: (r["followup"].id if r["followup"] else None) for r in rows}
    assert status == {2: None, 3: 4}
    assert rows[0]["call"].id == 2  # open callbacks first

    missed_only = session.scalars(call_query(CallFilters(date(2025, 9, 16), date(2025, 9, 16), outcome="missed"), TZ)).all()
    assert {c.id for c in missed_only} == {2, 3}
    search = session.scalars(call_query(CallFilters(date(2025, 9, 16), date(2025, 9, 16), q="555-0111"), TZ)).all()
    assert [c.id for c in search] == [2]
