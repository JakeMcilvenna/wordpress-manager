"""Create Website is a real local-only Docker workflow, never production."""
from pathlib import Path

import pytest

from dev.site_creator import LocalSiteProvisioner, created_site, validate_fields
from dev.preview import create_preview_app
from wordpress_manager import create_app

ORIGIN = "http://127.0.0.1:8090"


class FakeProvisioner:
    def __init__(self):
        self.installed = False
        self.install_count = 0
        self.domain = None

    def status(self):
        if self.installed:
            return {
                "ready": True, "installed": True, "title": "Example test website",
                "domain": self.domain, "version": "7.1.4",
            }
        return {"ready": True, "installed": False}

    def install(self, plan):
        self.install_count += 1
        assert plan["title"] == "Example test website"
        assert plan["domain"] == "client-example.test"
        assert plan["php"] == "8.5"
        self.installed = True
        self.domain = plan["domain"]
        return {
            "installed": True, "domain": self.domain,
            "url": "http://127.0.0.1:8092/",
            "admin_url": "http://127.0.0.1:8092/wp-admin/",
            "username": "admin", "password": "generated-test-password",
        }


class FixedObserver:
    def snapshot(self):
        return {"dev_sites": [{
            "id": "local-first-site", "name": "Existing test site",
            "url": "http://127.0.0.1:8091/",
            "admin_url": "http://127.0.0.1:8091/wp-admin/",
            "version": "7.1.4", "state": "healthy",
        }]}


def sign_in(client):
    client.get("/login", base_url=ORIGIN)
    with client.session_transaction(base_url=ORIGIN) as state:
        token = state["csrf"]
    assert client.post("/login", base_url=ORIGIN, headers={"Origin": ORIGIN},
                       data={"csrf": token, "username": "admin",
                             "password": "test-password"}).status_code == 302


def csrf(client):
    with client.session_transaction(base_url=ORIGIN) as state:
        return state["csrf"]


def test_domain_validation_and_fixed_local_preview():
    value = validate_fields("  Example test website  ", "admin@client-example.test",
                            "CLIENT-EXAMPLE.TEST", "8.5")
    assert value["title"] == "Example test website"
    assert value["domain"] == "client-example.test"
    assert value["url"] == "http://127.0.0.1:8092/"
    for domain in ("", "localhost", "abc.localhost", "127.0.0.1", "example.com",
                   "-invalid.test", "bad-.test", "has space.test", "bad..test",
                   "http://domain.test", "example.test:8092", "_hidden.test",
                   "x" * 64 + ".test"):
        with pytest.raises(ValueError):
            validate_fields("site", "admin@example.test", domain)
    with pytest.raises(ValueError):
        validate_fields("site", "admin@example.test", "client-example.test", "8.3")
    with pytest.raises(ValueError):
        validate_fields("site", "not-an-email", "client-example.test")


