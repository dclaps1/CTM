from datetime import date

import httpx
import pytest

from app.ctm_client import CTMError
from tests.conftest import ctm_call, json_response, make_client


def test_iter_calls_paginates_with_basic_auth_and_dates():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = int(request.url.params["page"])
        calls = [ctm_call(page * 10 + i) for i in range(2)]
        return json_response({"calls": calls, "page": page, "total_pages": 3})

    with make_client(handler) as client:
        calls = list(client.iter_calls(date(2025, 9, 1), date(2025, 9, 7)))

    assert len(calls) == 6
    assert [r.url.params["page"] for r in seen] == ["1", "2", "3"]
    first = seen[0]
    assert first.url.path == "/api/v1/accounts/1234/calls.json"
    assert first.url.params["start_date"] == "2025-09-01"
    assert first.url.params["end_date"] == "2025-09-07"
    assert first.headers["authorization"].startswith("Basic ")


def test_retries_rate_limits_then_raises_on_client_errors():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return json_response({"users": [{"id": 1, "first_name": "A", "last_name": "B"}], "total_pages": 1})

    with make_client(handler) as client:
        assert len(list(client.iter_users())) == 1
    assert attempts["n"] == 3

    with make_client(lambda r: httpx.Response(401, text="bad key")) as client:
        with pytest.raises(CTMError) as exc:
            client.get_call(1)
    assert exc.value.status_code == 401


def test_record_sale_clamps_score():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        return json_response({"status": "success"})

    with make_client(handler) as client:
        client.record_sale(99, name="QA review", score=9, conversion=True, value=100.0)
    assert captured["path"] == "/api/v1/accounts/1234/calls/99/sale"
    assert "score=5" in captured["body"] and "conversion=1" in captured["body"]


def test_recordings_only_fetched_from_ctm_hosts():
    def handler(request):
        return httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg"})

    with make_client(handler) as client:
        resp = client.open_recording("https://app.calltrackingmetrics.com/x/recording.mp3")
        assert resp.read() == b"ID3audio"
        resp.close()
        for bad in ("https://evil.example.com/a.mp3", "http://app.calltrackingmetrics.com/a.mp3",
                    "https://calltrackingmetrics.com.evil.com/a.mp3"):
            with pytest.raises(CTMError):
                client.open_recording(bad)
