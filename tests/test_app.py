import os
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import mongomock
import pytest
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from app import create_app


@pytest.fixture
def app():
    uri = os.environ.get("TEST_MONGO_URI")
    client = MongoClient(uri) if uri else mongomock.MongoClient()
    db = client["test_" + uuid.uuid4().hex]
    application = create_app({"TESTING": True, "SECRET_KEY": "test-only-" * 8}, db)
    yield application
    client.drop_database(db.name)
    client.close()


def post(client, path, **data):
    client.get("/login")
    with client.session_transaction() as session:
        token = session["csrf"]
    return client.post(path, data={"csrf": token, **data})


def account(app, name="alice"):
    client = app.test_client()
    assert post(client, "/register", username=name, password="a-strong-test-password").status_code == 302
    assert post(client, "/login", username=name, password="a-strong-test-password").status_code == 302
    return client


def task(app, client, title="Ship the project"):
    assert post(client, "/tasks", title=title, priority="high").status_code == 302
    with client.session_transaction() as s:
        return app.extensions["db"].tasks.find_one({"owner": s["user_id"]})


def test_secret_required():
    with pytest.raises(ValueError):
        create_app({"SECRET_KEY": "short"}, mongomock.MongoClient().db)


def test_passwords_are_hashed_and_login_checked(app):
    c = account(app)
    user = app.extensions["db"].users.find_one()
    assert user["password_hash"].startswith("scrypt:")
    post(c, "/logout")
    assert post(c, "/login", username="alice", password="wrong").status_code == 401
    assert post(c, "/login", username="missing", password="wrong").status_code == 401


def test_case_insensitive_unique_username(app):
    c = account(app)
    assert post(c, "/register", username="ALICE", password="a-strong-test-password").status_code == 409


@pytest.mark.parametrize("username,password", [("a", "long-password"), ("bad name", "long-password"), ("valid", "short")])
def test_invalid_registration(app, username, password):
    assert post(app.test_client(), "/register", username=username, password=password).status_code == 400


def test_csrf_and_logout_method(app):
    c = account(app)
    assert c.post("/tasks", data={"title": "forged"}).status_code == 400
    assert c.post("/tasks", data={"title": "forged", "csrf": "é"}).status_code == 400
    assert c.get("/logout").status_code == 405
    assert app.extensions["db"].tasks.count_documents({}) == 0


def test_session_token_rotated(app):
    c = app.test_client()
    post(c, "/register", username="alice", password="a-strong-test-password")
    with c.session_transaction() as s:
        previous = s["csrf"]
    post(c, "/login", username="alice", password="a-strong-test-password")
    with c.session_transaction() as s:
        assert s["csrf"] != previous


def test_account_isolation(app):
    alice, bob = account(app), account(app, "bob")
    t = task(app, alice, "Alice private record")
    assert b"Alice private record" not in bob.get("/").data
    for action in ["complete", "reopen", "delete"]:
        assert post(bob, f"/tasks/{t['_id']}/{action}", version="1").status_code == 404
    assert app.extensions["db"].tasks.find_one()["done"] is False


def test_stale_edits_rejected(app):
    c = account(app)
    t = task(app, c)
    assert post(c, f"/tasks/{t['_id']}/complete", version="1").status_code == 302
    assert post(c, f"/tasks/{t['_id']}/reopen", version="1").status_code == 409
    assert post(c, f"/tasks/{t['_id']}/delete", version="1").status_code == 409
    assert post(c, f"/tasks/{t['_id']}/reopen", version="2").status_code == 302
    assert post(c, f"/tasks/{t['_id']}/delete", version="3").status_code == 302
    assert app.extensions["db"].tasks.count_documents({}) == 0


def test_render_escapes_user_input(app):
    c = account(app)
    task(app, c, "<script>alert(1)</script>")
    response = c.get("/")
    assert b"<script>" not in response.data
    assert b"&lt;script&gt;" in response.data
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Request-ID"]


def test_pagination_and_status_filter(app):
    c = account(app)
    with c.session_transaction() as s:
        owner = s["user_id"]
    app.extensions["db"].tasks.insert_many([dict(owner=owner, title=f"Task {i}", done=i==0,
        priority="normal", version=1, created_at=datetime.now(timezone.utc)) for i in range(23)])
    assert c.get("/").data.count(b'class="task ') == 20
    assert c.get("/?page=2").data.count(b'class="task ') == 3
    assert c.get("/?status=done").data.count(b'class="task ') == 1
    assert c.get("/?page=0").status_code == 400


def test_invalid_task_inputs(app):
    c = account(app)
    assert post(c, "/tasks", title=" ").status_code == 400
    assert post(c, "/tasks", title="x"*201).status_code == 400
    assert post(c, "/tasks/not-an-id/delete", version="1").status_code == 404
    t = task(app, c)
    assert post(c, f"/tasks/{t['_id']}/complete", version="abc").status_code == 400


def test_health_separates_process_and_database(app):
    c = app.test_client()
    with patch.object(app.extensions["db"], "command", side_effect=ConnectionFailure("sensitive internal detail")):
        assert c.get("/health/live").status_code == 200
        r = c.get("/health/ready")
        assert r.status_code == 503
        assert b"sensitive" not in r.data


def test_database_failure_is_not_a_success(app):
    c = account(app)
    with patch.object(type(app.extensions["db"].tasks), "insert_one", side_effect=ConnectionFailure("secret")):
        r = post(c, "/tasks", title="Cannot save")
        assert r.status_code == 503
        assert b"secret" not in r.data


def test_signed_session_shared_between_replicas(app):
    c = account(app)
    t = task(app, c)
    second = create_app(app.config, app.extensions["db"]).test_client()
    second.set_cookie("session", c.get_cookie("session").value)
    assert t["title"].encode() in second.get("/").data


def test_simultaneous_edits_have_one_winner(app):
    from concurrent.futures import ThreadPoolExecutor
    c = account(app)
    t = task(app, c)
    cookie = c.get_cookie("session").value
    with c.session_transaction() as s:
        token = s["csrf"]
    def edit(action):
        browser = app.test_client()
        browser.set_cookie("session", cookie)
        return browser.post(f"/tasks/{t['_id']}/{action}", data={"csrf":token,"version":"1"}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(edit, ["complete", "reopen"]))
    assert sorted(statuses) == [302, 409]
    assert app.extensions["db"].tasks.find_one()["version"] == 2
