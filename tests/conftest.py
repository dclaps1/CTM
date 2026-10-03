from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app import db
from app.ctm_client import CTMClient


def ctm_call(call_id: int, **overrides) -> dict:
    """A CTM call object shaped like the /calls.json API response."""
    base = {
        "id": call_id,
        "direction": "inbound",
        "unix_time": 1_758_000_000 + call_id * 60,
        "called_at": "2025-09-16 01:20 AM -04:00",
        "dial_status": "answered",
        "duration": 185,
        "talk_time": 170,
        "ring_time": 15,
        "caller_number": "+15555550100",
        "name": "Jane Caller",
        "city": "Austin",
        "state": "TX",
        "tracking_number": "+15125550000",
        "tracking_label": "Website",
        "source": "Google Organic",
        "receiving_number": "+15125559999",
        "agent": {"id": 501, "name": "Alex Agent", "email": "alex@example.com"},
        "tag_list": ["new lead", "franchise"],
        "is_new_caller": True,
        "sale": {"score": 4, "conversion": 1, "value": 250},
        "audio": f"https://app.calltrackingmetrics.com/accounts/1/calls/{call_id}/recording.mp3",
    }
    base.update(overrides)
    return base


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> CTMClient:
    return CTMClient("key", "secret", 1234, transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload), headers={"content-type": "application/json"})


@pytest.fixture
def session(tmp_path):
    db.configure(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db()
    s = db.new_session()
    yield s
    s.close()
