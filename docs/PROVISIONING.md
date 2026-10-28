# Privileged operations: implementation contract

The first release has no root service and no root API. This document is the next increment's acceptance contract. It does not authorize or execute changes to the VPS.

## Separation of privilege

Run the web process as `wm-dashboard` with no sudo or write permission over code, `/etc`, site content, database sockets, backups or the provisioning service. The future provisioner must be installed separately as root-owned code. Do not give Flask a sudo allowlist containing `sh`, `bash`, `python`, `php`, `wp`, `systemctl`, `tee`, `cp` or arbitrary paths.

Use a Unix-domain socket with root-managed group permissions and Linux `SO_PEERCRED` checks for the expected dashboard UID. Limit message size, request duration and rate. Reject all unknown operation names and all unknown fields. No arbitrary commands, shell strings, environment variables, SQL, filenames, unit names or file payloads from the caller.

The service should accept high-level typed requests (`plan_setup`, `apply_setup`, `plan_site`, `create_site`) and derive every pathname, username and command itself. Hold a global lock for package/config operations and per-site locks for site operations. Store jobs and append-only audit events outside web-writable directories. Protect against symlink traversal and time-of-check/time-of-use changes using directory file descriptors and no-follow opens.

## Increment 2: server setup

The pure plan builder in `wordpress_manager/planning.py` is implemented and unit-tested. It has no execution capability. Next, test a narrowly scoped executor in a disposable Ubuntu 24.04 VM. Only after that expose a confirmed dashboard action.

1. Inventory installed packages, configured repositories, PHP-FPM versions, Nginx includes, listeners and MariaDB health. Preserve the user's PHP 8.5.12 deployment; Ubuntu's default PHP package must not replace it.
2. Generate an immutable plan ID and a digest of the baseline. If prerequisites are absent, report the exact package/repository decisions for approval. Do not add a PPA or curl-to-shell installer automatically.
3. Treat detection as distinct from readiness: missing metadata or failed probes cannot prove that installation is needed. Stop on conflicting listeners/configuration.
4. Require explicit approval of the packages/configuration, an operator-verified VPS recovery point and an expiry-bound plan. Reject stale plans when the baseline changes.
5. Only install missing, approved, allowlisted packages. Package scripts can restart services; installation itself is a production change. Do not run a blanket upgrade. Never store database passwords in debconf logs or HTTP responses.
6. WP-CLI must come from an operator-approved pinned artifact with verified integrity/signature. No execution of an unverified download. Keep phpMyAdmin unexposed; bind an explicit management listener to loopback and access it through a separate SSH forward if enabled. Confirm that default package includes do not publish it.
7. Validate candidate configuration with `nginx -t` and the matching FPM validation command before installation/reload. Securely back up previous configuration and record a rollback plan. Do not stop production to discover invalid syntax.
8. Audit outcomes, interruption and partial failure. Retrying a plan must not overwrite existing configurations or silently undo an operator's intervening change.

SSL tooling installation and certificate issuance are separate approvals. Verify domain ownership/DNS and Nginx challenges first. Never expose dashboard/phpMyAdmin as part of public HTTP challenges.

## Increment 3: production site

Accept a validated ASCII/IDNA domain and bounded site identifier. Derive a short Linux username, database name, FPM pool and Nginx file beneath fixed managed roots. Reject IP literals, wildcards, path characters, shell metacharacters, duplicate identities, existing document roots and unmanaged configuration collisions. Store only non-secret public inventory in dashboard-readable metadata.

Sequence: preflight plan → confirmation → lock → private recovery journal → Linux user with locked password/no interactive shell → private site directory → database/user scoped to one database → matching FPM pool → verified WordPress download and checksum validation → site configuration/installation under its Linux UID → candidate Nginx/FPM validation → atomic configuration publication → reload → HTTP/TLS verification → register inventory. Keep incomplete sites out of public routing. Back out only resources proven to have been created by that exact job; never recursively delete an operator-supplied path.

Use cryptographically generated database and admin secrets. Send them over protected local descriptors or private files, not process arguments, audit logs or source control. Never run WordPress plugin/theme/site code as root. The provisioning service must drop UID/GID, clear supplementary groups and use a constrained environment for WP-CLI. Core checksums do not establish that plugins are trustworthy.

Each FPM user must be unable to read sibling site configuration, sockets, databases and backups. FPM process limits must fit the 4 GB VPS (start conservatively, measure before increasing). Shared MariaDB and Nginx still need OS permissions and database grants; FPM isolation alone is not a complete sandbox.

## Later staging and deployment invariants

- Production creation never creates staging. A distinct Create Staging action requires an existing managed production site.
- Enforce staging privacy at Nginx with authentication/access control before publishing a route; also emit `X-Robots-Tag: noindex, nofollow, noarchive`, robots exclusions and WordPress no-index settings. Robots directives alone provide no access protection.
- Block SMTP and outbound production integrations at an enforceable boundary. WordPress mail hooks are defense in depth, not network isolation. Payment safety requires plugin/provider-specific sandbox validation and removal/replacement of live credentials; disabling cron alone is insufficient. If those guarantees cannot be established, fail closed before making staging usable.
- Pull production into staging only after backing up staging and confirming its overwrite. Use serialization-aware URL replacement under the staging UID. Scrub customer data and production secrets according to the site's policy before allowing access.
- File pushes default to excluding uploads, caches, secrets, `wp-config.php` and environment-specific configuration. Use a reviewed manifest, recovery point and atomic switch where possible.
- Database operations are separate from file deployments. Never offer a generic production database overwrite for a live shop, membership site or other site with new customer data. Require an explicit migration strategy (schema migration or vetted, scoped content transfer), maintenance window/concurrency controls where appropriate, and an independent verified recovery point.
- Backups include matching files/database, manifest, integrity hash, destination identity, creation time and retention policy. Encrypt off-server copies. Restore tests must prove recovery on an isolated destination before production restores are enabled.

## Verification gates

Unit tests are not permission to deploy a root daemon. The installer needs disposable-VM tests for first run, already-configured server, version preservation, bad Nginx/FPM syntax, interrupted apt/dpkg, stale plan, failed reload, socket peer rejection, unexpected fields, symlink substitution, secret redaction and concurrent requests.

Site provisioning additionally needs two-client isolation tests, failed download/checksum, partial database creation, repeated requests, duplicate domains, inventory integrity, TLS failure and job-scoped rollback. Staging must demonstrate blocked public access, outbound mail and live payment calls. Backup/deploy work must prove restoration and preservation of customer rows created after the staging snapshot.

No live mutation is approved until the relevant gate is complete and the operator has approved a concrete plan.
