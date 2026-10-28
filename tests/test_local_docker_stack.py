"""Development stack contract; no Docker daemon or VPS required in unit CI."""
from pathlib import Path

from dev.preview import create_preview_app

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8090"


def login(client):
    client.get("/login", base_url=ORIGIN)
    with client.session_transaction(base_url=ORIGIN) as state:
        csrf = state["csrf"]
    return client.post("/login", data={
        "username": "admin", "password": "test-preview-password", "csrf": csrf,
    }, base_url=ORIGIN, headers={"Origin": ORIGIN})


def test_local_preview_uses_real_login_logout_and_csrf_routes():
    app = create_preview_app(password="test-preview-password", secret_key="a" * 64)
    client = app.test_client()
    assert client.get("/", base_url=ORIGIN).status_code == 302
    assert client.get("/login", base_url=ORIGIN).status_code == 200
    assert b"LOCAL DEVELOPMENT PREVIEW" in client.get("/login", base_url=ORIGIN).data
    assert client.post("/login", data={
        "username": "admin", "password": "test-preview-password",
    }, base_url=ORIGIN).status_code == 403

    logged_in = login(client)
    assert logged_in.status_code == 302
    assert logged_in.headers["Location"] == "/"
    assert "HttpOnly" in logged_in.headers["Set-Cookie"]
    assert "SameSite=Strict" in logged_in.headers["Set-Cookie"]
    assert client.get("/", base_url=ORIGIN).status_code == 200

    with client.session_transaction(base_url=ORIGIN) as state:
        csrf = state["csrf"]
    logout = client.post("/logout", data={"csrf": csrf},
                         base_url=ORIGIN, headers={"Origin": ORIGIN})
    assert logout.status_code == 302
    assert client.get("/", base_url=ORIGIN).status_code == 302


def test_preview_refuses_wrong_password_and_simulates_privileged_actions():
    app = create_preview_app(password="test-preview-password", secret_key="b" * 64)
    client = app.test_client()
    client.get("/login", base_url=ORIGIN)
    with client.session_transaction(base_url=ORIGIN) as state:
        csrf = state["csrf"]
    assert client.post("/login", data={
        "csrf": csrf, "username": "admin", "password": "wrong",
    }, base_url=ORIGIN).status_code == 401
    assert login(client).status_code == 302
    assert b"Simulate server software setup" in client.get("/setup/control", base_url=ORIGIN).data
    with client.session_transaction(base_url=ORIGIN) as state:
        csrf = state["csrf"]
    preview = client.post("/setup/control", data={
        "csrf": csrf, "action": "plan", "php": "8.5",
    }, base_url=ORIGIN)
    assert preview.status_code == 200
    assert b"SIMULATED" in preview.data
    assert b"Simulate approval (no installation)" in preview.data


def test_local_wordpress_compose_has_real_services_without_host_exposure():
    stack = (ROOT / "compose.stack.yml").read_text()
    assert "mariadb:10.11" in stack
    assert "wordpress:php8.5-fpm" in stack
    assert "wordpress:cli-2.12.0-php8.5" in stack
    assert "image: nginx/nginx-unprivileged:stable-alpine" in stack
    assert "cap_drop:" in stack and "- ALL" in stack
    assert "no-new-privileges:true" in stack
    assert "read_only: true" in stack
    assert '"127.0.0.1:8091:8080"' in stack
    assert "wordpress_database:/var/lib/mysql" in stack
    assert "wordpress_files:/var/www/html" in stack
    assert "healthcheck.sh" in stack
    assert "privileged: true" not in stack
    assert "/var/run/docker.sock" not in stack
    # Nginx and WordPress live on the Compose-only network.
    assert "fastcgi_pass wordpress:9000" in (ROOT / "dev/nginx-wordpress.conf").read_text()
    assert "listen 8080" in (ROOT / "dev/nginx-wordpress.conf").read_text()
