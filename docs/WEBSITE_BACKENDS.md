# Website management architecture: one app, two backends

**Non-negotiable target:** WordPress Manager runs **natively on the Hetzner
Ubuntu 24.04 VPS without Docker**. Docker Desktop is an isolated local
development/test harness. New functionality must implement one shared
application contract and have separately verified environment adapters.

## Architectural boundary

```
Authenticated Flask UI / shared website workflow
    |
    +-- wordpress_manager/site_operations.py
    |      WebsiteSpec: canonical name, email, domain, PHP validation
    |      WebsitePlan: immutable proposed site ID and owned resources
    |      WebsiteService: plan integrity, explicit confirmation, no overwrite
    |      WebsiteBackend: inspect / list / plan / create / suspend / delete
    |
    +-- DockerWebsiteBackend [development only]
    |      dev/docker_sites.py -> dev/site_creator.py
    |      fixed internal site2-provisioner -> dedicated WP-CLI
    |      isolated volumes, MariaDB database, PHP-FPM and Nginx
    |      current local URLs 127.0.0.1:8091 and :8092
    |
    +-- UbuntuWebsiteBackend [currently intentionally DISABLED]
           wordpress_manager/ubuntu_sites.py
           future: restricted VPS provisioner via Unix socket
           future: per-site Linux account, PHP-FPM pool, Nginx vhost,
                   MariaDB database/user, WP-CLI and TLS
```

The common `WebsiteService` and `WebsiteBackend` contract are importable
from the production Python package; the existing Docker client remains an
adapter. The preview's real Create Website form uses `WebsiteService.plan`
and `WebsiteService.create`, so validation, plan verification and the
confirmation requirement are not Docker-specific.

### Already implemented on Docker

- Domain/name/email/PHP validation and approval are shared.
- `DockerWebsiteBackend` uses a dedicated fixed-site container service.
- Site creation is real and retains isolation and data across Docker restart.
- Listing and inspection use the site's WP-CLI-backed status.
- The existing test WordPress website and its database/files are unaffected.
- Suspend/delete methods exist in the interface but explicitly raise
  `WebsiteOperationUnavailable`, because there is no safe implementation.

### Explicitly **not** implemented on Ubuntu production

`UbuntuWebsiteBackend` implements the same interface but all methods
raise `WebsiteOperationUnavailable` deliberately. The live dashboard has
no website-create routes, and the existing restricted system-stack installer
does NOT support arbitrary WordPress site writes. Nothing in this architecture
PR enables site provisioning, suspend or delete on Hetzner.

The planned production adapter must invoke a *new, allowlisted website protocol*
on the existing restricted control service, not Flask subprocesses or sudo.
The future policy should verify resource ownership and site IDs on the
server side, enforce per-site UNIX/PHP-FPM isolation, generate and securely
store MariaDB credentials, check domain conflicts and atomic Nginx
configuration, and journal operations with rollback on failure.

## Shared planning with multiple identities

WebsiteService now checks the selected backend's actual managed-site inventory,
rather than refusing every new plan whenever any site exists. Unique site IDs
and canonical domains must not collide. Inconsistent inventories fail closed,
including missing or non-boolean installation state, duplicate ownership IDs,
duplicate canonical domains, and empty site identities. Both planning and
confirmation recheck this inventory before invoking the platform adapter.

This does not add dynamic Docker slots: the current local provisioner still
owns one fixed site ID and prevents additional installations. The native
disposable adapter can use distinct site IDs. Production remains disabled.

## Shared lifecycle data model

- **Spec** — canonical `title`, `domain`, `email` and `php`.
  Validation occurs before adapter-specific planning. `domain` is an
  *intended* name locally; browser preview uses a fixed localhost port.
- **Plan** — site ID and resource ownership manifest, backend identifier,
  preview URL, and confirmation phrase. This is not an installation command.
  It is regenerated and checked before mutation, so forged or stale
  client-supplied resource names cannot change an operation's scope.
- **Inspect/list** — return only the websites truly controlled by the
  backend; never imply an unreachable database or service is healthy.
- **Create** — only after explicit confirmation and installed/ownership
  checks. Backend performs the platform-specific side effects.
- **Suspend/delete** — contract reserved but fail closed until resource
  ownership, backup, undo, locking and staged operation-recovery tests exist.

Do not change this contract to expose Docker API operations, arbitrary bash,
unvalidated paths/domains or raw system privileges to a web request.
Use per-site opaque IDs and derive ownership on the trusted executor side.

