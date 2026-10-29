# WordPress Manager implementation rules

These requirements apply to ALL future site-management changes in this
repository, including work performed by Codex or another coding assistant.

## Two execution environments, one application

WordPress Manager is a **native Ubuntu 24.04 LTS hosting panel**, deployed to
the real Hetzner VPS without requiring Docker. **Docker Desktop** is the local
development/integration harness only. Both environments must support the same
user-visible website lifecycle semantics through the shared interface in
`wordpress_manager/site_operations.py`.

- Keep request validation, domain normalization, site identities, plans,
  confirmation, ownership manifests, status values, job and error states
  in the shared application layer. Do not duplicate or fork business rules
  inside Docker-specific files.
- Platform implementations adapt the same `WebsiteBackend` interface:
  `dev/docker_sites.py` for local containers and
  `wordpress_manager/ubuntu_sites.py` for production. A new feature is NOT
  considered production-ready until *both* paths are implemented and tested.
- The Ubuntu backend MUST NOT depend on Docker, container networking, named
  Docker volumes or test-only passwords. It must use the existing restricted
  provisioner security boundary, with a separately reviewed allowlist for
  website operations and minimal per-site privileges.
- Keep Flask/dashboard unprivileged, loopback-only, with real authentication,
  CSRF and Origin validation. No Docker socket, sudo, shell access, arbitrary
  path/URL/command execution, broad filesystem mounts or database secrets
  in the dashboard.
- Do not promote local mock/single-slot Docker code into production. Explicit
  `WebsiteOperationUnavailable` is safer than pretending an operation works.
- Before implementing site deletion, suspend or restore, design durable
  site-to-resource ownership records, backups, confirmed target/site ID,
  concurrency controls, recovery after partial failure, audit logs and
  rollbacks. Never infer deletion targets directly from user-provided domains
  or paths. Never touch unmanaged websites.
- Changes must preserve both existing Docker test sites on ports 8091 and
  8092 and existing WordPress data volumes unless an isolated disposable test
  explicitly creates and owns the data.

## Completion requirements

1. Add shared-service contract tests and local Docker adapter tests for each
   new website operation; use the real local stack for end-to-end functionality.
2. Add disposable Ubuntu 24.04/systemd tests executing the **actual restricted
   Ubuntu site executor** before claiming production support.
3. Keep feature PRs in `main` with fast checks; only release reviewed features
   into the protected `production` branch, with full security/deployment tests.
4. Do not enable an incomplete or unverified Ubuntu website action in the
   production dashboard. Production availability must fail closed.
5. Update `docs/WEBSITE_BACKENDS.md` when adding operations, backends or
   differences between development and production.

Current status: the shared interface and Docker Create Website adapter exist,
but `UbuntuWebsiteBackend` is deliberately disabled. Docker proof-of-concept
success does **not** mean website creation/deletion on Hetzner is implemented.
