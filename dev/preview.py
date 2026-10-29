"""Local Docker-only preview with real Flask auth routes and simulated back-end ops.

This entrypoint is never installed as the systemd dashboard or restricted
provisioner. No Unix socket, root privilege, packages or host system files.
"""
import hashlib
import hmac
import os
from pathlib import Path
import secrets

from flask import abort, redirect, render_template, request, session, url_for
from wordpress_manager import create_app
from dev.stack_discovery import DockerStackCollector
from dev.site_registry import SiteRegistry, SITE_ID
from dev.site_creator import LocalSiteProvisioner, created_site
from dev.docker_sites import DockerWebsiteBackend
from wordpress_manager.site_operations import WebsiteService


class PreviewController:
    """In-memory stand-in for the private provisioner, not a privileged executor."""

    def __init__(self, password):
        self.password = password
        self.capabilities = set()
        self.plans = {}
        self.jobs = []

    def call(self, op, **fields):
        if op == "login":
            supplied = fields.get("password", "")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self.password):
                raise ValueError("Invalid preview credentials")
            capability = secrets.token_urlsafe(32)
            self.capabilities.add(capability)
            return {"capability": capability}

        capability = fields.get("capability")
        if capability not in self.capabilities:
            raise ValueError("Preview session is not active")

        if op == "logout":
            self.capabilities.discard(capability)
            return {}
        if op == "status":
            return {"jobs": list(self.jobs)}
        if op == "plan":
            php = fields.get("php", "8.3")
            if php not in ("8.3", "8.4", "8.5"):
                raise ValueError("Unsupported PHP version")
            plan_id = secrets.token_hex(16)
            plan = {
                "id": plan_id,
                "digest": hashlib.sha256(("preview:" + plan_id + ":" + php).encode()).hexdigest(),
                "packages": {"php" + php + "-fpm": "SIMULATED — no package installation"},
                "artifacts": [],
                "artifact_pins": {},
                "preserved_php": [],
                "notice": "LOCAL PREVIEW ONLY — no software, services or databases are changed.",
            }
            self.plans[plan_id] = capability
            return plan
        if op == "apply":
            plan = fields.get("plan")
            if fields.get("confirmation") != "INSTALL MISSING COMPONENTS":
                raise ValueError("Incorrect confirmation")
            if self.plans.get(plan) != capability:
                raise ValueError("Unknown or expired preview plan")
            del self.plans[plan]
            job_id = secrets.token_hex(8)
            self.jobs.insert(0, {
                "id": job_id,
                "state": "simulated",
                "detail": "Preview confirmation recorded. No system changes performed.",
            })
            return {"job": job_id}
        raise ValueError("Unsupported preview operation")


def session_secret(path=None):
    """Stable across live reloads but never shared with production."""
    path = Path(path) if path is not None else Path("/tmp/wm-preview-session-key")
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        value = secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return session_secret(path)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
    if len(value) < 64:
        raise RuntimeError("Invalid local preview session key")
    return value


