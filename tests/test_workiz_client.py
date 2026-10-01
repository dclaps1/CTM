from datetime import date

import httpx
import pytest

from app.workiz_client import WorkizClient, WorkizError, configured_markets


def client(handler):
    return WorkizClient("tok123", transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def test_jobs_paginate_with_token_in_path():
    seen = []

    def handler(request):
        seen.append(request)
        offset = int(request.url.params["offset"])
        jobs = [{"UUID": f"J{offset + i}", "JobTotalPrice": 250} for i in range(2 if offset < 4 else 1)]
        return httpx.Response(200, json={"data": jobs})

    with client(handler) as c:
        jobs = list(c.iter_jobs(date(2026, 9, 1), page_size=2))

    assert [j["UUID"] for j in jobs] == ["J0", "J1", "J2", "J3", "J4"]
    assert seen[0].url.path == "/api/v1/tok123/job/all/"
    assert seen[0].url.params["start_date"] == "2026-09-01" and seen[0].url.params["records"] == "2"


def test_errors_do_not_leak_the_token():
    with client(lambda r: httpx.Response(401, json={"error": "bad token"})) as c:
        with pytest.raises(WorkizError) as err:
            c.team()
    assert "tok123" not in str(err.value) and err.value.status_code == 401


def test_configured_markets_reads_per_market_tokens():
    env = {"WORKIZ_TOKEN_BOSTON": "a", "WORKIZ_TOKEN_CHARLESTON": "", "OTHER": "x"}
    assert configured_markets(env) == {"Greater Boston": "a"}
