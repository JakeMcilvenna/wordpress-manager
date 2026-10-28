from unittest.mock import patch

import pytest

from wordpress_manager import create_app


@pytest.mark.parametrize("path", ["/", "/view/setup", "/view/websites", "/view/staging", "/view/backups", "/view/security", "/fragments/overview"])
def test_every_view_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert b"<script>" not in response.data


def test_real_snapshot_contract(client):
    data = client.get("/api/snapshot").get_json()
    assert data["resources"]["memory"]["percent"] == 25
    assert data["resources"]["disk"]["percent"] == 30
    assert b"8.5.12" in client.get("/").data
    assert b"No managed websites yet" in client.get("/view/websites").data


@pytest.mark.parametrize("address", ["192.168.1.2", "203.0.113.2", "::", "garbage", ""])
def test_non_loopback_rejected_even_with_forwarded_headers(client, address):
    response = client.get("/api/snapshot", environ_overrides={"REMOTE_ADDR": address},
                          headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 403


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_supported(client, address):
    assert client.get("/", environ_overrides={"REMOTE_ADDR": address}).status_code == 200


def test_dns_rebinding_host_rejected(client):
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_cross_site_fetch_rejected(client):
    assert client.get("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_mutation_routes(client, method):
    for path in ("/", "/view/setup", "/api/snapshot", "/api/install", "/api/provision"):
        assert client.open(path, method=method).status_code == 405


def test_security_headers_on_errors_too(client):
    for path in ("/", "/missing", "/static/app.css"):
        response = client.get(path)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
        assert "Access-Control-Allow-Origin" not in response.headers


def test_root_process_refuses_to_start():
    with patch("wordpress_manager.os.geteuid", return_value=0, create=True):
        with pytest.raises(RuntimeError, match="never root"):
            create_app()


def test_health_does_not_trigger_collector(client, observer):
    with patch.object(observer, "snapshot", side_effect=AssertionError("Probe should not run")):
        assert client.get("/healthz").get_json() == {"status": "ok", "mode": "private"}


def test_read_log_bounded_and_omits_query_secrets(client, app):
    for _ in range(110):
        client.get("/healthz?token=never-record-this")
    assert len(app.extensions["audit_events"]) == 100
    log = client.get("/view/security").data
    assert b"never-record-this" not in log
    assert b"Read health" in log


def test_snapshot_values_are_html_escaped(client, observer):
    observer.snapshot()["hostname"] = '<script>alert("x")</script>'
    response = client.get("/")
    assert b"&lt;script&gt;" in response.data
    assert b'<script>alert' not in response.data


def test_htmx_fragment_and_vendored_assets(client):
    response = client.get("/fragments/overview", headers={"HX-Request": "true"})
    assert b'id="live"' in response.data
    assert b"<html" not in response.data
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
