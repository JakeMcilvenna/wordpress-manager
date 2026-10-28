# Local validation — 28 October 2026

All checks were run in the local workspace or disposable Docker guests. No SSH connection to the live Hetzner VPS was made and nothing was published externally.

## Production CI/CD validation

- Windows / Python 3.11: **132 passed, 78 skipped**. Linux-only checks account for
  the skips; the real Chromium login form regression passed.
- Ubuntu 24.04 amd64 / Python 3.12 as an unprivileged test account: **208 passed,
  2 skipped**, including Chromium. The separate root deployment/protocol suites
  passed **82 tests**, including the two root-only ownership checks.
- The complete real systemd deployment lifecycle passed: fresh/repeat install,
  invalid source, SIGTERM interruption, runtime-health failure with rollback,
  approved upgrade, explicit rollback/recovery, uninstall/reinstall and verified
  published-format archive installation/upgrade/rollback.
- A fresh disposable Ubuntu SSH/sudo suite passed with the actual production
  sender. It denied arbitrary commands, SFTP/SCP, forwarding, wrong host keys,
  extra sudo arguments, private credential/site-file reads, a disabled server gate,
  malformed/traversal archives, corrupt manifests and mismatched commit identities.
  Manifest-consistent replacements for the root manager, shell upgrader, provisioner,
  verifier, dependency lock and service policy were refused. Executable attack
  payloads never created their root marker. A healthy release produced a receipt
  with commit, release ID, digest, outcome and timestamp. Both locks blocked
  competing delivery; failed runtime health restored the prior release. Manual
  rollback passed; credentials, web configuration and site/shared-component
  snapshots were unchanged. Test keys existed only inside disposable guests.
- The real stack/site suite passed authenticated plan/apply, private phpMyAdmin,
  unchanged PHP configuration and repeat provisioning. Verified CI activation and
  administrator rollback while real WordPress fixtures served traffic preserved
  the panel credentials/configuration, production/staging WordPress, MariaDB
  users/data, site account/FPM pool, TLS, backups/schedules, shared service
  PIDs/package versions and phpMyAdmin. Removal/purge preserved that same baseline.
- Git-object release tests proved reproducibility despite dirty/untracked checkout
  files. Digest, full inventory, per-file hashes, canonical paths, symlink/hardlink
  rejection and compressed expansion limits passed.
- Both workflows passed Actionlint; Python compilation, shell parsing, Git
  whitespace checks and Ubuntu `cloud-init schema` passed.

Initial test-environment failures were corrected before these final results:
Chromium needed downloading; a minimal guest needed SSH started before `sshd -t`
and a `/var/www` fixture directory; the full app suite correctly refuses root and
was rerun as an unprivileged user with Git installed. Disposable root filesystem
fixtures were cleaned before the independent stack test. None of these runs
connected to the VPS or used real deployment credentials.

GitHub Actions itself has not run this uncommitted change, and the Hetzner SSH
configuration/network has not been tested. Deployment remains disabled; follow
[the operator setup and rollback procedure](PRODUCTION_DEPLOYMENT.md) to configure
the protected production environment, trusted host key and dedicated identity.

## Earlier baseline validation (retained history)

- Windows / Python 3.11: **90 passed**, 77 POSIX-specific tests skipped.
- Ubuntu 24.04 amd64 / Python 3.12, unprivileged test account: **165 passed**, two root-only tests skipped (lock ownership and external-account file ownership). The root deployment filesystem suite separately passed **69 tests**, including both root-only checks.
- Real systemd deployment suite: fresh install, exact-source repeat install, invalid-source failure cleanup, real SIGTERM interruption, upgrade prevalidation, failed runtime health with automatic rollback, approved upgrade, explicit rollback, interrupted activation recovery, immediate reinstall and safe uninstall passed.
- Verified published-format archive: build, integrity-checked extraction, install without the checkout override, repeat install, upgrade and rollback passed. Authentication settings and credentials survived upgrades/rollback.
- Real APT/dashboard stack suite: authenticated plan/confirmation/job workflow, package installation, restricted socket access, arbitrary-command rejection, private phpMyAdmin HTTP health/listener, preserved PHP configuration, repeat no-op provisioning, interrupted-job status after daemon restart, and panel removal preserving the stack passed.
- Real production/staging WordPress 6.8.3 fixtures: HTTP and certificate-validated local HTTPS, MariaDB tables and per-site users/grants, a dedicated Linux site account/PHP-FPM pool, Nginx site configuration, certificates/renewal configuration, SQL/file backups and a cron backup schedule all survived dry run, default removal, explicit recovery purge and repeated purge. Shared Nginx/MariaDB/PHP-FPM/phpMyAdmin service PIDs and installed package versions were unchanged. Panel units/account/socket and TCP port 8080 were absent afterwards.
- Removal safety: resource manifests, no-write previews, explicit interactive confirmation, default recovery-data retention, later purge, retained-account manifests, partial/missing files, interrupted purge/resume, original deployment-journal retention, hard links, replaced/symlinked resources, mounts, unknown files/configuration, custom drop-ins, stop failures and panel-dependent site configurations are covered by filesystem tests. Shared tool ownership was verified root:root; the provisioner explicitly assigns the dashboard group only to its socket and avoids writes to immutable releases.
- Cloud-init schema validated using Ubuntu's `cloud-init schema`; its embedded Python parses and safe defaults are tested.
- Login and plan UI inspected at desktop and a 390px emulated mobile viewport; mobile document width stays at 390px.
- Python compilation, shell parsing, release archive reproducibility/integrity/path traversal tests, and Git whitespace checks passed.

## Limits before production

Only Ubuntu 24.04 amd64 is supported by the installer. The integration guests used real systemd/APT/accounts on Docker Desktop's Linux engine; they are not a Hetzner hardware or network certification.

Unmanifested legacy deployments and operator-modified resources require manual ownership review. A reused account or external files owned by its user/group cause account retention, with minimal ownership metadata kept even after a recovery purge. Custom website integrations outside the detected configuration/dependency paths require operator review. Default removal retains secrets/audit/configuration; explicit purge requires independent recovery backups. No production website deletion workflow is implemented or invoked by uninstall.

The cloud-init template still needs a first-boot smoke test on a **new disposable Hetzner server**, including real SSH access with the supplied key and security update behavior. Its URL/key/checksum placeholders must be completed. No public release location has been created; publish the reviewed archive bytes intentionally and distribute the SHA256 through a trusted channel before using remote bootstrap.

PHP 8.3 is the newly installed runtime exercised here. Existing PHP 8.4/8.5 inventories/version preservation are covered by simulation and a real separate-version configuration sentinel. Their third-party binary/repository combinations are not certified; automatic package resolution deliberately refuses third-party repositories or upgrades to existing packages.

Interrupted APT operations require operator review/repair or snapshot recovery; there is no automatic rollback of package/database state. Managed sites, TLS issuance, backups and staging are outside this increment. See the README and security architecture for failure recovery and retained resources.
