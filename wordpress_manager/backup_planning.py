"""Fail-closed backup and restore *planning*, without privileged side effects.

This is a shared contract only. It never opens archives, copies files, invokes
WP-CLI, calls Docker or enables a production/guest operation. An executor must
later verify archive bytes, a trusted ownership ledger, backups, concurrency,
rollback and a disposable Ubuntu integration test before any write is enabled.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from wordpress_manager.site_operations import WebsiteOperationUnavailable, WebsiteSpec


_SITE_ID = re.compile(r"[a-z][a-z0-9-]{2,63}\Z")
_BACKEND = re.compile(r"[a-z][a-z0-9-]{1,39}\Z")
_BACKUP_ID = re.compile(r"bkp-[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ARCHIVE_BYTES = 1024 ** 4  # Defensive planning limit; not a storage quota.


def _identity(value, pattern, name):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("Invalid " + name)
    return value


def _resources(value):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 32:
        raise ValueError("Invalid owned resource inventory")
    resources = tuple(value)
    if any(not isinstance(item, str) or not 1 <= len(item) <= 256
           or any(ord(char) < 32 or ord(char) == 127 for char in item)
           for item in resources):
        raise ValueError("Invalid owned resource inventory")
    if len(set(resources)) != len(resources):
        raise ValueError("Duplicate owned resources")
    # Lexical claims only: do not treat this as filesystem authorization.
    # Reject aliases now; a future executor must resolve real paths and
    # symlinks while holding an exclusive ownership lock.
    for resource in resources:
        if resource.startswith("/") and (
            resource == "/" or resource.endswith("/") or "//" in resource
            or "\\" in resource
            or any(part in (".", "..") for part in resource.split("/"))
        ):
            raise ValueError("Ambiguous owned resource path")
    return resources


def _domain(value):
    # The same domain normalization rules apply to creation and recovery.
    if not isinstance(value, str):
        raise ValueError("Invalid owned domain")
    domain = WebsiteSpec.from_fields(
        "Backup verification", "backup@internal.test", value, "8.3"
    ).domain
    if value != domain:
        raise ValueError("Owned domain must already be canonical")
    return domain


@dataclass(frozen=True)
class OwnedSite:
    site_id: str
    domain: str
    backend: str
    resources: tuple[str, ...]
    state: str

    @classmethod
    def from_record(cls, record):
        if not isinstance(record, dict):
            raise ValueError("Owned site inventory unavailable")
        site_id = _identity(record.get("site_id"), _SITE_ID, "site ID")
        backend = _identity(record.get("backend"), _BACKEND, "backend")
        domain = _domain(record.get("domain"))
        resources = _resources(record.get("resources"))
        state = record.get("state")
        if state != "healthy":
            raise ValueError("Site needs operator review before backup or restore")
        return cls(site_id, domain, backend, resources, state)


@dataclass(frozen=True)
class BackupManifest:
    backup_id: str
    site_id: str
    domain: str
    backend: str
    resources: tuple[str, ...]
    archive_sha256: str
    archive_bytes: int
    created_at: str

    @classmethod
    def from_record(cls, record):
        if not isinstance(record, dict) or set(record) != {
            "format_version", "backup_id", "site_id", "domain", "backend",
            "resources", "archive_sha256", "archive_bytes", "created_at",
        }:
            raise ValueError("Invalid backup manifest")
        if type(record["format_version"]) is not int or record["format_version"] != 1:
            raise ValueError("Unsupported backup manifest version")
        backup_id = _identity(record["backup_id"], _BACKUP_ID, "backup ID")
        site_id = _identity(record["site_id"], _SITE_ID, "site ID")
        backend = _identity(record["backend"], _BACKEND, "backend")
        domain = _domain(record["domain"])
        resources = _resources(record["resources"])
        digest = _identity(record["archive_sha256"], _DIGEST, "archive checksum")
        size = record["archive_bytes"]
        if type(size) is not int or not 1 <= size <= _MAX_ARCHIVE_BYTES:
            raise ValueError("Invalid backup archive size")
        created = record["created_at"]
        if not isinstance(created, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created
        ):
            raise ValueError("Invalid backup creation time")
        try:
            timestamp = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            raise ValueError("Invalid backup creation time") from None
        if timestamp > datetime.now(timezone.utc):
            raise ValueError("Backup creation time is in the future")
        return cls(backup_id, site_id, domain, backend, resources, digest, size, created)



def verify_archive_stream(manifest_record, source):
    """Check bytes from a *trusted, already-opened* binary archive stream.

    This is a read-only checksum/length check, not archive format validation,
    ledger authentication, extraction authorization or a restore operation.
    The caller must resolve the stream from a trusted server-side backup
    ledger, never from a client-supplied path. At most 1 MiB is requested
    per read, and no archive contents are retained or returned.
    """
    from hmac import compare_digest

    manifest = BackupManifest.from_record(manifest_record)
    reader = getattr(source, "read", None)
    if not callable(reader):
        raise ValueError("Trusted binary archive stream required")
    digest = sha256()
    remaining = manifest.archive_bytes
    while remaining:
        requested = min(1024 * 1024, remaining)
        chunk = reader(requested)
        if not isinstance(chunk, bytes) or not 1 <= len(chunk) <= requested:
            raise ValueError("Backup archive size or binary stream mismatch")
        digest.update(chunk)
        remaining -= len(chunk)
    # A correct prefix is insufficient: reject appended archive bytes too.
    extra = reader(1)
    if not isinstance(extra, bytes) or extra:
        raise ValueError("Backup archive size or binary stream mismatch")
    if not compare_digest(digest.hexdigest(), manifest.archive_sha256):
        raise ValueError("Backup archive SHA-256 mismatch")


class BackupPlanner:
    """Pure review-only plans using server-owned inventory, never client paths.

    inventory.site(site_id), sites(), jobs() and backup(backup_id) must be
    complete trusted server-side read-only ledger lookups. No executor is attached to this class.
    """

    def __init__(self, inventory):
        self.inventory = inventory

    def _site(self, site_id):
        site_id = _identity(site_id, _SITE_ID, "site ID")
        site = OwnedSite.from_record(self.inventory.site(site_id))
        if site.site_id != site_id:
            raise ValueError("Owned site identity mismatch")
        self._verify_ownership_snapshot(site)
        return site

    def _verify_ownership_snapshot(self, selected):
        """Fail closed if any site shares a resource/domain or a job is unfinished.

        Both lookups must be complete trusted server-side snapshots. This
        read-only check is not a concurrency lock; a future executor must
        repeat it under an exclusive lock before touching backup bytes.
        """
        records = self.inventory.sites()
        if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= 10000:
            raise ValueError("Complete site inventory required")
        site_ids, domains, resources = set(), set(), set()
        path_claims = {}
        selected_found = False
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Complete site inventory required")
            site_id = _identity(record.get("site_id"), _SITE_ID, "site ID")
            domain = _domain(record.get("domain"))
            _identity(record.get("backend"), _BACKEND, "backend")
            owned = _resources(record.get("resources"))
            # An incomplete foreign site may have unrecorded ownership claims.
            # Refuse planning even when the selected site itself is healthy.
            if record.get("state") != "healthy":
                raise ValueError("Unhealthy or incomplete site inventory requires review")
            if site_id in site_ids or domain in domains:
                raise ValueError("Ambiguous site or domain ownership")
            site_ids.add(site_id)
            domains.add(domain)
            for resource in owned:
                if resource in resources:
                    raise ValueError("Resource is claimed by multiple sites")
                resources.add(resource)
                if resource.startswith("/"):
                    path_claims[resource] = site_id
            if site_id == selected.site_id:
                selected_found = True
                if (
                    domain != selected.domain
                    or record["backend"] != selected.backend
                    or owned != selected.resources
                    or record.get("state") != selected.state
                ):
                    raise ValueError("Selected site inventory changed during review")
        if not selected_found:
            raise ValueError("Selected site missing from complete inventory")

        # A parent directory and its child cannot belong to separate sites,
        # even when their resource strings differ and inventory order changes.
        for path, owner in path_claims.items():
            parent = path.rsplit("/", 1)[0]
            while parent:
                if parent in path_claims and path_claims[parent] != owner:
                    raise ValueError("Nested path claimed by multiple sites")
                parent = parent.rsplit("/", 1)[0]

        jobs = self.inventory.jobs()
        if not isinstance(jobs, (list, tuple)) or len(jobs) > 10000:
            raise ValueError("Complete operation journal required")
        for job in jobs:
            if not isinstance(job, dict) or job.get("state") not in (
                "succeeded", "cancelled"
            ):
                raise ValueError("Unfinished or ambiguous site operation requires review")

    def plan_backup(self, site_id):
        site = self._site(site_id)
        return {
            "operation": "backup", "site_id": site.site_id,
            "domain": site.domain, "backend": site.backend,
            "resources": list(site.resources),
            "confirmation": "BACKUP " + site.site_id,
            "executable": False,
        }

    def plan_restore(self, site_id, backup_id):
        site = self._site(site_id)
        backup_id = _identity(backup_id, _BACKUP_ID, "backup ID")
        manifest = BackupManifest.from_record(self.inventory.backup(backup_id))
        if manifest.backup_id != backup_id or (
            manifest.site_id, manifest.domain, manifest.backend, manifest.resources
        ) != (site.site_id, site.domain, site.backend, site.resources):
            raise ValueError("Backup ownership does not match the selected site")
        # This is a metadata digest, not proof that archive bytes are safe.
        metadata = {
            "format_version": 1, "backup_id": manifest.backup_id,
            "site_id": manifest.site_id, "domain": manifest.domain,
            "backend": manifest.backend, "resources": list(manifest.resources),
            "archive_sha256": manifest.archive_sha256,
            "archive_bytes": manifest.archive_bytes, "created_at": manifest.created_at,
        }
        metadata_digest = sha256(json.dumps(
            metadata, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        return {
            "operation": "restore", "site_id": site.site_id,
            "domain": site.domain, "backend": site.backend,
            "resources": list(site.resources), "backup_id": manifest.backup_id,
            "manifest_digest": metadata_digest,
            "confirmation": "RESTORE " + site.site_id,
            "executable": False,
        }

    def backup(self, stored_plan, confirmation):
        # Never turn a reviewed plan into an implicit privileged action.
        raise WebsiteOperationUnavailable(
            "Backup execution is disabled until ownership, archive and integration tests pass"
        )

    def restore(self, stored_plan, confirmation):
        raise WebsiteOperationUnavailable(
            "Restore execution is disabled until verified backup, rollback and integration tests pass"
        )
