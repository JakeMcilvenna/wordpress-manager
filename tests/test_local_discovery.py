"""Live local Docker discovery without credentials, Docker socket or host writes."""
from pathlib import Path

import pytest

from dev import stack_discovery
from dev.preview import create_preview_app

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8090"
SITE = {"name": "Test WordPress", "url": "http://127.0.0.1:8091/",
        "admin_url": "http://127.0.0.1:8091/wp-admin/",
        "version": "7.1.4", "state": "healthy", "source": "Live WordPress REST API"}


def test_live_docker_service_discovery_reads_versions_and_site(monkeypatch):
    def fake_json(path):
        if path == "/__wm_dev_status":
            return {"php_version": "8.5.12", "wordpress_version": "7.1.4"}, "nginx/1.28.0"
        if path == "/?rest_route=/":
            return {"name": "Test WordPress", "url": "http://127.0.0.1:8091/"}, ""
        raise AssertionError("Unapproved discovery URL")

    monkeypatch.setattr(stack_discovery, "get_json", fake_json)
    monkeypatch.setattr(stack_discovery, "database_version", lambda: "10.11.14-MariaDB")
    components, sites = stack_discovery.probe_stack()
    assert components["nginx"]["state"] == "healthy"
    assert components["nginx"]["version"] == "1.28.0"
    assert components["php"]["version"] == "8.5.12"
    assert components["fpm"]["state"] == "healthy"
    assert components["mariadb"]["version"] == "10.11.14-MariaDB"
    assert components["wpcli"]["state"] == "unknown"
    assert sites[0]["name"] == "Test WordPress"
    assert sites[0]["version"] == "7.1.4"
    assert sites[0]["admin_url"] == "http://127.0.0.1:8091/wp-admin/"


def test_discovery_handles_down_services_and_uninstalled_wordpress(monkeypatch):
    def disconnected(path):
        raise OSError("Container unavailable")

    monkeypatch.setattr(stack_discovery, "get_json", disconnected)
    monkeypatch.setattr(stack_discovery, "database_version", lambda: (_ for _ in ()).throw(OSError()))
    components, sites = stack_discovery.probe_stack()
    assert not sites
    assert components["nginx"]["state"] == "unknown"
    assert components["php"]["state"] == "unknown"
    assert components["mariadb"]["state"] == "unknown"

    monkeypatch.setattr(stack_discovery, "get_json", lambda path: (
        ({"php_version": "8.5.12", "wordpress_version": "7.1.4"}, "nginx/1.28.0")
        if path == "/__wm_dev_status" else ({"name": ""}, "")))
    _, sites = stack_discovery.probe_stack()
    assert sites == []


def test_untrusted_discovery_target_is_rejected():
    with pytest.raises(ValueError):
        stack_discovery.get_json("http://example.com/")
    with pytest.raises(ValueError):
        stack_discovery.get_json("/admin/")


def test_dev_browser_sees_discovered_site_only_after_login(monkeypatch, observer):
    monkeypatch.setenv("WM_DEV_WORDPRESS_INTERNAL_URL", "http://nginx:8080/")
    snapshot = observer.snapshot()
    snapshot["dev_sites"] = [SITE]
    snapshot["software"] = [
        {"id": "nginx", "name": "Nginx", "purpose": "Web server",
         "version": "1.28.0", "state": "healthy", "source": "HTTP", "service": "Docker"}
    ]
    class FixedObserver:
        def snapshot(self):
            return snapshot

    app = create_preview_app(password="dev-pass", secret_key="a" * 64, observer=FixedObserver())
    client = app.test_client()
    assert client.get("/view/websites", base_url=ORIGIN).status_code == 302
    client.get("/login", base_url=ORIGIN)
    with client.session_transaction(base_url=ORIGIN) as session:
        token = session["csrf"]
    response = client.post("/login", base_url=ORIGIN, headers={"Origin": ORIGIN},
                           data={"csrf": token, "username": "admin", "password": "dev-pass"})
    assert response.status_code == 302
    page = client.get("/view/websites", base_url=ORIGIN)
    assert page.status_code == 200
    assert b"Test WordPress" in page.data
    assert b"7.1.4" in page.data
    assert b"http://127.0.0.1:8091/wp-admin/" in page.data
    assert b"Discovered local WordPress sites" in page.data
    overview = client.get("/", base_url=ORIGIN)
    assert b"Discovered local websites" in overview.data
    assert b"Local Docker development" in overview.data


def test_discovery_configuration_never_exposes_vps_secrets():
    source = (ROOT / "dev/stack_discovery.py").read_text()
    compose = (ROOT / "compose.stack.yml").read_text()
    assert "docker.sock" not in source
    assert "mysql.connector" not in source and "pymysql" not in source
    assert "NoRedirect" in source
    assert "dev/wp-runtime-status.php:/var/www/html/.wm-dev-status.php:ro" in compose
    nginx = (ROOT / "dev/nginx-wordpress.conf").read_text()
    assert "location = /__wm_dev_status" in nginx
    assert "SCRIPT_FILENAME /var/www/html/.wm-dev-status.php" in nginx
    assert "location ~ /\\." in nginx
    php = (ROOT / "dev/wp-runtime-status.php").read_text()
    assert "wp-includes/version.php" in php
    assert "wp-config.php" not in php
    assert "WordPress REST API" in (ROOT / "wordpress_manager/templates/page.html").read_text()
