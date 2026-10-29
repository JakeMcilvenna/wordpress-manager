"""Production Ubuntu 24.04 website backend boundary (not yet enabled).

Production already has a separate restricted control service for SERVER STACK
package installation. That service has no approved website verbs. Never
re-purpose its package installer, execute WP-CLI from Flask, or allow this
backend to write Linux resources before adding a separately reviewed and
tested site-executor policy. This adapter is intentionally fail-closed.
"""
from wordpress_manager.site_operations import WebsiteOperationUnavailable


class UbuntuWebsiteBackend:
    """The future Ubuntu implementation must honor the same site contract.

    Future implementation requires a restricted control socket, durable
    resource ownership records, pinned/validated package and site IDs,
    domain-conflict checks, per-site Linux/PHP-FPM identities, atomic Nginx
    activation, per-site MariaDB credentials, rollback and backup semantics.
    """

    def _disabled(self):
        raise WebsiteOperationUnavailable(
            "Ubuntu website operations are disabled until the restricted "
            "production executor passes disposable-Ubuntu integration tests"
        )

    def inspect(self):
        return self._disabled()

    def list_sites(self):
        return self._disabled()

    def plan(self, spec):
        return self._disabled()

    def create(self, plan):
        return self._disabled()

    def suspend(self, site_id):
        return self._disabled()

    def delete(self, site_id, confirmation):
        return self._disabled()
