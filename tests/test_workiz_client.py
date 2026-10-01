from datetime import date

import httpx
import pytest

from app.workiz_client import WorkizClient, WorkizError, configured_markets


def client(handler):
    return WorkizClient("tok123", transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def test_jobs_paginate_by_page_number_with_token_in_path():
    seen = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["offset"])  # job/all: offset is a page number
        jobs = [{"UUID": f"J{page * 2 + i}"} for i in range(2 if page < 2 else 1)]
        return httpx.Response(200, json={"data": jobs, "has_more": page < 2})

    with client(handler) as c:
        jobs = list(c.iter_jobs(date(2026, 9, 1), page_size=2))

    assert [j["UUID"] for j in jobs] == ["J0", "J1", "J2", "J3", "J4"]
    assert [r.url.params["offset"] for r in seen] == ["0", "1", "2"]
    assert seen[0].url.path == "/api/v1/tok123/job/all/"
    assert seen[0].url.params["start_date"] == "2026-09-01" and seen[0].url.params["records"] == "2"
    assert seen[0].url.params["only_open"] == "false"


def test_leads_paginate_by_record_offset_and_stop_on_has_more():
    seen = []

    def handler(request):
        seen.append(request)
        offset = int(request.url.params["offset"])  # lead/all: offset counts records
        leads = [{"UUID": f"L{offset + i}"} for i in range(2)]
        return httpx.Response(200, json={"data": leads, "has_more": offset < 2})

    with client(handler) as c:
        leads = list(c.iter_leads(date(2026, 9, 1), page_size=2))

    assert [x["UUID"] for x in leads] == ["L0", "L1", "L2", "L3"]
    assert [r.url.params["offset"] for r in seen] == ["0", "2"]


def test_quota_429_is_retried_with_long_backoff():
    waits, calls = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": True, "msg": "Account reached Api Quotas"})
        return httpx.Response(200, json={"data": [{"id": 1}]})

    with WorkizClient("tok123", transport=httpx.MockTransport(handler), sleep=waits.append) as c:
        assert c.team() == [{"id": 1}]
    assert waits == [15]


def test_errors_do_not_leak_the_token():
    with client(lambda r: httpx.Response(401, json={"error": "bad token"})) as c:
        with pytest.raises(WorkizError) as err:
            c.team()
    assert "tok123" not in str(err.value) and err.value.status_code == 401


def test_configured_markets_reads_per_market_tokens():
    env = {"WORKIZ_TOKEN_BOSTON": "a", "WORKIZ_TOKEN_CHARLESTON": "", "OTHER": "x"}
    assert configured_markets(env) == {"Greater Boston": "a"}
    assert configured_markets({"WORKIZ_TOKEN_NORTH_DALLAS": "b"}) == {"North Dallas": "b"}
