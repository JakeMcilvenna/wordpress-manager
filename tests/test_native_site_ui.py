"""Guest-only dashboard-to-native-site socket regression tests."""
from wordpress_manager import create_app
from wordpress_manager import native_site_ui


class GuestBoundary:
    def __init__(self):
        self.calls = []
        self.jobs = []
        self.secret = "test-only-admin-secret"

    def call(self, op, **fields):
        self.calls.append((op, fields))
        if op == "login":
            if fields["password"] != "guest-password":
                raise ValueError("Invalid login")
            return {"capability": "guest-session"}
        if op == "site-status":
            return {"jobs": list(self.jobs)}
        if op == "site-plan":
            if fields["domain"] != "third.test":
                raise ValueError("Invalid disposable test domain")
            return {
                "id": "a" * 32, "digest": "d" * 64,
                "site_id": "wm-aaaaaaaaaaaa", "domain": "third.test",
                "backend": "ubuntu-disposable", "php": "8.3",
                "confirmation": "CREATE NATIVE TEST SITE",
                "resources": ["/srv/wordpress-manager-sites/wm-aaaaaaaaaaaa"],
            }
        if op == "site-apply":
            if fields["plan"] != "a" * 32 or fields["confirmation"] != "CREATE NATIVE TEST SITE":
                raise ValueError("Invalid or unconfirmed plan")
            self.jobs = [{
                "id": "b" * 32, "site_id": "wm-aaaaaaaaaaaa",
                "state": "succeeded", "detail": "Disposable site created",
            }]
            return {"job": "b" * 32}
        if op == "site-result":
            if fields["job"] != "b" * 32 or self.secret is None:
                raise ValueError("No available completed site result")
            result = {
                "domain": "third.test", "url": "http://third.test:18080/",
                "username": "admin", "password": self.secret,
            }
            self.secret = None
            return result
        raise AssertionError("Unexpected socket verb: " + op)


def csrf(client):
    with client.session_transaction() as state:
        return state["csrf"]


def sign_in(client):
    client.get("/login")
    return client.post("/login", data={
        "csrf": csrf(client), "username": "admin", "password": "guest-password",
    })


def guest_app(monkeypatch, observer):
    monkeypatch.setattr(native_site_ui, "disposable_guest", lambda: True)
    boundary = GuestBoundary()
    app = create_app({
        "TESTING": True,
        "AUTH_SETTINGS": {"secret_key": "a" * 64, "provisioning": True},
        "CONTROLLER": boundary,
    }, observer)
    return app.test_client(), boundary, app


def test_no_guest_site_route_on_normal_hosts(monkeypatch, observer):
    monkeypatch.setattr(native_site_ui, "disposable_guest", lambda: False)
    boundary = GuestBoundary()
    app = create_app({
        "TESTING": True,
        "AUTH_SETTINGS": {"secret_key": "a" * 64, "provisioning": True},
        "CONTROLLER": boundary,
    }, observer)
    client = app.test_client()
    assert app.config["NATIVE_SITE_UI_ENABLED"] is False
    assert client.get("/development/native-sites").status_code == 404
    assert sign_in(client).status_code == 302
    assert b"Manage disposable websites" not in client.get("/view/websites").data
    assert not any(op.startswith("site-") for op, _ in boundary.calls)


def test_guest_site_ui_requires_login_csrf_origin_and_loopback(monkeypatch, observer):
    client, boundary, app = guest_app(monkeypatch, observer)
    assert client.get("/development/native-sites").status_code == 302
    assert sign_in(client).status_code == 302
    assert b"Manage disposable websites" in client.get("/view/websites").data
    path = "/development/native-sites"
    assert client.get(path).status_code == 200
    before = list(boundary.calls)
    payload = {
        "action": "plan", "title": "Third site",
        "email": "admin@third.test", "domain": "third.test", "php": "8.3",
    }
    assert client.post(path, data=payload).status_code == 403
    assert client.post(path, data={**payload, "csrf": csrf(client)},
                       headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.post(path, data={**payload, "csrf": csrf(client)},
                       environ_overrides={"REMOTE_ADDR": "203.0.113.4"}).status_code == 403
    assert client.put(path).status_code == 405
    assert not any(op == "site-plan" for op, _ in boundary.calls)
    assert client.post(path, data={"csrf": csrf(client), "action": "delete"}).status_code == 400
    assert boundary.calls == before


def test_guest_plan_apply_and_one_time_secret_never_enter_session_or_get(monkeypatch, observer):
    client, boundary, app = guest_app(monkeypatch, observer)
    sign_in(client)
    path = "/development/native-sites"
    plan = client.post(path, data={
        "csrf": csrf(client), "action": "plan", "title": "Third site",
        "email": "admin@third.test", "domain": "third.test", "php": "8.3",
    })
    assert plan.status_code == 200
    assert b"CREATE NATIVE TEST SITE" in plan.data
    assert b"wm-aaaaaaaaaaaa" in plan.data
    assert b"Plan fingerprint" in plan.data
    assert not boundary.jobs
    wrong = client.post(path, data={
        "csrf": csrf(client), "action": "apply", "plan": "a" * 32,
        "confirmation": "YES",
    })
    assert wrong.status_code == 409
    assert not boundary.jobs
    approved = client.post(path, data={
        "csrf": csrf(client), "action": "apply", "plan": "a" * 32,
        "confirmation": "CREATE NATIVE TEST SITE",
    })
    assert approved.status_code == 303
    assert approved.headers["Location"] == path
    page = client.get(path)
    assert b"Collect once" in page.data
    assert b"test-only-admin-secret" not in page.data
    received = client.post(path, data={
        "csrf": csrf(client), "action": "result", "job": "b" * 32,
    })
    assert received.status_code == 200
    assert b"test-only-admin-secret" in received.data
    assert received.headers["Cache-Control"] == "no-store"
    assert client.post(path, data={
        "csrf": csrf(client), "action": "result", "job": "b" * 32,
    }).status_code == 409
    assert b"test-only-admin-secret" not in client.get(path).data
    with client.session_transaction() as state:
        assert set(state).isdisjoint({"plan", "password", "result", "site_id"})
        assert "test-only-admin-secret" not in str(dict(state))
    assert "test-only-admin-secret" not in str(app.extensions["audit_events"])
    assert [op for op, _ in boundary.calls].count("site-apply") == 2


def test_revoking_guest_marker_disables_registered_route(monkeypatch, observer):
    client, boundary, app = guest_app(monkeypatch, observer)
    sign_in(client)
    monkeypatch.setattr(native_site_ui, "disposable_guest", lambda: False)
    assert client.get("/development/native-sites").status_code == 404
    assert client.post("/development/native-sites", data={
        "csrf": csrf(client), "action": "plan",
    }).status_code == 404
    assert not any(op.startswith("site-") for op, _ in boundary.calls)
