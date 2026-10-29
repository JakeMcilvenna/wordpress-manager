"""Deletion review must not select, mutate or adopt another site's assets."""
from copy import deepcopy

import pytest

from wordpress_manager.deletion_planning import DeletionPlanner
from wordpress_manager.site_operations import WebsiteOperationUnavailable


SITE = {
    "site_id": "wm-aaaaaaaaaaaa", "domain": "one.test",
    "backend": "ubuntu-disposable", "state": "healthy",
    "resources": ["/srv/wordpress-manager-sites/wm-aaaaaaaaaaaa",
                  "linux-user:wmsaaaaaaaaaa", "mariadb-database:wm_aaaaaaaaaa"],
}
OTHER = {
    "site_id": "wm-bbbbbbbbbbbb", "domain": "two.test",
    "backend": "ubuntu-disposable", "state": "healthy",
    "resources": ["/srv/wordpress-manager-sites/wm-bbbbbbbbbbbb",
                  "linux-user:wmsbbbbbbbbbb", "mariadb-database:wm_bbbbbbbbbb"],
}
RECOVERY = {
    "backup_id": "bkp-" + "c" * 32, "site_id": SITE["site_id"],
    "domain": SITE["domain"], "backend": SITE["backend"],
    "resources": SITE["resources"], "archive_sha256": "d" * 64,
    "archive_bytes": 4096, "verified_at": "2026-01-01T00:00:00Z",
    "state": "verified",
}


class Ledger:
    def __init__(self):
        self.selected = deepcopy(SITE)
        self.records = [deepcopy(SITE), deepcopy(OTHER)]
        self.recovery = deepcopy(RECOVERY)
        self.operations = [{"state": "succeeded"}]
        self.writes = 0

    def site(self, site_id):
        return deepcopy(self.selected) if site_id == SITE["site_id"] else None

    def sites(self):
        return deepcopy(self.records)

    def recovery_point(self, site_id):
        return deepcopy(self.recovery)

    def jobs(self):
        return deepcopy(self.operations)


def test_deletion_is_review_only_and_requires_exact_site_and_domain():
    ledger = Ledger()
    planner = DeletionPlanner(ledger)
    plan = planner.plan_delete(SITE["site_id"])
    assert plan["operation"] == "delete"
    assert plan["site_id"] == SITE["site_id"]
    assert plan["domain"] == SITE["domain"]
    assert plan["resources"] == SITE["resources"]
    assert plan["recovery"]["backup_id"] == RECOVERY["backup_id"]
    assert plan["confirmation"] == "DELETE wm-aaaaaaaaaaaa one.test"
    assert len(plan["snapshot_digest"]) == 64
    assert plan["executable"] is False
    assert ledger.writes == 0
    with pytest.raises(WebsiteOperationUnavailable, match="disabled"):
        planner.delete(plan, plan["confirmation"])
    with pytest.raises(WebsiteOperationUnavailable, match="disabled"):
        planner.delete({**plan, "resources": OTHER["resources"]}, "YES")
    assert ledger.writes == 0


@pytest.mark.parametrize("site_id", [
    "", None, "../etc/passwd", "WM-AAAAAAAAAAAA", "wm-aaaaaaaaaaaa/../",
    "wm-aaaaaaaaaaaa\n", "a" * 90,
])
def test_user_supplied_identity_cannot_select_paths(site_id):
    with pytest.raises(ValueError, match="site ID"):
        DeletionPlanner(Ledger()).plan_delete(site_id)


@pytest.mark.parametrize("field,value", [
    ("site_id", OTHER["site_id"]),
    ("domain", "TWO.TEST"),
    ("backend", "ubuntu/unsafe"),
    ("state", "provisioning"),
    ("resources", []),
    ("resources", ["/srv/a", "/srv/a"]),
    ("resources", ["/srv/owned\x00unsafe"]),
])
def test_selected_site_must_have_unambiguous_healthy_ownership(field, value):
    ledger = Ledger()
    ledger.selected[field] = value
    with pytest.raises(ValueError):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


def test_all_site_inventory_is_required_and_must_match_selected_record():
    ledger = Ledger()
    ledger.records = [deepcopy(OTHER)]
    with pytest.raises(ValueError, match="missing"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])
    ledger.records = [deepcopy(SITE), deepcopy(OTHER)]
    ledger.records[0]["resources"].append("/srv/untracked")
    with pytest.raises(ValueError, match="changed"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


@pytest.mark.parametrize("mutation", [
    lambda ledger: ledger.records.append(deepcopy(SITE)),
    lambda ledger: ledger.records[1].update(domain=SITE["domain"]),
    lambda ledger: ledger.records[1]["resources"].append(SITE["resources"][0]),
    lambda ledger: ledger.records[1].update(resources=["/srv/unsafe\n"]),
])
def test_foreign_or_ambiguous_resource_claims_block_deletion(mutation):
    ledger = Ledger()
    mutation(ledger)
    with pytest.raises(ValueError):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


@pytest.mark.parametrize("state", [
    "running", "queued", "interrupted", "failed", "needs-review", None,
])
def test_any_unfinished_or_unknown_job_blocks_deletion(state):
    ledger = Ledger()
    ledger.operations = [{"state": state}]
    with pytest.raises(ValueError, match="operation"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


@pytest.mark.parametrize("changed", [
    {"site_id": OTHER["site_id"]},
    {"domain": OTHER["domain"]},
    {"backend": "local-docker"},
    {"resources": OTHER["resources"]},
    {"resources": list(reversed(SITE["resources"]))},
    {"state": "pending"},
    {"archive_sha256": "X" * 64},
    {"archive_bytes": 0},
    {"archive_bytes": True},
    {"archive_bytes": 1024 ** 4 + 1},
    {"verified_at": "2099-01-01T00:00:00Z"},
    {"verified_at": "2026-02-30T00:00:00Z"},
    {"verified_at": "2026-01-01T00:00:00+00:00"},
])
def test_missing_invalid_or_cross_site_recovery_blocks_deletion(changed):
    ledger = Ledger()
    ledger.recovery.update(changed)
    with pytest.raises(ValueError):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


def test_unknown_recovery_fields_and_missing_ledger_fail_closed():
    ledger = Ledger()
    ledger.recovery["archive_path"] = "/etc/shadow"
    with pytest.raises(ValueError, match="recovery"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])
    ledger.recovery = None
    with pytest.raises(ValueError, match="recovery"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])
    ledger = Ledger()
    ledger.operations = None
    with pytest.raises(ValueError, match="journal"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


def test_recovery_metadata_changes_plan_fingerprint_without_authorizing_delete():
    ledger = Ledger()
    original = DeletionPlanner(ledger).plan_delete(SITE["site_id"])
    ledger.recovery["archive_sha256"] = "e" * 64
    changed = DeletionPlanner(ledger).plan_delete(SITE["site_id"])
    assert original["snapshot_digest"] != changed["snapshot_digest"]
    assert changed["executable"] is False
