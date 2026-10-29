"""Read-only, fail-closed website deletion review; no deletion executor.

The trusted inventory adapter must obtain site ownership, cross-site resource
claims, recovery evidence and job states from the server, not the browser.
This module never opens paths, touches Docker or invokes a privileged service.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from wordpress_manager.site_operations import WebsiteOperationUnavailable, WebsiteSpec


_SITE_ID = re.compile(r"[a-z][a-z0-9-]{2,63}\Z")
_BACKEND = re.compile(r"[a-z][a-z0-9-]{1,39}\Z")
_BACKUP_ID = re.compile(r"bkp-[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FINISHED_JOBS = frozenset(("succeeded", "cancelled"))


def _identity(value, pattern, label):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("Invalid " + label)
    return value


def _domain(value):
    if not isinstance(value, str):
        raise ValueError("Invalid owned domain")
    canonical = WebsiteSpec.from_fields(
        "Ownership review", "review@internal.test", value, "8.3"
    ).domain
    if value != canonical:
        raise ValueError("Owned domain must already be canonical")
    return canonical


def _resources(value):
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 32:
        raise ValueError("Missing or ambiguous owned resources")
    if any(not isinstance(resource, str) or not 1 <= len(resource) <= 256
           or any(ord(char) < 32 or ord(char) == 127 for char in resource)
           for resource in value):
        raise ValueError("Invalid owned resource")
    if len(set(value)) != len(value):
        raise ValueError("Duplicate owned resource")
    return tuple(value)


def _site(record):
    if not isinstance(record, dict):
        raise ValueError("Trusted site inventory unavailable")
    return {
        "site_id": _identity(record.get("site_id"), _SITE_ID, "site ID"),
        "domain": _domain(record.get("domain")),
        "backend": _identity(record.get("backend"), _BACKEND, "backend"),
        "resources": _resources(record.get("resources")),
        "state": record.get("state"),
    }


def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise ValueError("Invalid recovery verification timestamp")
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise ValueError("Invalid recovery verification timestamp") from None
    if instant > datetime.now(timezone.utc):
        raise ValueError("Recovery verification timestamp is in the future")
    return value


def _recovery(record, site):
    # This is ledger metadata, not cryptographic authentication or a tested
    # restore. It can never authorize deletion without a separate executor.
    expected = {
        "backup_id", "site_id", "domain", "backend", "resources",
        "archive_sha256", "archive_bytes", "verified_at", "state",
    }
    if not isinstance(record, dict) or set(record) != expected:
        raise ValueError("Verified recovery record is unavailable")
    if record["state"] != "verified":
        raise ValueError("Recovery point needs verification")
    if (
        record["site_id"] != site["site_id"]
        or record["domain"] != site["domain"]
        or record["backend"] != site["backend"]
        or _resources(record["resources"]) != site["resources"]
    ):
        raise ValueError("Recovery ownership does not match the selected site")
    backup_id = _identity(record["backup_id"], _BACKUP_ID, "backup ID")
    digest = _identity(record["archive_sha256"], _SHA256, "archive checksum")
    size = record["archive_bytes"]
    if type(size) is not int or not 1 <= size <= 1024 ** 4:
        raise ValueError("Invalid recovery archive size")
    return {
        "backup_id": backup_id,
        "archive_sha256": digest,
        "archive_bytes": size,
        "verified_at": _utc(record["verified_at"]),
    }


class DeletionPlanner:
    """Plan-only guard around trusted server-owned records.

    inventory.site(site_id), sites(), recovery_point(site_id) and jobs()
    must be server-side reads. Every call fails closed on incomplete data.
    """

    def __init__(self, inventory):
        self.inventory = inventory

    def plan_delete(self, site_id):
        site_id = _identity(site_id, _SITE_ID, "site ID")
        selected = _site(self.inventory.site(site_id))
        if selected["site_id"] != site_id or selected["state"] != "healthy":
            raise ValueError("Selected site needs ownership or health review")

        records = self.inventory.sites()
        if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= 10000:
            raise ValueError("Complete site inventory required")
        claims = set()
        site_ids = set()
        domains = set()
        found = 0
        for raw in records:
            candidate = _site(raw)
            if candidate["site_id"] in site_ids or candidate["domain"] in domains:
                raise ValueError("Ambiguous site or domain ownership")
            site_ids.add(candidate["site_id"])
            domains.add(candidate["domain"])
            for resource in candidate["resources"]:
                if resource in claims:
                    raise ValueError("Resource is claimed by multiple sites")
                claims.add(resource)
            if candidate["site_id"] == site_id:
                found += 1
                if candidate != selected:
                    raise ValueError("Site inventory changed during review")
        if found != 1:
            raise ValueError("Selected site missing from complete inventory")

        jobs = self.inventory.jobs()
        if not isinstance(jobs, (list, tuple)) or len(jobs) > 10000:
            raise ValueError("Complete operation journal required")
        for job in jobs:
            if not isinstance(job, dict) or job.get("state") not in _FINISHED_JOBS:
                raise ValueError("Unfinished or ambiguous site operation requires review")

        recovery = _recovery(self.inventory.recovery_point(site_id), selected)
        snapshot = {"site": selected, "recovery": recovery}
        fingerprint = sha256(json.dumps(
            snapshot, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        return {
            "operation": "delete", "site_id": site_id,
            "domain": selected["domain"], "backend": selected["backend"],
            "resources": list(selected["resources"]),
            "recovery": recovery,
            "snapshot_digest": fingerprint,
            "confirmation": "DELETE " + site_id + " " + selected["domain"],
            "gates": [
                "Revalidate all ownership and jobs under an exclusive lock",
                "Prove archive authenticity and successful disposable restore",
                "Approve site-scoped staging, rollback and recovery",
                "Record an immutable deletion audit trail",
            ],
            "executable": False,
        }

    def delete(self, plan, confirmation):
        raise WebsiteOperationUnavailable(
            "Website deletion is disabled pending reviewed recovery integration"
        )