def create_preview_app(*, password=None, secret_key=None, observer=None, registry_path=None, site_provisioner=None):
    password = password if password is not None else os.environ.get("WM_PREVIEW_PASSWORD", "preview-only")
    if not password:
        raise ValueError("The local preview password cannot be empty")
    controller = PreviewController(password)
    app = create_app({
        # The existing app only accepts injected authentication settings in
        # TESTING mode. Production still reads its root-owned settings instead.
        "TESTING": True,
        "TEMPLATES_AUTO_RELOAD": True,
        "DEV_PREVIEW": True,
        "AUTH_SETTINGS": {"secret_key": secret_key or session_secret(), "provisioning": True},
        "CONTROLLER": controller,
    }, collector=observer or (DockerStackCollector()
                            if os.environ.get("WM_DEV_WORDPRESS_INTERNAL_URL") else None))

    registry = SiteRegistry(registry_path) if registry_path is not None else SiteRegistry()
    site_creator = site_provisioner or LocalSiteProvisioner(
        os.environ.get("WM_DEV_BROKER_TOKEN", "")
    )
    site_service = WebsiteService(DockerWebsiteBackend(site_creator))
    app.extensions["website_service"] = site_service

    def second_site_status():
        if os.environ.get("WM_DEV_SECOND_SITE_ENABLED") != "1":
            return {"ready": False, "installed": False}
        try:
            return site_service.inspect()
        except (OSError, ValueError, RuntimeError):
            return {"ready": False, "installed": False}

    def known_site():
        if not os.environ.get("WM_DEV_WORDPRESS_INTERNAL_URL"):
            return None
        discovered = app.extensions["collector"].snapshot().get("dev_sites", [])
        return discovered[0] if discovered else None

    @app.context_processor
    def preview_labels():
        enabled = bool(os.environ.get("WM_DEV_WORDPRESS_INTERNAL_URL"))
        snapshot = app.extensions["collector"].snapshot() if enabled else {}
        try:
            managed_sites = site_service.list_sites() if os.environ.get("WM_DEV_SECOND_SITE_ENABLED") == "1" else []
        except (OSError, ValueError, RuntimeError):
            managed_sites = []
        return {
            "dev_preview": True,
            "dev_stack_enabled": enabled,
            "dev_sites": list(snapshot.get("dev_sites", [])) + managed_sites,
            "dev_site_registered": registry.is_registered() if enabled else False,
            "dev_can_create_site": os.environ.get("WM_DEV_SECOND_SITE_ENABLED") == "1",
        }

    @app.post("/dev/sites/register")
    def dev_register_site():
        # The standard authentication, CSRF and Origin checks run before this.
        # No browser-provided site URL, path or command is accepted.
        if known_site() is None:
            abort(409)
        registry.register()
        return redirect(url_for("dev_site_details"))

    @app.post("/dev/sites/unregister")
    def dev_unregister_site():
        # This only drops local registry metadata; never touches WordPress.
        if not os.environ.get("WM_DEV_WORDPRESS_INTERNAL_URL"):
            abort(404)
        registry.unregister()
        return redirect(url_for("page", name="websites"))

    @app.get("/dev/sites/local-wordpress")
    def dev_site_details():
        site = known_site()
        if site is None:
            abort(404)
        return render_template(
            "dev_site_details.html", title="Website details",
            active="websites", site=site, registered=registry.is_registered(),
        )

    @app.route("/dev/sites/create", methods=["GET", "POST"])
    def dev_create_site():
        if os.environ.get("WM_DEV_SECOND_SITE_ENABLED") != "1":
            abort(404)
        current = second_site_status()
        error = None
        plan = session.get("wm_site_creation_plan")
        if request.method == "GET" and request.args.get("edit") == "1":
            session.pop("wm_site_creation_plan", None)
            plan = None
        result = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "plan":
                if current.get("installed"):
                    error = "The local second-site slot is already installed."
                else:
                    try:
                        plan = site_service.plan(
                            request.form.get("title", ""),
                            request.form.get("email", ""),
                            request.form.get("domain", ""),
                            request.form.get("php", "8.5"),
                        )
                        session["wm_site_creation_plan"] = plan
                    except ValueError as exc:
                        error = str(exc)
                        plan = None
            elif action == "create":
                try:
                    result = site_service.create(plan, request.form.get("confirmation", ""))
                    session.pop("wm_site_creation_plan", None)
                    plan = None
                except (ValueError, RuntimeError) as exc:
                    error = str(exc)
            else:
                abort(400)
        return render_template(
            "dev_create_site.html", active="websites", title="Create WordPress website",
            plan=plan, current=current, result=result, error=error,
        ), 409 if error else 200

    @app.get("/dev/sites/second-site")
    def dev_created_site_details():
        site = created_site(second_site_status())
        if site is None:
            abort(404)
        return render_template("dev_created_site.html", active="websites",
                               title="Created WordPress website", site=site)

    @app.get("/dev/stack")
    def dev_stack():
        # Authentication is handled by the standard Flask before-request gate.
        # Reuse fixed-target cached probes instead of following WordPress redirects.
        enabled = bool(os.environ.get("WM_DEV_WORDPRESS_INTERNAL_URL"))
        snapshot = app.extensions["collector"].snapshot() if enabled else {}
        nginx = next((row for row in snapshot.get("software", [])
                      if row["id"] == "nginx"), None)
        status = "Connected" if nginx and nginx["state"] == "healthy" else (
            "Unavailable" if enabled else "Not configured")
        return render_template("dev_stack.html", title="Local WordPress stack",
                               active="websites", dev_stack_status=status,
                               dev_stack_rows=snapshot.get("software", []))

    return app


def main():
    # Live reload, no interactive debugger, no public bind. The Docker port
    # relay terminates inside the same container and host port is loopback only.
    app = create_preview_app()
    app.run(host="127.0.0.1", port=8080, debug=False, use_reloader=True)


if __name__ == "__main__":
    main()
