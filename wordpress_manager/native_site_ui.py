"""Authenticated dashboard bridge to the disposable Ubuntu website socket.

This module never imports or executes the root site executor. The *only*
website mutation path is the root-owned, allowlisted Unix-socket protocol,
which independently checks the disposable guest marker on every request.
No production website route is registered.
"""
import os
from pathlib import Path
import re
import stat

from flask import abort, redirect, render_template, request, session, url_for

DISPOSABLE_MARKER = Path("/run/wm-disposable-test")
DOCKER_MARKER = Path("/.dockerenv")
OS_RELEASE = Path("/etc/os-release")
DISPOSABLE_TEXT = "disposable Ubuntu deployment test\n"


def disposable_guest():
    """Never expose website controls on a real Ubuntu host or local preview."""
    try:
        marker = DISPOSABLE_MARKER.lstat()
        if (not stat.S_ISREG(marker.st_mode) or marker.st_uid != 0
                or marker.st_mode & 0o022):
            return False
        if DISPOSABLE_MARKER.read_text() != DISPOSABLE_TEXT:
            return False
        if not DOCKER_MARKER.is_file() or DOCKER_MARKER.is_symlink():
            return False
        system = OS_RELEASE.read_text()
        return (re.search(r"(?m)^ID=ubuntu$", system) is not None
                and re.search(r'(?m)^VERSION_ID="?24\.04"?$', system) is not None)
    except (OSError, UnicodeError):
        return False


def attach(app, controller):
    """Register guest-only site controls after authentication is configured."""

    @app.route("/development/native-sites", methods=["GET", "POST"])
    def native_site_control():
        # Recheck on *every* request: removing the marker disables an already
        # running dashboard without waiting for a restart.
        if not disposable_guest():
            abort(404)

        error, plan, result = None, None, None
        try:
            if request.method == "POST":
                action = request.form.get("action")
                if action == "plan":
                    plan = controller.call(
                        "site-plan", capability=session["capability"],
                        title=request.form.get("title", ""),
                        email=request.form.get("email", ""),
                        domain=request.form.get("domain", ""),
                        php=request.form.get("php", "8.3"),
                    )
                elif action == "apply":
                    controller.call(
                        "site-apply", capability=session["capability"],
                        plan=request.form.get("plan", ""),
                        confirmation=request.form.get("confirmation", ""),
                    )
                    # POST/redirect/GET prevents accidental repeat approvals.
                    return redirect(url_for("native_site_control"), code=303)
                elif action == "result":
                    # The root service destroys the one-time result when read.
                    # Never put a credential into the signed browser session,
                    # an audit entry, a URL, or a redirect.
                    result = controller.call(
                        "site-result", capability=session["capability"],
                        job=request.form.get("job", ""),
                    )
                else:
                    abort(400)
            jobs = controller.call(
                "site-status", capability=session["capability"]
            )["jobs"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Do not include socket paths or system exceptions in browser errors.
            error = (str(exc) if isinstance(exc, ValueError)
                     else "Disposable website control is unavailable.")
            jobs = []
        return render_template(
            "native_site_control.html", active="websites",
            title="Disposable Ubuntu websites", plan=plan, jobs=jobs,
            result=result, error=error,
        ), 409 if error else 200
