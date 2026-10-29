"""Shared website contract must be consistent across Docker and Ubuntu."""
from pathlib import Path

import pytest

from dev.docker_sites import DockerWebsiteBackend
from dev.site_creator import validate_fields
from wordpress_manager.site_operations import (
    WebsiteOperationUnavailable, WebsitePlan, WebsiteService, WebsiteSpec,
)
from wordpress_manager.ubuntu_sites import UbuntuWebsiteBackend


class FakeDockerProvisioner:
    def __init__(self):
        self.installed = False
        self.calls = []

    def status(self):
        return {"installed": self.installed, "ready": True, "title": "Second site",
                "version": "7.1.4", "domain": "second.test"}

    def install(self, plan):
        self.calls.append(plan)
        self.installed = True
        return {"installed": True, "url": plan["url"], "username": "admin",
                "password": "random-one-time-credential"}


def test_platform_independent_validation_canonicalizes_domain():
    spec = WebsiteSpec.from_fields("  Example site ", "admin@demo.test",
                                   "CLIENT-DEMO.TEST.", "8.5")
    assert spec.title == "Example site"
    assert spec.domain == "client-demo.test"
    assert spec.php == "8.5"
    docker = validate_fields("  Example site ", "admin@demo.test",
                             "CLIENT-DEMO.TEST.", "8.5")
    assert docker["domain"] == spec.domain
    assert docker["email"] == spec.email
    assert docker["title"] == spec.title


@pytest.mark.parametrize("domain", [
    "bad..test", "example.com", "127.0.0.1", "example.test:8092",
    "http://domain.test", "a_b.test", "-bad.test", "abc.localhost",
])
def test_all_backends_reject_invalid_domain_in_shared_core(domain):
    with pytest.raises(ValueError):
        WebsiteSpec.from_fields("Example", "admin@example.test", domain, "8.5")


def test_docker_backend_obeys_shared_plan_and_creates_only_once():
    fake = FakeDockerProvisioner()
    service = WebsiteService(DockerWebsiteBackend(fake))
    plan = service.plan("Second site", "admin@second.test", "SECOND.TEST", "8.5")
    assert plan["site_id"] == "local-second-site"
    assert plan["domain"] == "second.test"
    assert plan["backend"] == "local-docker"
    assert plan["url"] == "http://127.0.0.1:8092/"
    assert "second_site_files" in plan["resources"]
    assert "second_site_database" in plan["resources"]
    assert plan["confirmation"] == "CREATE LOCAL SITE"
    assert "password" not in repr(plan).lower()
    with pytest.raises(ValueError, match="CREATE LOCAL SITE"):
        service.create(plan, "YES")
    assert not fake.calls

    # The signed session must not be able to change owned resources.
    tampered = {**plan, "resources": ["wordpress_files"]}
    with pytest.raises(ValueError, match="plan changed"):
        service.create(tampered, "CREATE LOCAL SITE")
    with pytest.raises(ValueError, match="plan changed"):
        service.create({**plan, "backend": "ubuntu"}, "CREATE LOCAL SITE")
    assert not fake.calls

    result = service.create(plan, "CREATE LOCAL SITE")
    assert result["installed"] is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["domain"] == "second.test"
    assert fake.calls[0]["site_id"] == "local-second-site"

    assert len(service.list_sites()) == 1
    with pytest.raises(ValueError, match="already been installed"):
        service.create(plan, "CREATE LOCAL SITE")
    with pytest.raises(ValueError, match="already installed"):
        service.plan("Again", "admin@again.test", "again.test", "8.5")
    assert len(fake.calls) == 1


def test_ubuntu_adapter_remains_disabled_for_every_lifecycle_operation():
    backend = UbuntuWebsiteBackend()
    spec = WebsiteSpec.from_fields("Example", "admin@example.test",
                                   "example.test", "8.5")
    plan = WebsitePlan(spec, "test", "https://example.test", "ubuntu",
                       ("example-site",), "CREATE WEBSITE")
    for call in (
        backend.inspect,
        backend.list_sites,
        lambda: backend.plan(spec),
        lambda: backend.create(plan),
        lambda: backend.suspend("test"),
        lambda: backend.delete("test", "CONFIRM"),
    ):
        with pytest.raises(WebsiteOperationUnavailable, match="disabled"):
            call()
    service = WebsiteService(backend)
    with pytest.raises(WebsiteOperationUnavailable):
        service.plan("Example", "admin@example.test", "example.test", "8.5")
    with pytest.raises(WebsiteOperationUnavailable):
        service.list_sites()


def test_lifecycle_deletion_and_suspension_not_accidentally_implemented():
    service = WebsiteService(DockerWebsiteBackend(FakeDockerProvisioner()))
    for call in (lambda: service.suspend("local-second-site"),
                 lambda: service.delete("local-second-site", "anything")):
        with pytest.raises(WebsiteOperationUnavailable):
            call()


def test_project_policy_and_web_routes_cannot_depend_on_docker_specific_core():
    root = Path(__file__).resolve().parents[1]
    architecture = (root / "docs/WEBSITE_BACKENDS.md").read_text()
    agents = (root / "AGENTS.md").read_text()
    app_source = (root / "wordpress_manager/__init__.py").read_text()
    assert "Docker Desktop" in architecture
    assert "Ubuntu 24.04" in architecture
    assert "production" in agents.lower() and "Docker" in agents
    assert "dev.site_creator" not in app_source
    assert "dev.docker_sites" not in app_source
    assert "dev_create_site" in app_source  # Only permitted behind DEV_PREVIEW.
    assert "app.config.get(\"DEV_PREVIEW\") is True" in app_source
    assert "class WebsiteBackend(Protocol)" in (
        root / "wordpress_manager/site_operations.py").read_text()
    preview_source = (root / "dev/preview.py").read_text()
    assert "WebsiteService(DockerWebsiteBackend(site_creator))" in preview_source
    assert "site_service.plan(" in preview_source
    assert "site_service.create(" in preview_source
