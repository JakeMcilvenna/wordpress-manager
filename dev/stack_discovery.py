"""Fixed-target, read-only discovery for the *local Docker Compose* preview.

This code never runs in the production service. It cannot access Docker's
daemon, local host paths, the server control socket or any database credentials.
All destinations and advertised browser URLs are constants, not request input.
"""
import json
import re
import socket
from urllib import request
from urllib.error import HTTPError, URLError

from wordpress_manager.collector import Collector

NGINX_BASE = "http://nginx:8080"
PUBLIC_WORDPRESS = "http://127.0.0.1:8091"
MAX_HTTP = 512 * 1024


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(path):
    """Read bounded JSON from the fixed internal WordPress service."""
    if path not in ("/__wm_dev_status", "/?rest_route=/"):
        raise ValueError("Only the two fixed internal discovery paths are allowed")
    opener = request.build_opener(NoRedirect())
    req = request.Request(NGINX_BASE + path, headers={"Accept": "application/json"})
    with opener.open(req, timeout=3) as response:
        if response.status != 200:
            raise ValueError("Unexpected local HTTP status")
        raw = response.read(MAX_HTTP + 1)
        server = response.headers.get("Server", "")
    if len(raw) > MAX_HTTP:
        raise ValueError("Response exceeds discovery limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Invalid discovery response")
    return payload, server


def database_version():
    """Read MariaDB's unauthenticated protocol greeting, without logging in."""
    with socket.create_connection(("database", 3306), timeout=2) as conn:
        conn.settimeout(2)
        raw = conn.recv(128)
    if len(raw) < 8 or raw[4] != 10:
        raise ValueError("Invalid database greeting")
    version = raw[5:].split(b"\0", 1)[0].decode("ascii", errors="strict")
    if not re.fullmatch(r"[A-Za-z0-9.+_-]{1,100}", version):
        raise ValueError("Invalid database version")
    # MariaDB advertises 5.5.5-10.11.x for older MySQL clients.
    return version.removeprefix("5.5.5-")


def probe_stack():
    """Discover real Docker services and an already-installed WordPress site."""
    components = {}
    sites = []
    runtime = {}
    try:
        runtime, server_header = get_json("/__wm_dev_status")
        php = str(runtime.get("php_version", ""))[:80]
        wp_version = str(runtime.get("wordpress_version", ""))[:80]
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", php):
            raise ValueError("Invalid PHP runtime version")
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", wp_version):
            raise ValueError("Invalid WordPress file version")
        header = re.search(r"nginx/(\d+(?:\.\d+){1,3})", server_header)
        components["nginx"] = {"version": header.group(1) if header else None,
                               "state": "healthy", "source": "Internal Nginx HTTP probe"}
        components["php"] = {"version": php, "state": "healthy",
                             "name": "PHP runtime", "source": "Internal PHP-FPM probe"}
        components["fpm"] = {"version": php, "state": "healthy",
                             "source": "Internal PHP-FPM probe"}
    except (OSError, HTTPError, URLError, ValueError, KeyError, json.JSONDecodeError):
        components["nginx"] = {"version": None, "state": "unknown",
                               "source": "Internal Nginx HTTP probe"}
        components["php"] = {"version": None, "state": "unknown",
                             "name": "PHP runtime", "source": "Internal PHP-FPM probe"}
        components["fpm"] = {"version": None, "state": "unknown",
                             "source": "Internal PHP-FPM probe"}

    try:
        version = database_version()
        components["mariadb"] = {"version": version, "state": "healthy",
                                 "source": "Internal MariaDB handshake (no login)"}
    except (OSError, ValueError, UnicodeError):
        components["mariadb"] = {"version": None, "state": "unknown",
                                 "source": "Internal MariaDB handshake (no login)"}

    # WP-CLI is an on-demand tool rather than a continuously running service.
    components["wpcli"] = {"version": "2.12.0 (Compose configuration)", "state": "unknown",
                           "source": "Local tools image; not runtime-verified"}

    try:
        info, _ = get_json("/?rest_route=/")
        title = info.get("name", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ValueError("WordPress is not installed or API metadata invalid")
        version = runtime.get("wordpress_version")
        if isinstance(version, str) and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            sites.append({
                "name": title.strip(),
                "url": PUBLIC_WORDPRESS + "/",
                "admin_url": PUBLIC_WORDPRESS + "/wp-admin/",
                "version": version,
                "state": "healthy",
                "source": "Live WordPress REST API and local PHP version probe",
            })
    except (OSError, HTTPError, URLError, ValueError, json.JSONDecodeError):
        pass

    return components, sites


class DockerStackCollector(Collector):
    """Preserve container resource metrics but replace only container-stack rows."""

    def _collect(self):
        snapshot = super()._collect()
        components, sites = probe_stack()
        for item in snapshot["software"]:
            if item["id"] in components:
                item.update(components[item["id"]])
                if item["id"] in ("nginx", "mariadb", "fpm") and not item.get("service"):
                    item["service"] = "Docker Compose " + item["id"]
        snapshot["healthy_services"] = sum(
            item["state"] == "healthy" for item in snapshot["software"]
        )
        snapshot["service_count"] = sum(
            item.get("service") is not None for item in snapshot["software"]
        )
        snapshot["dev_sites"] = sites
        snapshot["dev_stack"] = True
        return snapshot
