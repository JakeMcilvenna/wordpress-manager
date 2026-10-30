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


@pytest.mark.parametrize("resource", [
    "/", "/srv/wordpress-manager-sites/", "/srv//wordpress-manager-sites/site",
    "/srv/wordpress-manager-sites/./site",
    "/srv/wordpress-manager-sites/site/../other",
    "/srv/wordpress-manager-sites/site\\other",
])
def test_noncanonical_resource_paths_fail_even_with_matching_records(resource):
    ledger = Ledger()
    ledger.selected["resources"][0] = resource
    ledger.records[0] = deepcopy(ledger.selected)
    ledger.recovery["resources"] = deepcopy(ledger.selected["resources"])
    with pytest.raises(ValueError, match="resource path"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


@pytest.mark.parametrize("foreign_path", [
    SITE["resources"][0] + "/uploads",
    "/srv/wordpress-manager-sites",
])
@pytest.mark.parametrize("reverse_inventory", [False, True])
def test_parent_child_paths_cannot_be_owned_by_different_sites(
    foreign_path, reverse_inventory
):
    ledger = Ledger()
    ledger.records[1]["resources"][0] = foreign_path
    if reverse_inventory:
        ledger.records.reverse()
    with pytest.raises(ValueError, match="Nested path"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


@pytest.mark.parametrize("state", [
    "provisioning", "needs-review", "failed", "suspended", None,
])
def test_foreign_unhealthy_site_blocks_deletion_planning(state):
    ledger = Ledger()
    ledger.records[1]["state"] = state
    with pytest.raises(ValueError, match="Unhealthy or incomplete"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


def test_sibling_prefix_is_not_a_nested_path_claim():
    ledger = Ledger()
    ledger.records[1]["resources"][0] = SITE["resources"][0] + "-sibling"
    assert DeletionPlanner(ledger).plan_delete(SITE["site_id"])["executable"] is False


def test_same_site_nested_path_claims_are_not_cross_site_conflicts():
    ledger = Ledger()
    nested = SITE["resources"][0] + "/uploads"
    ledger.selected["resources"].append(nested)
    ledger.records[0] = deepcopy(ledger.selected)
    ledger.recovery["resources"] = deepcopy(ledger.selected["resources"])
    assert DeletionPlanner(ledger).plan_delete(SITE["site_id"])["executable"] is False


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


def test_review_fingerprint_binds_foreign_ownership_and_finished_jobs():
    ledger = Ledger()
    planner = DeletionPlanner(ledger)
    baseline = planner.plan_delete(SITE["site_id"])["snapshot_digest"]

    ledger.records[1]["resources"].append("/srv/foreign-extra")
    assert planner.plan_delete(SITE["site_id"])["snapshot_digest"] != baseline

    ledger.records[1]["resources"].pop()
    ledger.operations.append({"state": "cancelled", "job_id": "old-job"})
    assert planner.plan_delete(SITE["site_id"])["snapshot_digest"] != baseline


def test_review_fingerprint_is_stable_across_ledger_enumeration_order():
    ledger = Ledger()
    ledger.operations = [
        {"state": "succeeded", "job_id": "job-a"},
        {"state": "cancelled", "job_id": "job-b"},
    ]
    planner = DeletionPlanner(ledger)
    original = planner.plan_delete(SITE["site_id"])["snapshot_digest"]
    ledger.records.reverse()
    ledger.operations.reverse()
    assert planner.plan_delete(SITE["site_id"])["snapshot_digest"] == original


def test_nonfinite_or_unserializable_completed_job_fails_closed():
    ledger = Ledger()
    ledger.operations = [{"state": "succeeded", "metadata": float("nan")}]
    with pytest.raises(ValueError, match="journal"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])
    ledger.operations = [{"state": "succeeded", "metadata": object()}]
    with pytest.raises(ValueError, match="journal"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


@pytest.mark.parametrize("resource", [
    "srv/wordpress-manager-sites/wm-aaaaaaaaaaaa",
    "../srv/wordpress-manager-sites/wm-aaaaaaaaaaaa",
    "linux-user:wmsaaaaaaaaaa/../root",
    "mariadb-user:..",
    "linux-user:",
    "untyped-identity",
    "linux-user:valid\\backslash",
    "linux-user:valid:extra",
    "linux-user:valid name",
])
@pytest.mark.parametrize("foreign", [False, True])
def test_relative_and_malformed_opaque_resource_claims_fail_closed(resource, foreign):
    ledger = Ledger()
    if foreign:
        ledger.records[1]["resources"][0] = resource
    else:
        ledger.selected["resources"][0] = resource
        ledger.records[0] = deepcopy(ledger.selected)
        ledger.recovery["resources"] = deepcopy(ledger.selected["resources"])
    with pytest.raises(ValueError, match="resource identifier"):
        DeletionPlanner(ledger).plan_delete(SITE["site_id"])


def test_canonical_typed_resource_claims_remain_accepted():
    ledger = Ledger()
    ledger.selected["resources"].append("php-fpm-pool:wm_aaaaaaaaaaaa.conf")
    ledger.records[0] = deepcopy(ledger.selected)
    ledger.recovery["resources"] = deepcopy(ledger.selected["resources"])
    plan = DeletionPlanner(ledger).plan_delete(SITE["site_id"])
    assert plan["executable"] is False
    assert "php-fpm-pool:wm_aaaaaaaaaaaa.conf" in plan["resources"]
