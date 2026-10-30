"""Backup/restore preflight must never select another site's resources."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO

import pytest

from wordpress_manager.backup_planning import BackupManifest, BackupPlanner, verify_archive_stream
from wordpress_manager.site_operations import WebsiteOperationUnavailable


SITE = {
    "site_id": "wm-firstsite001", "domain": "first.test",
    "backend": "ubuntu-disposable", "state": "healthy",
    "resources": ["/srv/wm-firstsite001", "linux-user:wmsfirstsite"],
}
OTHER = {
    "site_id": "wm-secondsite02", "domain": "second.test",
    "backend": "ubuntu-disposable", "state": "healthy",
    "resources": ["/srv/wm-secondsite02", "linux-user:wmssecondsite"],
}
BACKUP_ID = "bkp-" + "a" * 32
MANIFEST = {
    "format_version": 1, "backup_id": BACKUP_ID,
    "site_id": SITE["site_id"], "domain": SITE["domain"],
    "backend": SITE["backend"], "resources": SITE["resources"],
    "archive_sha256": "b" * 64, "archive_bytes": 4096,
    "created_at": "2026-01-01T00:00:00Z",
}


class Ledger:
    def __init__(self):
        self.site_records = {item["site_id"]: deepcopy(item) for item in (SITE, OTHER)}
        self.backups = {BACKUP_ID: deepcopy(MANIFEST)}
        self.writes = 0

    def site(self, site_id):
        return deepcopy(self.site_records.get(site_id))

    def sites(self):
        return [deepcopy(record) for record in self.site_records.values()]

    def jobs(self):
        return []

    def backup(self, backup_id):
        return deepcopy(self.backups.get(backup_id))


def test_backup_plan_uses_only_owned_inventory_and_never_executes():
    ledger = Ledger()
    planner = BackupPlanner(ledger)
    plan = planner.plan_backup(SITE["site_id"])
    assert plan == {
        "operation": "backup", "site_id": SITE["site_id"],
        "domain": SITE["domain"], "backend": SITE["backend"],
        "resources": SITE["resources"], "confirmation": "BACKUP " + SITE["site_id"],
        "executable": False,
    }
    assert planner.plan_backup(OTHER["site_id"])["resources"] == OTHER["resources"]
    assert ledger.writes == 0
    with pytest.raises(WebsiteOperationUnavailable, match="disabled"):
        planner.backup(plan, plan["confirmation"])
    assert ledger.writes == 0


def test_restore_plan_requires_exact_site_and_backup_ownership():
    ledger = Ledger()
    planner = BackupPlanner(ledger)
    plan = planner.plan_restore(SITE["site_id"], BACKUP_ID)
    assert plan["operation"] == "restore"
    assert plan["site_id"] == SITE["site_id"]
    assert plan["resources"] == SITE["resources"]
    assert plan["confirmation"] == "RESTORE " + SITE["site_id"]
    assert plan["executable"] is False
    assert len(plan["manifest_digest"]) == 64
    with pytest.raises(ValueError, match="ownership"):
        planner.plan_restore(OTHER["site_id"], BACKUP_ID)
    with pytest.raises(WebsiteOperationUnavailable, match="disabled"):
        planner.restore(plan, plan["confirmation"])
    assert ledger.writes == 0


@pytest.mark.parametrize("changed", [
    {"site_id": OTHER["site_id"]},
    {"domain": OTHER["domain"]},
    {"backend": "local-docker"},
    {"resources": OTHER["resources"]},
    {"resources": list(reversed(SITE["resources"]))},
    {"backup_id": "bkp-" + "c" * 32},
])
def test_restore_rejects_foreign_or_tampered_backup(changed):
    ledger = Ledger()
    ledger.backups[BACKUP_ID].update(changed)
    with pytest.raises(ValueError, match="ownership"):
        BackupPlanner(ledger).plan_restore(SITE["site_id"], BACKUP_ID)


@pytest.mark.parametrize("changed", [
    {"format_version": 2},
    {"format_version": True},
    {"archive_sha256": "not-a-sha256"},
    {"archive_sha256": "A" * 64},
    {"archive_bytes": 0},
    {"archive_bytes": True},
    {"archive_bytes": 1024 ** 4 + 1},
    {"created_at": "2026-02-30T00:00:00Z"},
    {"created_at": "2099-01-01T00:00:00Z"},
    {"created_at": "2026-01-01T00:00:00+00:00"},
    {"resources": [SITE["resources"][0]] * 2},
    {"resources": ["/srv/unsafe\n"]},
    {"domain": "FIRST.TEST"},
])
def test_invalid_manifest_fails_closed(changed):
    ledger = Ledger()
    ledger.backups[BACKUP_ID].update(changed)
    with pytest.raises(ValueError):
        BackupPlanner(ledger).plan_restore(SITE["site_id"], BACKUP_ID)


def test_manifest_rejects_unknown_fields_and_never_trusts_archive_paths():
    ledger = Ledger()
    ledger.backups[BACKUP_ID]["archive_path"] = "/etc/shadow"
    with pytest.raises(ValueError, match="manifest"):
        BackupPlanner(ledger).plan_restore(SITE["site_id"], BACKUP_ID)
    with pytest.raises(ValueError, match="manifest"):
        BackupManifest.from_record({"archive_path": "/tmp/attacker.tar"})


@pytest.mark.parametrize("changed", [
    {"state": "needs-review"},
    {"state": "provisioning"},
    {"state": "suspended"},
    {"domain": "FIRST.TEST."},
    {"site_id": OTHER["site_id"]},
    {"resources": []},
    {"resources": ["/srv/owned", "/srv/owned"]},
    {"resources": ["/srv/owned\x00bad"]},
    {"backend": "ubuntu/disposable"},
])
def test_unhealthy_or_ambiguous_site_ownership_blocks_backup_and_restore(changed):
    ledger = Ledger()
    ledger.site_records[SITE["site_id"]].update(changed)
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)


@pytest.mark.parametrize("site_id", [
    "", "../etc/passwd", "wm-firstsite001/../", "WM-FIRSTSITE001",
    "wm-firstsite001\n", "a" * 100, None,
])
def test_user_supplied_site_id_cannot_select_arbitrary_paths(site_id):
    ledger = Ledger()
    with pytest.raises(ValueError, match="site ID"):
        BackupPlanner(ledger).plan_backup(site_id)


def test_unknown_site_or_backup_fails_closed():
    ledger = Ledger()
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError):
        planner.plan_backup("wm-unknownsite")
    with pytest.raises(ValueError):
        planner.plan_restore(SITE["site_id"], "bkp-" + "d" * 32)


def test_manifest_metadata_digest_changes_if_archive_metadata_changes():
    ledger = Ledger()
    planner = BackupPlanner(ledger)
    original = planner.plan_restore(SITE["site_id"], BACKUP_ID)
    ledger.backups[BACKUP_ID]["archive_sha256"] = "c" * 64
    changed = planner.plan_restore(SITE["site_id"], BACKUP_ID)
    assert original["manifest_digest"] != changed["manifest_digest"]


def _manifest_for_bytes(payload):
    manifest = deepcopy(MANIFEST)
    manifest["archive_bytes"] = len(payload)
    manifest["archive_sha256"] = sha256(payload).hexdigest()
    return manifest


def test_read_only_archive_verifier_checks_exact_bytes_and_leaves_stream_open():
    payload = b"trusted test backup data" * 100
    stream = BytesIO(payload)
    assert verify_archive_stream(_manifest_for_bytes(payload), stream) is None
    assert stream.closed is False


@pytest.mark.parametrize("altered", [
    lambda data: data[:-1],
    lambda data: data + b"x",
    lambda data: b"X" + data[1:],
])
def test_archive_verifier_rejects_truncated_appended_or_modified_bytes(altered):
    payload = b"trusted test backup data"
    with pytest.raises(ValueError, match="archive"):
        verify_archive_stream(_manifest_for_bytes(payload), BytesIO(altered(payload)))


def test_archive_verifier_limits_read_requests_and_checks_trailing_byte():
    payload = b"x" * (1024 * 1024 + 9)

    class TrackingStream:
        def __init__(self, data):
            self.source = BytesIO(data)
            self.requests = []

        def read(self, amount):
            self.requests.append(amount)
            return self.source.read(amount)

    stream = TrackingStream(payload)
    verify_archive_stream(_manifest_for_bytes(payload), stream)
    assert stream.requests == [1024 * 1024, 9, 1]


def test_archive_verifier_rejects_nonbinary_and_nonconforming_streams():
    payload = b"trusted data"
    manifest = _manifest_for_bytes(payload)

    class TextStream:
        def read(self, amount):
            return "text instead of bytes"

    class OversizedStream:
        def read(self, amount):
            return b"x" * (amount + 1)

    for stream in (None, TextStream(), OversizedStream()):
        with pytest.raises(ValueError, match="stream|size"):
            verify_archive_stream(manifest, stream)


def test_invalid_archive_metadata_rejected_before_any_stream_read():
    manifest = _manifest_for_bytes(b"data")
    manifest["archive_bytes"] = 0

    class NeverRead:
        def read(self, amount):
            raise AssertionError("invalid manifest must fail before reading")

    with pytest.raises(ValueError, match="size"):
        verify_archive_stream(manifest, NeverRead())


def test_backup_and_restore_reject_cross_site_resource_or_domain_claims():
    ledger = Ledger()
    ledger.site_records[OTHER["site_id"]]["resources"].append(SITE["resources"][0])
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="Resource"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="Resource"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)

    ledger = Ledger()
    ledger.site_records[OTHER["site_id"]]["domain"] = SITE["domain"]
    with pytest.raises(ValueError, match="domain"):
        BackupPlanner(ledger).plan_backup(SITE["site_id"])


@pytest.mark.parametrize("resource", [
    "/", "/srv/", "/srv//site", "/srv/./site", "/srv/site/../other",
    "/srv/site\\\\other",
])
def test_backup_and_restore_reject_aliased_paths_even_in_consistent_ledger(resource):
    ledger = Ledger()
    ledger.site_records[SITE["site_id"]]["resources"][0] = resource
    ledger.backups[BACKUP_ID]["resources"][0] = resource
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="resource path"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="resource path"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)


@pytest.mark.parametrize("foreign_path", [
    SITE["resources"][0] + "/uploads",
    "/srv",
])
@pytest.mark.parametrize("reverse_inventory", [False, True])
def test_backup_and_restore_reject_nested_cross_site_paths(
    foreign_path, reverse_inventory
):
    ledger = Ledger()
    ledger.site_records[OTHER["site_id"]]["resources"][0] = foreign_path
    if reverse_inventory:
        ledger.sites = lambda: [
            deepcopy(ledger.site_records[OTHER["site_id"]]),
            deepcopy(ledger.site_records[SITE["site_id"]]),
        ]
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="Nested path"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="Nested path"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)


@pytest.mark.parametrize("state", [
    "provisioning", "needs-review", "failed", "suspended", None,
])
def test_foreign_unhealthy_site_blocks_backup_and_restore_planning(state):
    ledger = Ledger()
    ledger.site_records[OTHER["site_id"]]["state"] = state
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="Unhealthy or incomplete"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="Unhealthy or incomplete"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)


def test_sibling_path_prefix_does_not_block_backup_planning():
    ledger = Ledger()
    ledger.site_records[OTHER["site_id"]]["resources"][0] = (
        SITE["resources"][0] + "-sibling"
    )
    assert BackupPlanner(ledger).plan_backup(SITE["site_id"])["executable"] is False


def test_backup_and_restore_require_complete_consistent_site_snapshot():
    ledger = Ledger()
    planner = BackupPlanner(ledger)
    ledger.sites = lambda: [deepcopy(OTHER)]
    with pytest.raises(ValueError, match="missing"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="missing"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)

    ledger = Ledger()
    ledger.sites = lambda: [deepcopy(SITE), deepcopy(SITE)]
    with pytest.raises(ValueError, match="Ambiguous"):
        BackupPlanner(ledger).plan_backup(SITE["site_id"])

    ledger = Ledger()
    ledger.sites = lambda: None
    with pytest.raises(ValueError, match="Complete"):
        BackupPlanner(ledger).plan_backup(SITE["site_id"])

    ledger = Ledger()
    ledger.sites = lambda: [dict(SITE, resources=["/srv/different-resource"]), deepcopy(OTHER)]
    with pytest.raises(ValueError, match="changed"):
        BackupPlanner(ledger).plan_restore(SITE["site_id"], BACKUP_ID)


@pytest.mark.parametrize("jobs", [
    None, [None], [{"state": "running"}], [{"state": "failed"}],
    [{"state": "queued"}], [{"state": "interrupted"}],
])
def test_backup_and_restore_refuse_unfinished_or_ambiguous_jobs(jobs):
    ledger = Ledger()
    ledger.jobs = lambda: jobs
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="journal|operation"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="journal|operation"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)


@pytest.mark.parametrize("metadata", [
    float("nan"), float("inf"), object(), {1: "number", "key": "text"},
])
def test_malformed_completed_job_metadata_blocks_backup_and_restore(metadata):
    ledger = Ledger()
    ledger.jobs = lambda: [{"state": "succeeded", "metadata": metadata}]
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="journal"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="journal"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)
    assert ledger.writes == 0


def test_completed_operation_journal_is_accepted_without_enabling_writes():
    ledger = Ledger()
    ledger.jobs = lambda: [{"state": "succeeded"}, {"state": "cancelled"}]
    planner = BackupPlanner(ledger)
    assert planner.plan_backup(SITE["site_id"])["executable"] is False
    assert planner.plan_restore(SITE["site_id"], BACKUP_ID)["executable"] is False
    assert ledger.writes == 0


@pytest.mark.parametrize("resource", [
    "srv/wm-firstsite001", "../srv/wm-firstsite001", "./srv/site",
    "wm-firstsite001", "linux-user:", "linux-user:.hidden",
    "linux-user:/etc/shadow", "linux-user:account/other",
    "linux-user:account\\other", "linux-user:account:other",
])
def test_backup_and_restore_reject_relative_or_untyped_selected_claims(resource):
    ledger = Ledger()
    ledger.site_records[SITE["site_id"]]["resources"][0] = resource
    ledger.backups[BACKUP_ID]["resources"][0] = resource
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="resource identifier"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="resource identifier"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)


@pytest.mark.parametrize("resource", [
    "srv/other", "untyped", "linux-user:.hidden", "mariadb-database:other/db",
])
def test_foreign_ambiguous_claim_blocks_both_backup_and_restore(resource):
    ledger = Ledger()
    ledger.site_records[OTHER["site_id"]]["resources"][0] = resource
    planner = BackupPlanner(ledger)
    with pytest.raises(ValueError, match="resource identifier"):
        planner.plan_backup(SITE["site_id"])
    with pytest.raises(ValueError, match="resource identifier"):
        planner.plan_restore(SITE["site_id"], BACKUP_ID)


def test_manifest_cannot_introduce_untyped_claim_even_with_valid_site():
    ledger = Ledger()
    ledger.backups[BACKUP_ID]["resources"][0] = "relative/backup/path"
    assert BackupPlanner(ledger).plan_backup(SITE["site_id"])["executable"] is False
    with pytest.raises(ValueError, match="resource identifier"):
        BackupPlanner(ledger).plan_restore(SITE["site_id"], BACKUP_ID)


def test_namespaced_opaque_resource_claims_remain_valid_and_read_only():
    ledger = Ledger()
    extra = ["mariadb-database:wm_firstsite001", "php-fpm-pool:wm-firstsite001"]
    ledger.site_records[SITE["site_id"]]["resources"].extend(extra)
    ledger.backups[BACKUP_ID]["resources"].extend(extra)
    planner = BackupPlanner(ledger)
    assert planner.plan_backup(SITE["site_id"])["resources"][-2:] == extra
    assert planner.plan_restore(SITE["site_id"], BACKUP_ID)["executable"] is False
    assert ledger.writes == 0
