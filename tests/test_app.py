from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth import hash_password
from app.config import Settings
from app.models import Call, CallReview, User
from app.sync import upsert_call
from tests.conftest import ctm_call, json_response, make_client


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("recording.mp3"):
        return httpx.Response(200, content=b"ID3fake-audio", headers={"content-type": "audio/mpeg"})
    if "/calls/" in request.url.path and request.url.path.endswith(".json"):
        call_id = int(request.url.path.rsplit("/", 1)[1].split(".")[0])
        return json_response({"call": ctm_call(call_id, notes="from api")})
    return json_response({"calls": [], "total_pages": 1})


@pytest.fixture
def app_env(tmp_path):
    settings = Settings(
        ctm_access_key="k", ctm_secret_key="s", ctm_account_id="1234",
        database_url=f"sqlite:///{tmp_path / 'app.db'}", secret_key="test-secret",
        webhook_token="hook-token", timezone="UTC",
    )
    from app.main import create_app

    app = create_app(settings, client_factory=lambda: make_client(_handler), start_worker=False)
    session = db.new_session()
    now = int(datetime.now(timezone.utc).timestamp())
    upsert_call(session, ctm_call(1, unix_time=now - 3600))  # Alex (501)
    upsert_call(session, ctm_call(2, unix_time=now - 1800, agent={"id": 502, "name": "Sam"}))
    session.add_all([
        User(email="boss@example.com", name="Boss", role="manager", password_hash=hash_password("password1")),
        User(email="alex@example.com", name="Alex", role="agent", agent_id="501", password_hash=hash_password("password1")),
    ])
    session.commit()
    session.close()
    return app


def login(client: TestClient, email: str) -> None:
    resp = client.post("/login", data={"email": email, "password": "password1"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"


def test_requires_login_and_setup_is_locked(app_env):
    with TestClient(app_env) as client:
        resp = client.get("/calls", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
        bad = client.post("/login", data={"email": "boss@example.com", "password": "nope"}, follow_redirects=False)
        assert bad.headers["location"] == "/login"


def test_manager_pages_render(app_env):
    with TestClient(app_env) as client:
        login(client, "boss@example.com")
        for path in ("/", "/calls", "/calls/1", "/callbacks", "/reviews", "/agents", "/calls/export.csv"):
            assert client.get(path).status_code == 200, path
        assert "Alex Agent" in client.get("/calls").text
        assert client.get("/admin").status_code == 403


def test_agent_only_sees_own_calls(app_env):
    with TestClient(app_env) as client:
        login(client, "alex@example.com")
        assert client.get("/calls/1").status_code == 200
        assert client.get("/calls/2").status_code == 404
        assert client.get("/calls/2/recording").status_code == 404
        assert client.get("/agents").status_code == 403
        csv_text = client.get("/calls/export.csv?agent=502").text
        assert "\n1," in csv_text and "\n2," not in csv_text


def test_recording_is_proxied(app_env):
    with TestClient(app_env) as client:
        login(client, "alex@example.com")
        resp = client.get("/calls/1/recording")
        assert resp.status_code == 200
        assert resp.content == b"ID3fake-audio"
        assert resp.headers["content-type"] == "audio/mpeg"


def test_review_and_acknowledge_flow(app_env):
    with TestClient(app_env) as client:
        login(client, "boss@example.com")
        resp = client.post("/calls/1/reviews", data={"c_greeting": "5", "c_close": "3", "c_tone": "",
                                                      "feedback": "Great opener"}, follow_redirects=False)
        assert resp.status_code == 303
    with db.new_session() as session:
        review = session.query(CallReview).one()
        assert review.score == 80.0 and review.agent_id == "501"
        review_id = review.id
    with TestClient(app_env) as client:
        login(client, "alex@example.com")
        assert "Great opener" in client.get("/").text
        client.post(f"/reviews/{review_id}/ack", data={"next": "/"})
    with db.new_session() as session:
        assert session.get(CallReview, review_id).acknowledged_at is not None


def test_agents_cannot_review(app_env):
    with TestClient(app_env) as client:
        login(client, "alex@example.com")
        assert client.post("/calls/1/reviews", data={"c_greeting": "5"}).status_code == 403


def test_webhook_requires_token_and_fetches_from_api(app_env):
    with TestClient(app_env) as client:
        assert client.post("/webhooks/ctm?token=wrong", json={"id": 77}).status_code == 401
        resp = client.post("/webhooks/ctm?token=hook-token", json={"call": {"id": 77}})
        assert resp.status_code == 200 and resp.json()["call_id"] == 77
    with db.new_session() as session:
        assert session.get(Call, 77).notes == "from api"
