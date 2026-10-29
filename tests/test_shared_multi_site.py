"""Shared multi-site availability: never confuse 'any installed' with 'full'."""
from hashlib import sha256

import pytest

from wordpress_manager.site_operations import WebsitePlan, WebsiteService


class MemoryMultiSiteBackend:
    """Deterministic contract fixture; no host or Docker operations."""
    def __init__(self):
        self.sites = []
        self.create_calls = 0

    def inspect(self):
        return {"installed": bool(self.sites), "sites": len(self.sites)}

    def list_sites(self):
        return [dict(site) for site in self.sites]

    def plan(self, spec):
        site_id = "wm-" + sha256(spec.domain.encode("ascii")).hexdigest()[:12]
        return WebsitePlan(
            spec=spec, site_id=site_id, backend="native-contract-fixture",
            preview_url="https://" + spec.domain + "/",
            resources=("files:" + site_id, "db:" + site_id),
            confirmation="CREATE TEST SITE",
        )

    def create(self, plan):
        # Actual platform backends must atomically recheck inventory/ownership.
        assert plan == self.plan(plan.spec)
        self.create_calls += 1
        site = {"id": plan.site_id, "domain": plan.spec.domain,
                "name": plan.spec.title, "state": "healthy"}
        self.sites.append(site)
        return {"installed": True, **site}

    def suspend(self, site_id):
        raise NotImplementedError

    def delete(self, site_id, confirmation):
        raise NotImplementedError


def create_site(service, domain):
    plan = service.plan("Site " + domain, "admin@" + domain, domain, "8.3")
    result = service.create(plan, "CREATE TEST SITE")
    return plan, result


def test_two_independent_sites_created_through_same_shared_service():
    backend = MemoryMultiSiteBackend()
    service = WebsiteService(backend)
    first, first_result = create_site(service, "first.test")
    second, second_result = create_site(service, "second.test")
    assert first["site_id"] != second["site_id"]
    assert set(first["resources"]).isdisjoint(second["resources"])
    assert backend.create_calls == 2
    assert [site["domain"] for site in service.list_sites()] == ["first.test", "second.test"]
    assert service.inspect()["sites"] == 2

    # Existing sites remain intact and still independently addressable.
    with pytest.raises(ValueError, match="already installed"):
        create_site(service, "FIRST.TEST")
    with pytest.raises(ValueError, match="already been installed"):
        service.create(first, "CREATE TEST SITE")
    assert all(service.list_sites()[0][key] == first_result[key]
               for key in ("id", "domain", "name", "state"))
    assert all(service.list_sites()[1][key] == second_result[key]
               for key in ("id", "domain", "name", "state"))
    assert backend.create_calls == 2


def test_duplicate_domain_rejected_even_if_backend_assigns_different_site_id():
    backend = MemoryMultiSiteBackend()
    service = WebsiteService(backend)
    create_site(service, "first.test")
    original_plan = backend.plan
    def bad_plan(spec):
        plan = original_plan(spec)
        return WebsitePlan(plan.spec, "wm-deadbeefdead", plan.preview_url,
                           plan.backend, ("other-files",), plan.confirmation)
    backend.plan = bad_plan
    with pytest.raises(ValueError, match="already installed"):
        service.plan("Conflict", "admin@first.test", "FIRST.TEST.", "8.3")
    assert backend.create_calls == 1


def test_unreliable_inventory_fails_closed():
    backend = MemoryMultiSiteBackend()
    service = WebsiteService(backend)
    backend.inspect = lambda: {"installed": True}
    with pytest.raises(ValueError, match="inconsistent"):
        service.plan("Unsafe", "admin@third.test", "third.test", "8.3")
    backend.inspect = lambda: {"installed": False}
    backend.sites.append({"id": "site-a", "domain": "first.test"})
    with pytest.raises(ValueError, match="inconsistent"):
        service.plan("Unsafe", "admin@third.test", "third.test", "8.3")


def test_forged_manifest_cannot_create_or_change_another_site():
    backend = MemoryMultiSiteBackend()
    service = WebsiteService(backend)
    first, _ = create_site(service, "first.test")
    second = service.plan("Second", "admin@second.test", "second.test", "8.3")
    with pytest.raises(ValueError, match="plan changed"):
        service.create({**second, "resources": first["resources"]}, "CREATE TEST SITE")
    with pytest.raises(ValueError, match="plan changed"):
        service.create({**second, "site_id": first["site_id"]}, "CREATE TEST SITE")
    assert backend.create_calls == 1
    assert service.list_sites()[0]["domain"] == "first.test"
