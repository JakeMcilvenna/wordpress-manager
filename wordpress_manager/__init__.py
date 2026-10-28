"""Private WordPress operations console. The web process has no write capabilities."""
from collections import deque
from datetime import datetime, timezone
import ipaddress
import logging
import os
from threading import Lock

from flask import Flask, abort, jsonify, render_template, request

from .collector import Collector

PAGES = {
    "overview": ("Overview", "A clear view of your server."),
    "setup": ("Server Setup", "Your server stack, detected locally."),
    "websites": ("Websites", "Independent sites. One place to manage them."),
    "staging": ("Staging", "A private space to test your next change."),
    "backups": ("Backups", "Recovery comes before deployment."),
    "security": ("Security", "Know what is protected. Know what needs review."),
}


def create_app(config=None, collector=None):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("Run the dashboard as an unprivileged user, never root")
    app = Flask(__name__)
    app.config.from_mapping(TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
                            MAX_CONTENT_LENGTH=1024, MAX_FORM_MEMORY_SIZE=1024)
    if config:
        app.config.update(config)
    observer = collector or Collector()
    app.extensions["collector"] = observer
    events = deque(maxlen=100)
    event_lock = Lock()
    app.extensions["audit_events"] = events
    logger = logging.getLogger("wordpress_manager.audit")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    logger.propagate = False

    @app.before_request
    def local_access_only():
        try:
            address = ipaddress.ip_address(request.remote_addr or "")
            local = address.is_loopback or (getattr(address, "ipv4_mapped", None) is not None and address.ipv4_mapped.is_loopback)
        except ValueError:
            local = False
        if not local or request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not (
                app.config.get("AUTH_ENABLED") and request.method == "POST"
                and (request.endpoint in ("login", "logout", "stack_control")
                     or (app.config.get("DEV_PREVIEW") is True and
                         request.endpoint in ("dev_register_site", "dev_unregister_site")))):
            abort(405)

    from .control import attach
    attach(app)

    @app.after_request
    def secure_response(response):
        response.headers.update({
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin", "Cache-Control": "no-store",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cross-Origin-Resource-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin",
        })
        if request.endpoint and request.endpoint != "static" and request.method == "GET":
            # Store endpoint names only: no arbitrary URLs, queries, secrets or addresses.
            event = {"time": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                     "action": "Read " + request.endpoint.replace("_", " "), "status": response.status_code}
            with event_lock:
                events.appendleft(event)
            logger.info("endpoint=%s status=%s", request.endpoint, response.status_code)
        return response

    @app.template_filter("size")
    def size(value):
        if value is None:
            return "Unavailable"
        return f"{value / 1024**3:.1f} GB"

    @app.template_filter("uptime")
    def uptime(value):
        if value is None:
            return "Unavailable"
        days, rest = divmod(int(value), 86400)
        return f"{days}d {rest // 3600}h"

    @app.context_processor
    def navigation():
        return {"pages": PAGES, "state_labels": {"healthy": "Running", "installed": "Installed",
                "stopped": "Stopped", "failed": "Failed", "missing": "Not found", "unknown": "Unknown",
                "not-detected": "Not detected", "starting": "Starting", "stopping": "Stopping"}}

    @app.get("/")
    def overview():
        return page("overview")

    @app.get("/view/<name>")
    def page(name):
        if name not in PAGES:
            abort(404)
        snapshot = observer.snapshot()
        with event_lock:
            audit = list(events)
        return render_template("page.html", active=name, title=PAGES[name][0],
                               subtitle=PAGES[name][1], snapshot=snapshot, audit=audit)

    @app.get("/fragments/overview")
    def overview_fragment():
        return render_template("fragments/overview.html", snapshot=observer.snapshot())

    @app.get("/api/snapshot")
    def snapshot_api():
        return jsonify(observer.snapshot())

    @app.get("/healthz")
    def health():
        # Dashboard health is independent of monitored service health.
        return jsonify(status="ok", mode="private")

    @app.errorhandler(404)
    @app.errorhandler(403)
    @app.errorhandler(405)
    @app.errorhandler(400)
    def error(error):
        if error.code == 400:
            # Host validation can fail before Flask has a URL adapter. Do not
            # render templates with url_for in that case.
            return "Bad request. Use the loopback dashboard URL.", 400
        return render_template("error.html", code=error.code), error.code

    return app