## Local and production differences

| Concern | Local Docker Desktop | Hetzner Ubuntu 24.04 |
|---|---|---|
| App/browser validation | Shared `WebsiteService` | Same shared service |
| Web server | Dedicated Docker Nginx | Host Nginx virtual hosts |
| PHP | Docker PHP-FPM | Isolated PHP-FPM pools/users |
| WordPress | WP-CLI inside site container | WP-CLI under per-site OS identity |
| MariaDB | Separate Docker volume/database | Per-site database/user on host |
| Files | Owned Docker site volume | Owned /srv/… directory (policy-defined) |
| DNS | Intended domain metadata, localhost URL | Validated domain, DNS/TLS workflow |
| Privileges | Internal restricted local broker | Restricted host control service |
| CI | Fast tests + disposable Docker | Full disposable Ubuntu integration tests |
| Implementation | Single-slot Create Website working | Backend **disabled** until validated |

## Phase 1 native execution: disposable Ubuntu only

The repository now includes `deploy/ubuntu_site_executor.py` and an
explicitly guarded `DisposableUbuntuWebsiteBackend`, implementing the same
`WebsiteBackend` protocol as the Docker implementation. It is a separate
test helper, **not** an operation exposed by the deployed Flask application
or existing authenticated root control socket.

The helper will refuse to mutate a host unless all conditions hold:

- Effective UID is root.
- Ubuntu `/etc/os-release` reports 24.04.
- The exact `/run/wm-disposable-test` marker exists as a regular non-symlink
  file with the expected content.
- The environment is running inside a Docker container (`/.dockerenv`).
- The intended domain ends in `.test`, and a hardcoded disposable HTTP
  listener `127.0.0.1:18080` is used.
- An explicit `CREATE NATIVE TEST SITE` confirmation and an unchanged,
  server-recomputed shared `WebsitePlan` are supplied.

When the full disposable Ubuntu stack is available (Nginx, PHP 8.3-FPM,
MariaDB, WP-CLI), the test helper creates **real** native site resources:
a dedicated unprivileged Linux user, dedicated MariaDB user/database, a
dedicated site directory and `wp-config.php`, an isolated PHP-FPM pool/socket,
a loopback-only Nginx virtual host, and a WordPress 7.1.4 installation using
WP-CLI. No browser-provided command, filesystem path, database name or shell
program is used. Generated credentials are not logged or written to the
ownership journal. The WordPress administrator password is returned only to
the disposable integration test.

The root-owned site ledger records the site ID, intended domain, owned
resources and state. Partial failures are marked `needs-review`; the
prototype **does not automatically delete or roll back** partially created
resources. It refuses to adopt or overwrite existing paths/users/databases.

The real integration test `tests/native_site_integration.py` runs after
stack installation inside the existing disposable `tests/stack_integration.py`
guest (the **full production CI gate**, not fast UI CI). It verifies the
actual WordPress HTTP site, database, PHP-FPM pool, ownership and refusal of
duplicate or forged plans. This should not be taken as proof of production
support until that full integration job has actually passed.

**Production remains disabled:** `wordpress_manager/ubuntu_sites.py` still
raises `WebsiteOperationUnavailable` for every website operation. Before
enabling it, implement and review a separate allowlisted control socket
operation (using the existing session/plan/authorization boundary), durable
site ownership and duplicate-domain checks, backup and rollback, site
credentials handling, TLS and full VPS audit. Never add a shortcut that
invokes the disposable test helper from Flask or weakens its guest-only guard.

## Acceptance gate for the next multi-site milestone

1. The common model and validation cover duplicates, site identity, owned
   resources, job states and recovery across both environments.
2. Docker integration creates at least two independent test sites, proves
   they survive restarts and shows that one failure cannot change another.
3. Ubuntu's executor in a **disposable Ubuntu 24.04/systemd** test image
   creates Nginx config, Linux/PHP-FPM identity, MariaDB database/user,
   WordPress files and WP-CLI install through the same interface.
4. Disconnection, duplicate domain, partial install, timeout, failed
   Nginx config, disk-full and rollback cases are exercised. Never use
   production Hetzner as a test target.
5. Site deletion is NOT enabled until backups/recovery and per-resource
   ownership are proven. Deletion requires typed domain/site confirmation,
   access checks, immutable audit records and recovery proof.
6. Only after the complete Ubuntu pathway is verified can a separate
   production PR expose website operations on the real VPS.

See `AGENTS.md` for rules every future development session must follow.
