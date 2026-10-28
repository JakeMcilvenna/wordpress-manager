"""Regression tests for read-only site details and local-only registration."""
from pathlib import Path

import pytest

from dev.site_registry import SiteRegistry
from dev.preview import create_preview_app
from dev import stack_discovery
from wordpress_manager import create_app
from wordpress_manager.collector import Result

ORIGIN = "http://127.0.0.1:8090"
SITE = {
    "name": "Test website",
    "url": "http://127.0.0.1:8091/",
    "admin_url": "http://127.0.0.1:8091/wp-admin/",
    "version": "7.1.4",
    "state": "healthy",
    "plugins": [{"name": "Hello Dolly", "version": "1.7.2"}],
    "themes": [{"name": "Twenty Twenty-Five", "version": "1.4"}],
}


class FixedObserver:
    def __init__(self, discovered=True):
        self.discovered = discovered

    def snapshot(self):
        return {
            "software": [{"id": "nginx", "name": "Nginx",
                          "version": "1.30.6", "state": "healthy",
                          "purpose": "Web server", "source": "Docker",
                          "service": "Docker Nginx"}],
            "dev_sites": [SITE] if self.discovered else [],
        }


def login(client):
    client.get("/login", base_url=ORIGIN)
    with client.session_transaction(base_url=ORIGIN) as state:
        token = state["csrf"]
    assert client.post("/login", base_url=ORIGIN, headers={"Origin": ORIGIN},
                       data={"username": "admin", "password": "local-test",
                             "csrf": token}).status_code == 302


def csrf(client):
    with client.session_transaction(base_url=ORIGIN) as state:
        return state["csrf"]


def test_registry_survives_restarts_and_only_stores_local_registration(tmp_path):
    path = tmp_path / "db" / "sites.sqlite3"
    one = SiteRegistry(path)
    assert not one.is_registered()
    one.register()
    one.register()  # idempotent
    assert SiteRegistry(path).is_registered()
    SiteRegistry(path).unregister()
    assert not one.is_registered()


def test_dev_details_registration_logout_and_recreation(monkeypatch, tmp_path):
    monkeypatch.setenv("WM_DEV_WORDPRESS_INTERNAL_URL", "http://nginx:8080/")
    path = tmp_path / "registrations.sqlite3"
    one = create_preview_app(password="local-test", secret_key="a" * 64,
                             observer=FixedObserver(), registry_path=path)
    client = one.test_client()
    assert client.get("/dev/sites/local-wordpress", base_url=ORIGIN).status_code == 302
    assert client.post("/dev/sites/register", base_url=ORIGIN).status_code == 403
    login(client)
    detail = client.get("/dev/sites/local-wordpress", base_url=ORIGIN)
    assert detail.status_code == 200
    assert b"Test website" in detail.data
    assert b"Hello Dolly" in detail.data
    assert b"Twenty Twenty-Five" in detail.data
    assert b"Discovered only" in detail.data
    assert b"wp-admin/" in detail.data
    assert b"Register for monitoring" in detail.data

    # No POST bypasses CSRF or the exact browser Origin.
    assert client.post("/dev/sites/register", base_url=ORIGIN).status_code == 403
    assert client.post("/dev/sites/register", base_url=ORIGIN,
                       headers={"Origin": "http://evil.test"},
                       data={"csrf": csrf(client)}).status_code == 403
    assert not SiteRegistry(path).is_registered()
    result = client.post("/dev/sites/register", base_url=ORIGIN,
                         headers={"Origin": ORIGIN}, data={"csrf": csrf(client),
                         "url": "https://evil.test/ignore-this-value"})
    assert result.status_code == 302
    assert result.headers["Location"] == "/dev/sites/local-wordpress"
    assert SiteRegistry(path).is_registered()
    assert b"Registered for monitoring" in client.get(
        "/dev/sites/local-wordpress", base_url=ORIGIN).data

    # New Flask process, new signed-in session, same persistent volume.
    other = create_preview_app(password="local-test", secret_key="b" * 64,
                               observer=FixedObserver(), registry_path=path)
    second = other.test_client()
    login(second)
    assert b"Registered for monitoring" in second.get(
        "/dev/sites/local-wordpress", base_url=ORIGIN).data
    removed = second.post("/dev/sites/unregister", base_url=ORIGIN,
                          headers={"Origin": ORIGIN},
                          data={"csrf": csrf(second)})
    assert removed.status_code == 302
    assert not SiteRegistry(path).is_registered()
    assert b"Test website" in second.get("/view/websites", base_url=ORIGIN).data
    assert b"Discovered only" in second.get(
        "/dev/sites/local-wordpress", base_url=ORIGIN).data


