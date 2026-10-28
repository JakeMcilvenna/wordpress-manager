"""Pure planning for the next increments. This module cannot execute a plan.

Kept out of the web routes until a separately tested privileged executor exists.
No secrets are accepted, generated, serialized or placed into shell commands.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def setup_plan(snapshot: dict) -> dict:
    """Inventory-driven proposal; unknown installations always need review."""
    items = []
    for software in snapshot["software"]:
        installed = software.get("version") is not None
        if installed:
            action = "preserve"
            reason = "Keep the detected version and configuration."
        elif software["state"] in ("unknown", "healthy", "stopped", "failed", "starting", "stopping"):
            action = "review"
            reason = "Detection is incomplete or a service exists; never assume it needs installation."
        else:
            action = "review-installation"
            reason = "Not detected. Confirm installation paths and approve the exact package or verified artifact."
        items.append({"component": software["id"], "version": software["version"],
                      "action": action, "reason": reason})
    baseline = {"platform": snapshot["platform"], "software": snapshot["software"]}
    plan = {"operation": "setup", "executable": False, "baseline_digest": fingerprint(baseline),
            "items": items, "gates": ["Verify VPS recovery point", "Approve exact packages and repositories",
            "Preserve existing PHP versions", "Verify phpMyAdmin remains private",
            "Install and test restricted provisioning service", "Revalidate baseline before execution"]}
    return {"id": fingerprint(plan), **plan}


def site_plan(site_id: str, domain: str, existing_ids=(), existing_domains=()) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9]{2,15}", site_id, flags=re.ASCII):
        raise ValueError("Site ID must be 3–16 lowercase ASCII letters/digits, starting with a letter")
    # Intentionally conservative: callers must supply explicit ASCII/IDNA labels.
    if not isinstance(domain, str) or domain != domain.strip() or len(domain) > 253:
        raise ValueError("Supply a valid ASCII domain without whitespace")
    domain = domain.lower()
    labels = domain.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.ASCII) for label in labels):
        raise ValueError("Supply a domain with valid ASCII/IDNA labels")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("A site requires a domain, not an IP address")
    if labels[-1].isdigit() or site_id in existing_ids or domain in {d.lower() for d in existing_domains}:
        raise ValueError("Domain/identifier is invalid or already registered")
    user = "wp_" + site_id
    plan = {"operation": "create-site", "executable": False, "site_id": site_id, "domain": domain,
            "linux_user": user, "database": user, "document_root": f"/srv/wordpress/sites/{site_id}/public",
            "fpm_pool": user, "fpm_socket": f"/run/php/{user}.sock",
            "nginx_config": f"/etc/nginx/sites-available/{user}.conf", "create_staging": False,
            "steps": ["Preflight domain, DNS, available capacity and filesystem/configuration collisions",
                "Confirm exact plan and verified recovery point", "Acquire locks and write provisioning journal",
                "Create locked, non-login Linux account and private site directory",
                "Create private database and scoped database user with fresh secrets",
                "Prepare dedicated PHP-FPM pool using explicitly selected installed PHP version",
                "Verify WordPress artifact and install as the site UID",
                "Validate candidate PHP-FPM and Nginx configuration",
                "Publish routing only after all validations; reload approved services",
                "Verify HTTP/TLS and tenant boundaries before inventory registration"],
            "gates": ["Restricted executor has passed disposable-VM integration tests",
                "Production change explicitly approved", "No existing resources will be overwritten",
                "Secrets delivered privately outside logs/arguments/source control"]}
    return {"id": fingerprint(plan), **plan}


def main():
    parser = argparse.ArgumentParser(description="Preview a plan only. Never executes system changes.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Inspect local server and print setup proposal")
    site = sub.add_parser("site", help="Print site provisioning proposal")
    site.add_argument("site_id")
    site.add_argument("domain")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            from .collector import Collector
            plan = setup_plan(Collector().snapshot())
        else:
            plan = site_plan(args.site_id, args.domain)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
