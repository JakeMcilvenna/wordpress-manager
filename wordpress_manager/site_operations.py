"""Shared website lifecycle contract for Docker development and Ubuntu production.

Keep all user input validation, approval checks and plan integrity here.
Only platform adapters may access containers, WP-CLI, host files, databases
or the restricted provisioner. This package is safe to import in production.
"""
from dataclasses import dataclass
import re
from typing import Protocol


class WebsiteOperationUnavailable(RuntimeError):
    """A platform has not implemented an operation safely yet."""


@dataclass(frozen=True)
class WebsiteSpec:
    title: str
    email: str
    domain: str
    php: str

    @classmethod
    def from_fields(cls, title, email, domain, php):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 70:
            raise ValueError("Provide a website name between 1 and 70 characters")
        title = title.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in title):
            raise ValueError("Website name contains invalid control characters")
        if not isinstance(email, str) or len(email) > 120 or not re.fullmatch(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}", email
        ):
            raise ValueError("Provide a valid administrator email")
        if not isinstance(domain, str):
            raise ValueError("Provide a valid domain name")
        domain = domain.lower().strip().rstrip(".")
        if len(domain) > 253 or len(domain) < 4 or "." not in domain:
            raise ValueError("Provide a complete domain such as client-example.test")
        labels = domain.split(".")
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in labels):
            raise ValueError("Domain labels must contain only letters, numbers and interior hyphens")
        if not re.fullmatch(r"[a-z]{2,24}", labels[-1]):
            raise ValueError("The domain must end in a valid alphabetic suffix")
        if domain in ("localhost", "example.com", "example.org", "example.net") or domain.endswith(".localhost"):
            raise ValueError("Choose a domain reserved for this website")
        if not isinstance(php, str) or not re.fullmatch(r"\d+\.\d+", php):
            raise ValueError("Provide a supported PHP version")
        return cls(title, email, domain, php)


@dataclass(frozen=True)
class WebsitePlan:
    """Reviewed immutable site intent and explicitly owned resources.

    The adapter must derive resource ownership from a server-side site ID,
    not arbitrary client-supplied filesystem paths or database names.
    """
    spec: WebsiteSpec
    site_id: str
    preview_url: str
    backend: str
    resources: tuple[str, ...]
    confirmation: str

    def as_session(self):
        # JSON-safe and no passwords, paths supplied by a browser, or tokens.
        return {
            "title": self.spec.title,
            "email": self.spec.email,
            "domain": self.spec.domain,
            "php": self.spec.php,
            "site_id": self.site_id,
            "url": self.preview_url,
            "backend": self.backend,
            "resources": list(self.resources),
            "confirmation": self.confirmation,
        }


class WebsiteBackend(Protocol):
    """Same site lifecycle interface for both execution environments.

    Read operations never require direct host root. Writes must be executed
    by a narrowly scoped backend, not through a Flask route shell command.
    """
    def inspect(self) -> dict: ...
    def list_sites(self) -> list[dict]: ...
    def plan(self, spec: WebsiteSpec) -> WebsitePlan: ...
    def create(self, plan: WebsitePlan) -> dict: ...
    def suspend(self, site_id: str) -> None: ...
    def delete(self, site_id: str, confirmation: str) -> None: ...


class WebsiteService:
    """Single orchestration path shared across Docker and Ubuntu backends."""

    def __init__(self, backend: WebsiteBackend):
        self.backend = backend

    def inspect(self):
        return self.backend.inspect()

    def list_sites(self):
        return self.backend.list_sites()

    def plan(self, title, email, domain, php):
        spec = WebsiteSpec.from_fields(title, email, domain, php)
        if self.backend.inspect().get("installed") is True:
            raise ValueError("This website slot is already installed.")
        return self.backend.plan(spec).as_session()

    def create(self, stored_plan, confirmation):
        if not isinstance(stored_plan, dict):
            raise ValueError("Review a website installation plan before creating it.")
        spec = WebsiteSpec.from_fields(
            stored_plan.get("title"), stored_plan.get("email"),
            stored_plan.get("domain"), stored_plan.get("php"),
        )
        # Recompute the plan using the selected backend: neither resource
        # ownership nor an approved operation can be changed in the session.
        trusted = self.backend.plan(spec)
        if stored_plan != trusted.as_session():
            raise ValueError("The website plan changed. Review it again.")
        if confirmation != trusted.confirmation:
            raise ValueError("Review the plan and type " + trusted.confirmation + " to approve.")
        if self.backend.inspect().get("installed") is True:
            raise ValueError("This website has already been installed.")
        return self.backend.create(trusted)

    def suspend(self, site_id):
        return self.backend.suspend(site_id)

    def delete(self, site_id, confirmation):
        return self.backend.delete(site_id, confirmation)
