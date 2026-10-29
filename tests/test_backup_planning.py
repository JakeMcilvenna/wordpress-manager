"""Backup/restore preflight must never select another site's resources."""
from copy import deepcopy

import pytest

from wordpress_manager.backup_planning import BackupManifest, BackupPlanner
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
        self.sites = {item["site_id"]: deepcopy(item) for item in (SITE, OTHER)}
        self.backups = {BACKUP_ID: deepcopy(MANIFEST)}
        self.writes = 0

    def site(self, site_id):
        return deepcopy(self.sites.get(site_id))

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
    ledger.sites[SITE["site_id"]].update(changed)
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