def test_register_refuses_missing_or_unconfigured_site(monkeypatch, tmp_path):
    path = tmp_path / "registered.sqlite3"
    monkeypatch.setenv("WM_DEV_WORDPRESS_INTERNAL_URL", "http://nginx:8080/")
    app = create_preview_app(password="local-test", secret_key="a" * 64,
                             observer=FixedObserver(False), registry_path=path)
    client = app.test_client()
    login(client)
    response = client.post("/dev/sites/register", base_url=ORIGIN,
                           headers={"Origin": ORIGIN}, data={"csrf": csrf(client)})
    assert response.status_code == 409
    assert not SiteRegistry(path).is_registered()
    assert client.get("/dev/sites/local-wordpress", base_url=ORIGIN).status_code == 404

    monkeypatch.delenv("WM_DEV_WORDPRESS_INTERNAL_URL")
    assert client.post("/dev/sites/register", base_url=ORIGIN,
                       headers={"Origin": ORIGIN},
                       data={"csrf": csrf(client)}).status_code == 409


def test_production_app_has_no_site_registration_routes(observer):
    prod = create_app({"TESTING": True}, collector=observer).test_client()
    assert prod.get("/dev/sites/local-wordpress").status_code == 404
    assert prod.post("/dev/sites/register").status_code in (404, 405)
    assert prod.post("/dev/sites/unregister").status_code in (404, 405)


def test_service_counter_counts_only_monitored_services(monkeypatch):
    monkeypatch.setattr(stack_discovery, "probe_stack", lambda: (
        {
            "nginx": {"version": "1.30.6", "state": "healthy"},
            "php": {"version": "8.5.12", "state": "healthy"},
            "fpm": {"version": "8.5.12", "state": "healthy"},
            "mariadb": {"version": "10.11", "state": "healthy"},
        },
        [SITE],
    ))
    collector = stack_discovery.DockerStackCollector(
        runner=lambda probe, args=(): Result(""),
        reader=lambda path: None,
        system="Linux",
    )
    snapshot = collector.snapshot()
    assert snapshot["healthy_services"] == 3
    assert snapshot["service_count"] == 3


def test_plugin_theme_rows_are_bounded_and_sanitized():
    rows = stack_discovery.metadata_rows([
        {"name": "Example plugin", "version": "2.1"},
        {"name": "", "version": "1.0"},
        {"name": "unsafe", "version": [1, 2]},
        "other",
    ])
    assert rows == [{"name": "Example plugin", "version": "2.1"}]
    assert stack_discovery.metadata_rows("invalid") == []
    assert len(stack_discovery.metadata_rows([
        {"name": "Plugin", "version": "1"} for _ in range(100)
    ])) == 40


def test_preview_registration_does_not_mount_docker_or_wordpress_storage():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.preview.yml").read_text()
    assert "preview_registry:/var/lib/wm-preview:rw" in compose
    assert "/var/run/docker.sock" not in compose
    assert "wordpress_files" not in compose
    source = (root / "dev/site_registry.py").read_text()
    assert "SITE_ID = \"local-wordpress\"" in source
    assert "subprocess" not in source and "docker" not in source.lower()
    assert "dev_register_site" in (root / "wordpress_manager/__init__.py").read_text()
