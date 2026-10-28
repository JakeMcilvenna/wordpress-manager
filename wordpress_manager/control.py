"""Unprivileged authentication/UI client. Only fixed protocol verbs cross the socket."""
from datetime import timedelta
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat

from flask import abort, redirect, render_template, request, session, url_for

CONFIG = Path("/etc/wordpress-manager/web.json")
SOCKET = "/run/wordpress-manager/control.sock"


def settings():
    if not CONFIG.exists():
        if os.environ.get("WM_REQUIRE_AUTH") == "1":
            raise RuntimeError("Required authentication configuration is missing")
        return None
    info = CONFIG.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o027:
        raise RuntimeError("Unsafe authentication configuration")
    value = json.loads(CONFIG.read_text())
    if len(value.get("secret_key", "")) < 64:
        raise RuntimeError("Invalid session key")
    return value


class Controller:
    def call(self, op, **fields):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(30)
            connection.connect(SOCKET)
            connection.sendall((json.dumps({"op": op, **fields}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n") and len(data) <= 2 * 1024 * 1024:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
        response = json.loads(data)
        if not response["ok"]:
            raise ValueError(response["error"])
        return response["result"]


def attach(app):
    value = app.config.get("AUTH_SETTINGS") if app.testing else settings()
    if not value:
        app.config["AUTH_ENABLED"] = False
        return
    app.config.update(AUTH_ENABLED=True, SECRET_KEY=value["secret_key"],
                      PROVISIONING_ENABLED=value.get("provisioning", False),
                      SESSION_COOKIE_NAME="wm_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=False,
                      PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
                      SESSION_REFRESH_EACH_REQUEST=False, MAX_CONTENT_LENGTH=4096)
    controller = app.config.get("CONTROLLER") or Controller()
    app.extensions["controller"] = controller

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(32)
        return session["csrf"]

    @app.context_processor
    def context():
        return {"csrf_token": csrf, "auth_enabled": True,
                "control_enabled": app.config["PROVISIONING_ENABLED"], "signed_in": bool(session.get("capability"))}

    @app.before_request
    def access():
        if request.endpoint in ("health", "static") or request.endpoint is None:
            return
        if request.method == "POST":
            supplied = request.form.get("csrf", "")
            if not session.get("csrf") or not hmac.compare_digest(session["csrf"], supplied):
                abort(403)
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                abort(403)
        if request.endpoint != "login" and not session.get("capability"):
            return redirect(url_for("login")) if request.method == "GET" else abort(401)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            try:
                if request.form.get("username") != "admin":
                    # Still use the throttled authentication boundary for invalid user names.
                    password = ""
                else:
                    password = request.form.get("password", "")
                result = controller.call("login", password=password)
                session.clear()
                session.permanent = True
                session["capability"] = result["capability"]
                csrf()
                return redirect(url_for("overview"))
            except (OSError, ValueError):
                error = "Sign-in failed. Check your credentials or wait a minute before retrying. The provisioning service must be running."
        return render_template("login.html", title="Sign in", error=error), 401 if error else 200

    @app.post("/logout")
    def logout():
        try:
            controller.call("logout", capability=session["capability"])
        except (OSError, ValueError):
            pass  # The capability still expires on the server within 30 minutes.
        session.clear()
        return redirect(url_for("login"))

    @app.route("/setup/control", methods=["GET", "POST"])
    def stack_control():
        if not app.config["PROVISIONING_ENABLED"]:
            abort(403)
        error, plan, jobs = None, None, []
        try:
            if request.method == "POST":
                if request.form.get("action") == "plan":
                    plan = controller.call("plan", capability=session["capability"], php=request.form.get("php", "8.3"))
                elif request.form.get("action") == "apply":
                    controller.call("apply", capability=session["capability"], plan=request.form.get("plan"),
                                    confirmation=request.form.get("confirmation", ""))
                    return redirect(url_for("stack_control"))
                else:
                    abort(400)
            jobs = controller.call("status", capability=session["capability"])["jobs"]
        except (OSError, ValueError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else "Provisioning service unavailable. Inspect its system journal and retry."
        return render_template("control.html", active="setup", title="Install server software", plan=plan, jobs=jobs, error=error), 409 if error else 200