def test_local_creation_plan_approval_real_backend_and_isolated_second_site(monkeypatch, tmp_path):
    monkeypatch.setenv("WM_DEV_WORDPRESS_INTERNAL_URL", "http://nginx:8080/")
    monkeypatch.setenv("WM_DEV_SECOND_SITE_ENABLED", "1")
    backend = FakeProvisioner()
    app = create_preview_app(password="test-password", secret_key="c" * 64,
                             observer=FixedObserver(),
                             registry_path=tmp_path / "registry.sqlite3",
                             site_provisioner=backend)
    client = app.test_client()
    assert client.get("/dev/sites/create", base_url=ORIGIN).status_code == 302
    assert client.post("/dev/sites/create", base_url=ORIGIN).status_code == 403
    sign_in(client)

    before = client.get("/view/websites", base_url=ORIGIN)
    assert before.status_code == 200
    assert b"Existing test site" in before.data
    assert b"Create WordPress website" in before.data
    assert b"Example test website" not in before.data
    wizard = client.get("/dev/sites/create", base_url=ORIGIN)
    assert wizard.status_code == 200
    assert b'name="domain"' in wizard.data
    assert b"127.0.0.1:8092" in wizard.data

    # Exact CSRF and same-origin checks remain mandatory on all writes.
    assert client.post("/dev/sites/create", base_url=ORIGIN, data={
        "action": "plan", "title": "Example test website",
        "email": "admin@client-example.test", "domain": "client-example.test",
    }).status_code == 403
    assert client.post("/dev/sites/create", base_url=ORIGIN,
                       headers={"Origin": "http://attacker.test"},
                       data={"action": "plan", "csrf": csrf(client)}).status_code == 403

    wrong = client.post("/dev/sites/create", base_url=ORIGIN,
                        headers={"Origin": ORIGIN}, data={
        "action": "plan", "csrf": csrf(client),
        "title": "Example test website",
        "email": "admin@client-example.test", "domain": "bad..test",
    })
    assert wrong.status_code == 409
    assert backend.install_count == 0

    preview = client.post("/dev/sites/create", base_url=ORIGIN,
                          headers={"Origin": ORIGIN}, data={
        "action": "plan", "csrf": csrf(client),
        "title": "Example test website", "php": "8.5",
        "domain": "client-example.test", "email": "admin@client-example.test",
    })
    assert preview.status_code == 200
    assert b"Review installation plan" in preview.data
    assert b"client-example.test" in preview.data

    no_confirmation = client.post("/dev/sites/create", base_url=ORIGIN,
                                  headers={"Origin": ORIGIN}, data={
        "action": "create", "csrf": csrf(client), "confirmation": "wrong",
    })
    assert no_confirmation.status_code == 409
    assert backend.install_count == 0

    installed = client.post("/dev/sites/create", base_url=ORIGIN,
                            headers={"Origin": ORIGIN}, data={
        "action": "create", "csrf": csrf(client),
        "confirmation": "CREATE LOCAL SITE",
    })
    assert installed.status_code == 200
    assert b"WordPress installed successfully" in installed.data
    assert b"generated-test-password" in installed.data
    assert backend.install_count == 1

    # One-time admin password must not be persisted in Flask session/cookies.
    with client.session_transaction(base_url=ORIGIN) as state:
        assert "generated-test-password" not in str(dict(state))
        assert "wm_site_creation_plan" not in state
    assert b"generated-test-password" not in client.get(
        "/dev/sites/create", base_url=ORIGIN).data

    sites = client.get("/view/websites", base_url=ORIGIN)
    assert sites.status_code == 200
    assert b"Existing test site" in sites.data
    assert b"Example test website" in sites.data
    assert b"client-example.test" in sites.data
    assert b"127.0.0.1:8091" in sites.data
    assert b"127.0.0.1:8092" in sites.data
    details = client.get("/dev/sites/second-site", base_url=ORIGIN)
    assert details.status_code == 200
    assert b"client-example.test" in details.data

    # A second installation cannot overwrite either existing local website.
    assert b"already installed" in client.get(
        "/dev/sites/create", base_url=ORIGIN).data.lower()
    assert backend.install_count == 1


def test_preview_creation_disabled_without_full_stack(monkeypatch, tmp_path):
    monkeypatch.delenv("WM_DEV_SECOND_SITE_ENABLED", raising=False)
    app = create_preview_app(password="test-password", secret_key="d" * 64,
                             observer=FixedObserver(),
                             registry_path=tmp_path / "registry.sqlite3",
                             site_provisioner=FakeProvisioner())
    client = app.test_client()
    sign_in(client)
    assert client.get("/dev/sites/create", base_url=ORIGIN).status_code == 404
    assert client.get("/dev/sites/second-site", base_url=ORIGIN).status_code == 404


def test_production_app_has_no_create_site_routes(observer):
    app = create_app({"TESTING": True}, collector=observer)
    client = app.test_client()
    assert client.get("/dev/sites/create").status_code == 404
    assert client.post("/dev/sites/create").status_code in (404, 405)


def test_created_site_metadata_requires_confirmed_installation():
    assert created_site({}) is None
    assert created_site({"installed": False}) is None
    assert created_site({"installed": True, "title": "New", "version": "invalid"}) is None
    site = created_site({"installed": True, "title": "New",
                         "version": "7.1.4", "domain": "client-example.test"})
    assert site["domain"] == "client-example.test"
    assert site["url"] == "http://127.0.0.1:8092/"


def test_second_site_docker_permissions_and_isolated_volumes():
    root = Path(__file__).resolve().parents[1]
    stack = (root / "compose.stack.yml").read_text()
    assert 'site2-database:' in stack
    assert 'site2-wordpress:' in stack
    assert 'site2-nginx:' in stack
    assert 'site2-provisioner:' in stack
    assert 'second_site_database:/var/lib/mysql' in stack
    assert 'second_site_files:/var/www/html' in stack
    assert '"127.0.0.1:8092:8080"' in stack
    assert 'local-second-site-only-password' in stack
    assert 'WM_DEV_BROKER_TOKEN:' in stack
    assert '/var/run/docker.sock' not in stack
    assert 'privileged: true' not in stack
    broker = (root / "dev/local-site-provisioner.php").read_text()
    assert "hash_equals" in broker
    assert "proc_open" in broker
    assert "'--prompt=admin_password'" in broker
    assert "'wm_intended_domain'" in broker
    assert "shell_exec(" not in broker and "exec(" not in broker
    assert "admin_password" not in (root / "dev/site_creator.py").read_text()
