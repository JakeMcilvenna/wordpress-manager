# Provisioning architecture

The panel and the optional stack workflow are implemented locally. Nothing in this document authorizes touching the live Hetzner VPS.

## Trust and process boundaries

`wordpress-manager.service` runs as wm-dashboard with no sudo and root-owned code. It serves only IPv4 loopback and requires deployed authentication settings. `wordpress-manager-provisioner.service` runs the standalone standard-library root program in `deploy/provision.py`; it never imports the Flask package. It resets its primary group to root before creating shared tools/packages and explicitly assigns only the control socket to the dashboard group. It disables bytecode writes so root imports cannot modify inventoried releases. `/run/wordpress-manager/control.sock` is root:wm-dashboard 0660, its directory 0750, and each accepted connection must have the exact wm-dashboard UID from SO_PEERCRED. Other local accounts cannot connect.

The protocol contains only login, logout, status, plan and apply. Unknown verbs/fields, oversized messages and invalid PHP enums are refused. No path, executable, shell, SQL, environment, repository or service-name input reaches subprocesses. All execution uses fixed argument arrays, a clean environment and timeouts. The root CLI is not callable by the dashboard and requires explicit non-interactive server-change approval to apply a saved plan.

The control state directory is root-only. Salted password hashes, session capability hashes, ten-minute session-bound plans, one-use approval state, jobs and structured audit events are protected there. Login throttling survives service restarts. Session capabilities expire after thirty minutes. The Flask session key is root-owned and group-readable; it never grants arbitrary privileged operations. Every web POST requires CSRF validation and same-origin policy; the job also requires exact confirmation text.

## Plan and execution contract

Inventory records installed package versions, hashes/symlinks of existing Nginx/PHP/MariaDB/phpMyAdmin configuration, and known custom installation paths. Unsupported/custom/ambiguous states fail closed. The planner preserves all installed packages and resolves missing dependencies through Ubuntu-origin repositories. Any simulated APT removal or modification to an existing package is refused. The approval displays every new transitive dependency and exact version, external release pins, selected PHP and preserved PHP versions. Existing 8.4/8.5 are preserve-only choices; new installs use Ubuntu 8.3.

Application consumes one stored plan in a SQLite transaction, records approval, creates a durable job and takes the deployment lock. It rechecks inventory and exact APT resolution, verifies external artifacts before mutations, installs exact new packages with --no-remove/--force-confold and a temporary service-start guard, and verifies all pre-existing versions/configuration hashes afterwards. Existing unmanaged policy-rc.d is refused. Only newly installed shared services are enabled/started. Existing customer routing, database content and pools are never generated, adopted or edited.

phpMyAdmin is extracted only from its pinned verified distribution into a new dedicated root-owned directory. New files/units are exclusive creations. Its independent FPM pool and Nginx instance run as www-data with read-only system protection and write only to dedicated runtime/state paths. The Nginx instance binds to 127.0.0.1:8081 and uses no public site routing. Cookie database authentication has no embedded database secret; the random cookie encryption secret lives in a root:www-data 0640 config. HTTP health and localhost binding must succeed before a ready marker is written. Existing phpMyAdmin is left untouched.

Provisioning does not create WordPress sites, users/databases, certificates, staging or backups. WP-CLI is installed as a verified executable but is never run as root against a site.

## Failure, recovery and removal

Deployment uses a separate durable transaction and atomic current-release link. Validation precedes activation; failed health restores the old release. The panel installer records owned control resources before creation and removes only those resources during failure cleanup/uninstall. Source/service hashes and account identity are checked before recovery. Configuration and the root ledger persist across normal upgrades/rollbacks.

Provisioning failures retain a durable failed job and safely installed resources for inspection. There is no pretend transactional rollback of APT/database state. On daemon restart, running jobs become interrupted and require a fresh plan after operator review. A power loss can leave the start guard or an incomplete private admin tree; neither is silently overwritten. Never delete unmanaged files to make a retry pass. Panel removal retains credentials, configuration, deployment state and the audit ledger by default. Only explicit `--purge-recovery` approval removes those payloads; deployment/journal logs remain. Shared packages, websites, databases and the independent private admin stack always remain. See README for dry run, interactive/automated removal, ownership manifest checks, retained-account handling and recovery.

## Verification

Ordinary unit tests never execute system mutations. Linux filesystem simulations cover deployments and rollbacks; security tests cover login/CSRF/protocol rejection, stale plans, version preservation, one-use session confirmation, throttling and redaction. Guarded root integration scripts run only in disposable Ubuntu 24.04 systemd guests. They exercise real APT, accounts, Gunicorn, control socket permissions, authenticated dashboard installation, private phpMyAdmin health/listener, PHP configuration preservation and safe uninstall. The live VPS is excluded. Cloud-init schema/embedded Python are checked; an actual provider first-boot smoke test remains separate.

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
