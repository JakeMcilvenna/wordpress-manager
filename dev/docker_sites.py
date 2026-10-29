"""Docker adapter for the shared website contract.

The adapter only controls the fixed isolated site2 provisioner, never the
Docker daemon or the existing site at localhost:8091. It is never imported by
the production application.
"""
from dev.site_creator import LocalSiteProvisioner, PUBLIC_URL, SITE_ID, created_site
from wordpress_manager.site_operations import (
    WebsiteOperationUnavailable, WebsitePlan, WebsiteSpec,
)


class DockerWebsiteBackend:
    def __init__(self, provisioner: LocalSiteProvisioner):
        self.provisioner = provisioner

    def inspect(self):
        return self.provisioner.status()

    def list_sites(self):
        site = created_site(self.inspect())
        return [site] if site else []

    def plan(self, spec: WebsiteSpec):
        if spec.php != "8.5":
            raise ValueError("Only PHP 8.5 is provisioned in this first Docker site slot")
        return WebsitePlan(
            spec=spec, site_id=SITE_ID, preview_url=PUBLIC_URL + "/",
            backend="local-docker",
            resources=("second_site_files", "second_site_database",
                       "site2-wordpress", "site2-nginx", "site2-provisioner"),
            confirmation="CREATE LOCAL SITE",
        )

    def create(self, plan: WebsitePlan):
        if not isinstance(plan, WebsitePlan) or plan.backend != "local-docker" or plan.site_id != SITE_ID:
            raise ValueError("Invalid Docker website installation plan")
        return self.provisioner.install(plan.as_session())

    def suspend(self, site_id):
        raise WebsiteOperationUnavailable("Suspending websites is not implemented in Docker yet")

    def delete(self, site_id, confirmation):
        raise WebsiteOperationUnavailable(
            "Website deletion is disabled until owned resources, backups and rollback are implemented"
        )
