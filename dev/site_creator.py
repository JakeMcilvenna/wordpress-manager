"""Bounded local-Docker-only WordPress site creation client.

The dashboard has NO Docker socket, site files, database password, exec/sudo
capabilities or arbitrary network target. A separate constrained container
operates on one dedicated fixed site slot, over the private Compose network.
Not imported by the production process.
"""
import json
import re
from urllib import request
from urllib.error import HTTPError, URLError

from wordpress_manager.site_operations import WebsiteSpec

BASE = "http://site2-provisioner:8093"
PUBLIC_URL = "http://127.0.0.1:8092"
SITE_ID = "local-second-site"
MAX_RESPONSE = 16 * 1024
MAX_TITLE = 70


def validate_fields(title, email, domain, php="8.5"):
    """Compatibility adapter; all field rules live in the shared core."""
    spec = WebsiteSpec.from_fields(title, email, domain, php)
    if spec.php != "8.5":
        raise ValueError("Only PHP 8.5 is provisioned in this first Docker site slot")
    return {"title": spec.title, "email": spec.email, "domain": spec.domain,
            "php": spec.php, "site_id": SITE_ID, "url": PUBLIC_URL + "/"}


class LocalSiteProvisioner:
    def __init__(self, token):
        self.token = token

    def call(self, action, data=None):
        if action not in ("status", "install"):
            raise ValueError("Unsupported local provisioner operation")
        if not isinstance(self.token, str) or len(self.token) < 24:
            raise RuntimeError("Local site provisioner secret is not configured")
        if action == "install" and not isinstance(data, dict):
            raise ValueError("Missing installation plan")
        payload = None if data is None else json.dumps(data).encode()
        target = BASE + ("/status" if action == "status" else "/install")
        req = request.Request(
            target, data=payload, method="GET" if action == "status" else "POST",
            headers={"X-WM-Local-Token": self.token, "Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=45 if action == "install" else 3) as response:
                raw = response.read(MAX_RESPONSE + 1)
                code = response.status
        except HTTPError as exc:
            # Don't return arbitrary body or upstream exception text to the browser.
            if exc.code == 409:
                raise ValueError("The local site already exists. Refresh the Websites page.") from None
            if exc.code == 503:
                raise ValueError("The local WordPress container is not ready yet.") from None
            raise ValueError("Local installation service rejected this request.") from None
        except (OSError, URLError):
            raise ValueError("Local installation service is unavailable. Check the Docker containers.") from None
        if code != 200 or len(raw) > MAX_RESPONSE:
            raise ValueError("Unexpected response from local installation service")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Invalid local installation service response") from None
        if not isinstance(value, dict):
            raise ValueError("Invalid local installation service response")
        return value

    def status(self):
        return self.call("status")

    def install(self, plan):
        # Do not send any request-controlled command, URL, database name or path.
        details = validate_fields(plan["title"], plan["email"], plan["domain"], plan["php"])
        return self.call("install", {"title": details["title"], "email": details["email"], "domain": details["domain"]})


def created_site(status):
    """Return a discovered-style website record only when install is confirmed."""
    if not isinstance(status, dict) or status.get("installed") is not True:
        return None
    title = status.get("title")
    version = status.get("version")
    if not isinstance(title, str) or not 1 <= len(title) <= 70:
        return None
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        return None
    domain = status.get("domain")
    if not isinstance(domain, str) or len(domain) > 253:
        domain = "Not recorded"
    return {
        "id": SITE_ID, "name": title, "version": version, "state": "healthy",
        "domain": domain,
        "url": PUBLIC_URL + "/", "admin_url": PUBLIC_URL + "/wp-admin/",
        "source": "Dedicated Docker WordPress installation",
    }
