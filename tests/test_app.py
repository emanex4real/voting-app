"""
Tests for the Flask web service.

Runs against a temporary SQLite file (not Postgres) and a fake in-memory
Redis (fakeredis), so these run fast with no external services needed —
suitable for CI. The real Postgres/Redis integration is what Phase 2/3
set up for actual deployment; these tests just prove the app logic itself
is correct.
"""

import os
import tempfile

import pytest
import fakeredis

# Point at a throwaway SQLite file BEFORE importing app, since app.py
# reads DATABASE_URL at import time.
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["SECRET_KEY"] = "test-secret"

import app as app_module  # noqa: E402  (must come after env vars are set)


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    # Swap the real Redis client for a fake one so no Redis server is needed.
    app_module.redis_client = fakeredis.FakeStrictRedis()

    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
        option = app_module.Option(name="Test Option")
        app_module.db.session.add(option)
        app_module.db.session.commit()

    with app_module.app.test_client() as test_client:
        yield test_client


def register(client, username="alice", password="pw123"):
    return client.post(
        "/register",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def login(client, username="alice", password="pw123"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_register_creates_user(client):
    register(client)
    with app_module.app.app_context():
        user = app_module.User.query.filter_by(username="alice").first()
        assert user is not None
        assert user.check_password("pw123")


def test_cannot_register_duplicate_username(client):
    register(client)
    resp = register(client)
    assert b"already taken" in resp.data


def test_login_with_correct_credentials(client):
    register(client)
    resp = login(client)
    assert resp.status_code == 200
    assert b"Welcome back" in resp.data


def test_login_with_wrong_password_fails(client):
    register(client)
    resp = client.post(
        "/login",
        data={"username": "alice", "password": "wrong"},
        follow_redirects=True,
    )
    assert b"Invalid username or password" in resp.data


def test_vote_requires_login(client):
    resp = client.get("/vote", follow_redirects=True)
    assert b"Please log in" in resp.data


def test_casting_a_vote_queues_a_job(client):
    register(client)
    login(client)

    with app_module.app.app_context():
        option = app_module.Option.query.first()

    resp = client.post(
        "/vote", data={"option_id": option.id}, follow_redirects=True
    )
    assert b"Your vote has been recorded" in resp.data

    # The job should now be sitting on the Redis queue, ready for the worker.
    assert app_module.redis_client.llen(app_module.VOTE_QUEUE_KEY) == 1

    with app_module.app.app_context():
        user = app_module.User.query.filter_by(username="alice").first()
        assert user.has_voted is True


def test_cannot_vote_twice(client):
    register(client)
    login(client)
    with app_module.app.app_context():
        option = app_module.Option.query.first()

    client.post("/vote", data={"option_id": option.id}, follow_redirects=True)
    resp = client.post(
        "/vote", data={"option_id": option.id}, follow_redirects=True
    )
    assert b"already voted" in resp.data
    # Still only one job queued, not two.
    assert app_module.redis_client.llen(app_module.VOTE_QUEUE_KEY) == 1


def test_results_page_blocked_for_non_admin(client):
    register(client)
    login(client)
    resp = client.get("/results", follow_redirects=True)
    assert b"Admin access required" in resp.data
