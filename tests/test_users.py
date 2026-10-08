"""User login and access model: per-user pbkdf2 passwords, two
roles (admin / viewer), admin user management, role-gated
writes; the access token seeds the admin account and is the
only bootstrap (main refuses to start without it)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.store import Store, _hash_password, _verify_password
import pytest  # noqa: E402


def _fresh_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path)


pytestmark = pytest.mark.essential


@pytest.mark.minimum
def test_password_hash_roundtrip():
    stored = _hash_password("hunter22")
    assert stored.startswith("pbkdf2$200000$")
    assert _verify_password("hunter22", stored)
    assert not _verify_password("hunter23", stored)
    assert not _verify_password("hunter22", "garbage")


def test_user_crud_and_roles():
    s = _fresh_store()
    assert s.user_count() == 0
    assert s.create_user("liam", "secret1", "admin")
    assert not s.create_user("liam", "whatever")     # taken
    assert not s.create_user("bad", "whatever", "root")
    assert s.create_user("friend", "friendpw", "viewer")
    assert s.user_count() == 2

    listed = s.list_users()
    assert [u["username"] for u in listed] == ["friend", "liam"]
    assert all("password_hash" not in u for u in listed)

    assert s.verify_user("liam", "secret1")["role"] == "admin"
    assert s.verify_user("friend", "friendpw")["role"] == "viewer"
    assert s.verify_user("friend", "secret1") is None
    assert s.verify_user("ghost", "x") is None

    assert s.update_role("friend", "admin")
    assert s.verify_user("friend", "friendpw")["role"] == "admin"
    assert s.delete_user("friend")
    assert not s.delete_user("friend")


def _make_app(store):
    from core.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
        TradingConfig, WealthsimpleConfig,
    )
    from consumer.web import create_app

    class Stub:
        def __init__(self):
            self.trading = TradingConfig(mode="notify")
            self.pipeline = type(
                "P", (), {"auth_token": "servicetoken"})()
            self.wealthsimple = WealthsimpleConfig(accounts=[])
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    return create_app(Stub(), store, None, None, None), Stub


def _login(client, username, password):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_admin_seeded_from_access_token():
    s = _fresh_store()
    app, cfg = _make_app(s)
    # the token seeded an admin user with the same password
    assert s.user_count() == 1
    assert s.verify_user("admin", "servicetoken")["role"] == "admin"

    client = app.test_client()
    r = _login(client, "admin", "servicetoken")
    assert r.status_code == 302
    assert client.get("/api/settings").status_code == 200
    r = client.post("/api/settings", json={"trading": {}})
    assert r.status_code == 200


def test_viewer_role_is_read_only():
    s = _fresh_store()
    s.create_user("liam", "ownerpw", "admin")
    s.create_user("friend", "friendpw", "viewer")
    app, cfg = _make_app(s)
    admin, viewer = app.test_client(), app.test_client()
    _login(admin, "liam", "ownerpw")
    _login(viewer, "friend", "friendpw")

    # reads are open to viewers
    assert viewer.get("/api/settings").status_code == 200
    assert viewer.get("/api/trades").status_code == 200
    dash = viewer.get("/api/dashboard")
    assert dash.status_code == 200
    assert dash.get_json()["me"]["role"] == "viewer"

    # writes are admin-only
    assert viewer.post(
        "/api/settings", json={"trading": {"stop_loss_pct": 1}}
    ).status_code == 403
    assert viewer.post("/api/paper-reset", json={"label": "x"}
                       ).status_code in (400, 403)
    assert viewer.get("/api/users").status_code == 403
    # the admin keeps full access
    assert admin.get("/api/users").status_code == 200


def test_user_management_rules():
    s = _fresh_store()
    s.create_user("liam", "ownerpw", "admin")
    app, cfg = _make_app(s)
    client = app.test_client()
    _login(client, "liam", "ownerpw")

    # create a viewer through the api
    r = client.post("/api/users", json={
        "action": "create", "username": "buddy",
        "password": "buddy12", "role": "viewer",
    })
    assert r.status_code == 200
    assert s.verify_user("buddy", "buddy12") is not None

    # bad inputs rejected
    assert client.post("/api/users", json={
        "action": "create", "username": "buddy",
        "password": "buddy12",
    }).status_code == 400                       # taken
    assert client.post("/api/users", json={
        "action": "create", "username": "ok name!",
        "password": "buddy12",
    }).status_code == 400                       # bad chars
    assert client.post("/api/users", json={
        "action": "create", "username": "short",
        "password": "abc",
    }).status_code == 400                       # too short

    # cannot delete yourself or the last admin
    assert client.post("/api/users", json={
        "action": "delete", "username": "liam",
    }).status_code == 400

    # delete another user works
    assert client.post("/api/users", json={
        "action": "delete", "username": "buddy",
    }).status_code == 200
    assert s.get_user("buddy") is None


def test_self_password_change_requires_current():
    s = _fresh_store()
    s.create_user("liam", "ownerpw", "admin")
    s.create_user("friend", "friendpw", "viewer")
    app, cfg = _make_app(s)
    viewer = app.test_client()
    _login(viewer, "friend", "friendpw")

    # wrong current password
    r = viewer.post("/api/users", json={
        "action": "set_password", "username": "friend",
        "current_password": "nope", "password": "newpass1",
    })
    assert r.status_code == 403

    r = viewer.post("/api/users", json={
        "action": "set_password", "username": "friend",
        "current_password": "friendpw", "password": "newpass1",
    })
    assert r.status_code == 200
    assert s.verify_user("friend", "newpass1") is not None

    # a viewer cannot change someone else's password
    r = viewer.post("/api/users", json={
        "action": "set_password", "username": "liam",
        "current_password": "friendpw", "password": "hacked1",
    })
    assert r.status_code == 403
    assert s.verify_user("liam", "ownerpw") is not None


def test_tokenless_install_cannot_claim_or_bare_token_login():
    """the token is the bootstrap (it seeds the admin at first
    boot and main() refuses to start without it) - a tokenless
    app (a test-only state) has no claim form and the bare token
    is not a password."""
    s = _fresh_store()
    from core.config import (
        AutoUpdateConfig, DiscordConfig, QuotesConfig, ReaderConfig,
    )
    from consumer.web import create_app

    class Stub:
        def __init__(self, token):
            self.trading = type("T", (), {"mode": "notify"})()
            self.pipeline = type("P", (), {"auth_token": token})()
            self.wealthsimple = type("W", (), {"accounts": []})()
            self.reader = ReaderConfig()
            self.discord = DiscordConfig()
            self.parser = type("P2", (), {"custom_patterns": []})()
            self.auto_update = AutoUpdateConfig()
            self.quotes = QuotesConfig()
            self.paper = None

    app = create_app(Stub(""), s, None, None, None)
    assert s.user_count() == 0
    client = app.test_client()
    r = client.post("/login", data={
        "username": "liam", "password": "bootstr1",
    }, follow_redirects=False)
    # no claim path - the login is just wrong
    assert r.status_code == 200
    assert s.user_count() == 0

    # the bare token is not an owner login either
    s2 = _fresh_store()
    app2 = create_app(Stub("legacytoken"), s2, None, None, None)
    c2 = app2.test_client()
    r = c2.post("/login", data={
        "username": "", "password": "legacytoken",
    }, follow_redirects=False)
    assert r.status_code == 200   # login rejected, form re-rendered
    assert c2.get("/api/settings").status_code == 401
