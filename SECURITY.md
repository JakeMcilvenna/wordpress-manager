# Security policy

WordPress Manager is private management software for a trusted operator. Support is limited to the tested Ubuntu 24.04 amd64 deployment. Never expose ports 8080 or 8081 publicly. Report vulnerabilities privately to the repository owner; do not publish credentials, site data or live configurations.

The dashboard account has no sudo, shell login, privileged group memberships or ownership of executable code. Gunicorn runs with systemd filesystem/device/kernel/capability restrictions and binds exclusively to 127.0.0.1:8080. Production requires root-managed authentication settings; missing or unsafe settings fail startup. Local development is read-only by default.

The separate root control service exposes only login/logout/status/plan/apply over a group-restricted Unix socket with SO_PEERCRED UID checks. It rejects unknown verbs/fields and never accepts commands, executable paths, environment variables, SQL, arbitrary files or service names. Compromise of the dashboard could still request its deliberately limited stack operation; this is not a sandbox against root package maintainer scripts or a compromised trusted artifact/repository.

Authentication uses a server-generated password, salted PBKDF2-SHA256 with 600,000 iterations, persistent login throttling, expiring random capabilities stored only as hashes in the ledger, signed HttpOnly SameSite=Strict cookies, CSRF tokens, origin checks and explicit session-bound one-use job confirmation. Root-only CLI approval is a separate administrative path. Passwords and capability tokens are omitted from audit events. The dashboard's signed session cookie is not encrypted; it contains a short-lived capability, never a password. HTTP is allowed only over the loopback SSH-tunnel workflow.

The root-owned synchronous SQLite ledger durably records login attempts, plans, approvals and job outcomes before/after mutations. A global file lock excludes deployment and package jobs. Crashed jobs are marked interrupted, not replayed. Root can alter the ledger; it is not a tamper-proof external audit system. Export/rotate records under your retention policy. Uninstall retains the ledger, credentials and configuration by default; export them before explicitly approving `--purge-recovery`. Deployment and system journal logs remain.

Stack plans preserve existing packages/configuration and prohibit removals/upgrades. All new transitive dependencies are pinned in the approved plan and verified by authenticated Ubuntu APT metadata. Only new Ubuntu PHP 8.3 is installed; preinstalled 8.4/8.5 with complete dependencies can be selected without replacement. Custom installations, partial configurations, third-party package sources and an existing policy-rc.d require manual review. Discovery cannot prove the absence of every possible custom installation.

Managed phpMyAdmin uses a separate unprivileged www-data Nginx instance and PHP pool at 127.0.0.1:8081; it creates no database accounts, permits no root/blank-password authentication, and adds no public Nginx route. Existing phpMyAdmin is preserved and its exposure remains unverified. The shared www-data identity is not tenant isolation; per-site creation/isolation remains unimplemented.

Deployment trusts an independently pinned archive SHA256 or an explicitly reviewed local checkout. Checksums verify integrity relative to that trust anchor, not publisher identity by themselves. Artifacts and package code execute with administrative privileges; review pins and publishers. Never execute an unverified downloaded script. Private repository credentials are never needed in cloud-init when the reviewed release is separately published.

The optional [production CI/CD boundary](docs/PRODUCTION_DEPLOYMENT.md) is disabled by
default. Its dedicated SSH identity has only a forced stdin artifact operation and
a no-argument sudo allowlist. It has no root key, general shell, file-transfer/read
API, forwarding or client-data group membership. The root receiver verifies the
archive/manifest/commit in private staging and pins every deployment file against
administrator-owned policy, including the root provisioner, wheel hashes and units.
It invokes server-owned upgrade tooling; uploaded deployment scripts and packaging
hooks are never executed as root. Privileged-policy changes require separate
administrator review. GitHub branch/environment protection and the dedicated key
are the delivery trust anchor; the server does not independently attest CI results.

Panel rollback does not reverse OS package operations. Partial provisioning retains resources for inspection instead of deleting websites/databases. Keep independent snapshots/backups. Package/service state is not evidence of site integrity, valid TLS or recoverable backups. Actual cloud-provider first boot and third-party PHP runtime combinations require further disposable environment validation.
