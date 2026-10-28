# WordPress Manager

A private Flask operations console for WordPress servers, with automated panel deployment and an optional restricted server-stack installer. The dashboard runs as a locked, unprivileged account and listens only on **127.0.0.1:8080**. Access it through SSH. No live VPS is contacted by this repository or its tests.

The tested production target is **Ubuntu 24.04 LTS on amd64 with systemd**, at least 768 MiB RAM and 2 GiB free on `/opt`. Use at least 2 GiB RAM for stack provisioning. Other distributions and architectures are not supported. Local Windows preview remains available for development.

## 1. Install on an existing VPS

From an exact, reviewed local checkout, the single installation command is:

```bash
sudo bash deploy/install.sh --trusted-checkout
```

It asks for `INSTALL`. For explicit automation use `--non-interactive`. Add `--enable-provisioning` to permit stack installation from the authenticated dashboard. Without that option, authenticated monitoring remains available and stack writes are disabled.

For a fresh server without the checkout, use a **published, versioned release artifact**. There is no release URL provisioned by this change: build/review the candidate and publish it first as described below. Obtain its SHA256 through a trusted independent channel, replace both placeholders, then run this single compound command on the server:

```bash
sudo bash -c 'set -euo pipefail; umask 077; work=$(mktemp -d /root/wm-release.XXXXXXXX); trap "rm -rf -- \"\$work\"" EXIT; apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl python3; curl --fail --location --proto "=https" --proto-redir "=https" --output "$work/release.tgz" "https://RELEASE_HOST/wordpress-manager-0.1.0.tar.gz"; printf "%s  %s\n" "TRUSTED_SHA256" "$work/release.tgz" | sha256sum --check --strict; mkdir "$work/source"; tar --extract --gzip --file "$work/release.tgz" --directory "$work/source" --no-same-owner --no-same-permissions; bash "$work/source/deploy/install.sh" --non-interactive --enable-provisioning'
```

The archive is verified **before** any downloaded code is executed or unpacked. Never replace this with `curl | bash`, fetch a moving branch, or download an unpinned checksum alongside an untrusted script and call that verification. The one-command download uses standard Ubuntu tools; it bootstraps Python itself. All extraction paths come from the trusted, reviewed artifact built by our release builder.

The installer validates Ubuntu/systemd/architecture/resources/root privileges, installs Python/venv and small OS dependencies, creates `wm-dashboard` without login/sudo, builds a root-owned versioned release and venv, installs a hardened Gunicorn unit plus a separate control service, enables reboot startup, and checks process identity, release directory, listener ownership and HTTP health three times. Production Python packages use exact wheel versions and SHA256 hashes. APT relies on configured signed Ubuntu repositories; OS package updates are not frozen forever.

Repeated installation of identical source succeeds after validating the existing release and health, preserving credentials/configuration. Different source requires an explicit upgrade. Unmanaged directories, accounts, services, unexpected drop-ins, unsafe ownership and unfinished transactions are refused. Installation never adopts or overwrites a legacy deployment.

Open a tunnel from your computer:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8080:127.0.0.1:8080 ADMIN@SERVER
```

Browse **http://127.0.0.1:8080**, username **admin**. Retrieve the generated password through your existing SSH session:

```bash
sudo cat /var/lib/wordpress-manager-control/initial-password
```

The password is generated on the server, kept root-only and never printed into installer/cloud-init logs. Do not open firewall port 8080 or create a public proxy. HTTP cookies intentionally omit `Secure` for this loopback HTTP workflow; SSH encrypts transit. Cookies are HttpOnly, SameSite=Strict and expire after 30 minutes.

## 2. Create a Hetzner server with cloud-init

Copy [deploy/cloud-init.yaml](deploy/cloud-init.yaml). Replace the SSH public key, immutable public HTTPS artifact URL and independently trusted SHA256. Set `provision_stack` to `true` only for a disposable/new server or one explicitly approved for these changes. Default is panel installation only. Never put credentials, private keys or GitHub tokens in user data.

In Hetzner Console, create a server using the official **Ubuntu 24.04** image, choose an amd64 instance, add your provider SSH key as a recovery path, and paste the completed YAML into the **Cloud config / user data** field before creating it. Alternatively, with an already authenticated Hetzner CLI:

```bash
hcloud server create --name wordpress-manager --image ubuntu-24.04 --type YOUR_AMD64_TYPE --ssh-key YOUR_KEY_NAME --user-data-from-file ./cloud-init.yaml
```

The template creates `wmadmin` with your public key and passwordless administrator sudo, preserves the default Ubuntu user, and disables SSH passwords. It validates the administrator's authorized key and effective SSH configuration before enforcing key-only SSH and reloading SSH. Provider root keys stay usable until you verify the new account; root passwords are disabled. Invalid placeholders fail visibly. Validate `wmadmin` access before changing/removing recovery accounts or keys. Then, if desired, change `PermitRootLogin prohibit-password` to `PermitRootLogin no` in `/etc/ssh/sshd_config.d/00-wordpress-manager.conf`, validate with `sudo sshd -t`, and reload SSH. The Hetzner console remains the recovery route.

Automatic Ubuntu security updates are enabled without automatic reboots. Cloud-init validates the release checksum before extracting/executing it, installs the panel without prompts, optionally creates and explicitly approves a root-only stack plan, and records status:

```bash
sudo cloud-init status --long
sudo cat /var/lib/wordpress-manager-cloud-init-status
sudo tail -n 100 /var/log/wordpress-manager-cloud-init.log
```

Cloud-init is a first-boot workflow, not an upgrade mechanism. Do not blindly rerun all modules on an existing server. Its schema and embedded Python are tested locally; provider first-boot/SSH connectivity still needs a disposable Hetzner smoke test before production use. [Hetzner user-data support](https://docs.hetzner.com/cloud/servers/faq/) requires a provider system image; [cloud-init SSH documentation](https://cloudinit.readthedocs.io/en/stable/reference/yaml_examples/ssh.html) explains key handling.

## 3. Install the WordPress stack through the dashboard

Install the panel with `--enable-provisioning`, sign in, open **Server Setup → Preview installation plan**, choose PHP, review the exact package/artifact list, and type **INSTALL MISSING COMPONENTS** to approve. Refresh jobs for the durable result. Plans expire in ten minutes, belong to the authenticated session and can be applied once. The service rechecks the full baseline and APT resolution immediately before execution. It refuses a stale plan, an installed-package upgrade/removal, custom/partial installations, or non-Ubuntu-origin repositories.

The optional workflow installs missing Nginx, MariaDB, Certbot/Nginx plugin, PHP-FPM/CLI and WordPress extensions (MySQL, XML, cURL, mbstring, ZIP, GD, intl), pinned WP-CLI 2.12.0 and pinned phpMyAdmin 5.2.3. All resolved APT dependencies are shown and pinned in the approval; APT verifies Ubuntu repository signatures/package hashes. The external artifact hashes are committed in the restricted service, based on [WP-CLI release downloads](https://make.wordpress.org/cli/handbook/guides/verifying-downloads/) and [phpMyAdmin releases](https://www.phpmyadmin.net/files/5.2.3/).

New PHP installation supports Ubuntu **8.3**. Already installed **8.4/8.5** can be selected only when their CLI, FPM and required extensions are present; no PPA is added and nothing upgrades/replaces existing PHP versions or pools. Third-party APT repositories cause refusal when missing packages must be resolved; review such a host manually. Unknown software paths cannot be comprehensively discovered, so review the detected inventory and plan before approval.

phpMyAdmin is a separately managed Nginx instance and FPM pool, listening only on **127.0.0.1:8081**, without edits to `/etc/nginx` or `/etc/php`. Use another SSH tunnel:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8081:127.0.0.1:8081 ADMIN@SERVER
```

Browse http://127.0.0.1:8081. It uses database cookie authentication, denies root/empty-password logins, and creates no database users or embedded database passwords. Supply an existing suitably scoped database account. Existing unmanaged phpMyAdmin is preserved and its exposure is not certified. Shared Nginx/MariaDB/new FPM services are started only when newly installed. Certbot is installed but does not issue certificates or modify routing automatically. Site creation, TLS setup, staging and backups remain separate future work.

For an explicitly approved disposable/new server, the equivalent root CLI is:

```bash
sudo apt-get update
sudo python3 -I /opt/wordpress-manager/current/deploy/provision.py plan --php 8.3
# Review the emitted plan and use its exact id:
sudo python3 -I /opt/wordpress-manager/current/deploy/provision.py apply --plan PLAN_ID --non-interactive --approve-server-changes
```

Do not run these commands against the live Hetzner VPS as part of development. There is no arbitrary command/SQL/path/unit endpoint or dashboard sudo rule. See [the security architecture](docs/PROVISIONING.md).

## Releases and private repositories

Build a reproducible candidate from a reviewed checkout:

```bash
python deploy/release.py build wordpress-manager-0.1.0.tar.gz
```

It produces the archive and `.sha256`, normalizes line endings, excludes `.git`, virtual environments, local secrets/tests, records every included file in `release-manifest.json`, and uses deterministic archive metadata. CI builds this candidate too. Publish these exact reviewed bytes to a public immutable HTTPS object or a separate public release repository; distribute the digest through an authenticated release announcement or infrastructure configuration. A private development repository can remain private. Do not use expiring signed URLs or long-lived access tokens in cloud-init. The archive contains application source, so publication must be intentional. This change prepares artifacts and does not publish anything externally.

Layout:

```text
/opt/wordpress-manager/{.managed.json,state.json,transaction.json,uninstalled.json,current,releases/}
/etc/wordpress-manager/web.json                         root:wm-dashboard 0640
/var/lib/wordpress-manager-control/                     root-only credentials, password, audit/jobs SQLite
/var/log/wordpress-manager-deploy.log                   root-only deployment log
/run/wordpress-manager/control.sock                    root:wm-dashboard 0660
/etc/systemd/system/wordpress-manager{,-provisioner}.service
/var/lib/wordpress-manager-stack/                       optional private phpMyAdmin and ownership records
```

## Upgrade, rollback and removal

Use a separate verified release directory or reviewed checkout. Never edit installed code or reuse an installed venv. For reviewed source add `--trusted-checkout`; a published release validates its complete manifest automatically:

```bash
sudo bash deploy/upgrade.sh --approve-upgrade --trusted-checkout
sudo bash deploy/upgrade.sh --rollback --approve-upgrade
sudo bash deploy/upgrade.sh --rollback RELEASE_ID --approve-upgrade
```

New releases are built and validated before switching `current`. Upgrade restarts the panel/control service and verifies health; failed activation restores the old release and verifies recovery. Prior releases remain available. Credentials, sessions, control configuration and sites live outside releases. A global lock excludes simultaneous panel deployment and stack package changes. Rollback affects panel code only; it never reverses OS package operations or deletes databases.

After interruption or failed rollback:

```bash
sudo bash deploy/upgrade.sh --recover --approve-upgrade
```

This uses the durable transaction to remove an incomplete fresh panel install, restore the pre-upgrade release, or finish panel removal. Changed ownership/accounts/policies or corrupt releases require investigation rather than automatic adoption. An interrupted account creation before its UID was recorded is deliberately left for manual inspection. Do not delete journals to bypass checks.

Remove the panel only, from a separate verified release directory or reviewed checkout (the installed copy removes its own code). Release archives ship executable shell entrypoints; for a Windows-origin checkout first run `chmod +x deploy/*.sh` on Ubuntu:

```bash
sudo ./deploy/uninstall.sh --dry-run
sudo ./deploy/uninstall.sh                # review, then type REMOVE PANEL
sudo ./deploy/uninstall.sh --yes          # explicitly approved automation
```

Dry run checks ownership and previews removal without creating logs, locks or journals, or stopping services. The final account scan runs after the services stop; its result is conditional in the preview. Removal takes the same exclusive lock as deployment and stack provisioning, so a running package job must finish first. Low free disk space does not prevent removal.

The uninstaller stops/disables both dashboard and privileged provisioner, removes their reviewed systemd units and recorded enablement links, removes the root-owned Unix provisioning socket/runtime directory, and removes recorded releases and Python environments. It reloads systemd and verifies panel units are inactive/disabled. It deletes `wm-dashboard` only when the recorded identity is still a locked dedicated account and a filesystem ownership scan finds no external user/group-owned resources; otherwise it retains the account and reports why. If that account must remain, even a recovery purge keeps a minimal non-secret ownership manifest so subsequent removal stays safe and repeatable. Resolve the reported external ownership/account issue separately, then rerun the purge to remove that final manifest. It never deletes account homes. No package manager, SQL, site-account or shared-service mutation runs during uninstall.

**Recovery data is retained by default:** `/etc/wordpress-manager`, `/var/lib/wordpress-manager-control` (credentials, initial password, SQLite audit/jobs), and `/opt/wordpress-manager` metadata (installation manifest, release state and removal receipt). Retained authentication directories become root-only with root group ownership. Deployment logs and journald history remain. Retained metadata prevents installation from silently replacing configuration.

After backing up anything required, explicitly approve destruction of retained panel data:

```bash
sudo ./deploy/uninstall.sh --dry-run --purge-recovery
sudo ./deploy/uninstall.sh --purge-recovery        # type REMOVE PANEL AND RECOVERY DATA
sudo ./deploy/uninstall.sh --yes --purge-recovery  # explicit automated purge
```

This also works after completed default removal. Deployment/system logs still remain under the server's log-retention policy. It never removes WordPress production/staging installations, databases or database users, site accounts or FPM pools, website Nginx configuration, PHP/Nginx/MariaDB, WP-CLI/phpMyAdmin/Certbot, SSL certificates/renewals, or backups/schedules. The separate phpMyAdmin services (`wordpress-manager-dbadmin*`) and loopback port **8081** remain operational and are intentionally retained; they execute system PHP/Nginx with configuration outside panel releases. Panel port **8080** and the provisioning socket disappear. Installation creates no public management route or firewall rule. Any operator-added proxy/firewall configuration requires separate review.

`.managed.json` is the root-owned installation resource manifest. It records the dedicated account UID, configuration directory device/inode identities, and every release's directory and file identities, including the venv. Unit hashes are fixed in reviewed deployment code. Removal validates fixed paths, ancestors, loaded unit fragments/drop-ins, boot links, file ownership, hard links, mounts, and unexpected files **before stopping services**. Unknown resources are refused. Missing recorded files are safe; repeated removal is supported. An old installation without this resource manifest requires manual ownership review/migration; the uninstaller will not infer ownership and delete it.

Normal website operation must use system PHP-FPM/Nginx/MariaDB and site-owned data independently of either panel unit or `/opt/wordpress-manager`. Reverse systemd dependencies on panel services cause removal to stop for manual correction. Operator-created cron jobs, scripts or proxies referencing panel code must also be moved to independent tools before removal. **Website deletion is a separate operation**, outside this uninstaller: it requires its own verified database/files backups and site-specific confirmation. Uninstall has no website-deletion option.

Verify removal and websites using your recorded baseline (replace example units/domains):

```bash
sudo systemctl show wordpress-manager.service wordpress-manager-provisioner.service -p ActiveState -p UnitFileState -p FragmentPath
sudo ss -ltnp '( sport = :8080 )'           # no panel listener
sudo test ! -e /run/wordpress-manager/control.sock
sudo systemctl is-active nginx mariadb php8.3-fpm
sudo nginx -t
curl --fail --head https://YOUR_PRODUCTION_DOMAIN/
curl --fail --head https://YOUR_STAGING_DOMAIN/
sudo certbot certificates
sudo systemctl list-timers --all           # compare renewal/backup timers to baseline
```

Also test a normal WordPress database-backed page/login, inspect site-specific pools and backup cron jobs, and confirm database users/grants using your usual administrative tools. Take independent website/database backups before server maintenance; this uninstaller neither creates nor deletes them.

Interrupted removal resumes with the **same** command and purge setting. It preserves `transaction.json` until completion, retaining any earlier deployment journal inside the removal receipt. `upgrade.sh --recover --approve-upgrade` also resumes a recorded uninstall with its recorded retention choice. A failed stop/disable, changed unit, unrecorded file or changed identity requires investigation; the command exits nonzero and leaves recovery records. Never delete manifests or journals to bypass checks. Interruption between the final manifest unlink and directory removal may leave only an empty `/opt/wordpress-manager`; a retry reports and retains that empty unrecorded directory. After confirming it is empty, use `sudo rmdir /opt/wordpress-manager` before reinstalling.

To recover the panel after completed removal, first make a root-only archive of retained state outside panel directories:

```bash
sudo install -d -m 0700 /root/wm-recovery
sudo tar -C / -cpf /root/wm-recovery/panel-state.tar \
  opt/wordpress-manager etc/wordpress-manager var/lib/wordpress-manager-control
sudo chmod 0600 /root/wm-recovery/panel-state.tar
```

Keep a separate copy of the verified release artifact identified in `state.json`; credentials and the audit database are secrets. After checking the archive, explicitly purge retained panel resources, reinstall that verified release, stop both panel services, and restore **only** the saved `/etc/wordpress-manager` and `/var/lib/wordpress-manager-control` contents. Do not overwrite the new installation manifest/release pointers with old inode records. Set control state to root:root with directory mode 0700 and files 0600; set `/etc/wordpress-manager` to root:wm-dashboard 0750 and `web.json` to root:wm-dashboard 0640. Start the provisioner and dashboard and verify `/healthz`, authenticated login, audit history and the loopback-only listener. Restored `web.json` retains the prior provisioning policy. Restore only trusted state while services are stopped. Without a saved artifact, install a reviewed compatible release and review state compatibility first. Purged data requires independent backups; website recovery remains separate.

## Troubleshooting and failure recovery

```bash
sudo systemctl status wordpress-manager wordpress-manager-provisioner --no-pager
sudo journalctl -u wordpress-manager -u wordpress-manager-provisioner -n 100 --no-pager
sudo tail -n 100 /var/log/wordpress-manager-deploy.log
sudo cat /opt/wordpress-manager/state.json /opt/wordpress-manager/transaction.json
sudo ss -ltnp '( sport = :8080 or sport = :8081 )'
sudo python3 -c "import sqlite3; c=sqlite3.connect('/var/lib/wordpress-manager-control/ledger.sqlite'); print(c.execute('select * from jobs').fetchall())"
```

A missing transaction file after success is normal. Fix disk/network/package availability and use the documented recovery command for panel transactions. APT-installed dependencies remain after failed panel installation. Restore from an independent VPS snapshot if a system package operation requires full host recovery.

Provisioning failures retain a failed job and any safely installed packages/resources; package operations are not rolled back by deleting software. On service restart, previously running jobs become **interrupted**, never automatically replayed. Inspect `sudo dpkg --audit`, service logs and the original plan. If package configuration was interrupted, review before running `sudo dpkg --configure -a`; then create a fresh plan. A temporary `/usr/sbin/policy-rc.d` start guard may remain after power loss/SIGKILL: remove it only after verifying its exact contents are the WordPress Manager marker followed by `exit 101` and no job is running. An existing operator-managed policy is never overwritten.

A partial private phpMyAdmin install is refused on retry. Inspect `/var/lib/wordpress-manager-stack/owned.json` and the two `wordpress-manager-dbadmin*` units, fix the reported issue, and verify private health before establishing a ready marker. Do not remove or replace unmanaged paths. Use a VPS snapshot for automatic whole-system recovery; the installer intentionally does not guess which partial databases or shared configurations are safe to erase.

Lost password: use the retained root-only initial-password file. To revoke all sessions, stop the control service, replace its credential hash/salt and rotate `secret_key` in `web.json` through an administrator-controlled process, then restart both services. Never place passwords in process arguments or logs. Read-only access logs remain bounded in memory and mirrored to journald; login/approval/job events are durable in the protected SQLite ledger. Protect/rotate exported audit logs according to your own retention policy.

## Local development and tests

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m wordpress_manager
.venv/bin/python -m pytest -q
```

On PowerShell use `.\.venv\Scripts\python`. Local preview has no authentication or mutation routes unless explicit test authentication is injected; deployed systemd requires the root-managed authentication file and fails closed when it is missing. Linux probes run only on the local machine. Snapshot observations use bounded fixed executables and are cached; unknown readings remain unknown. No development command connects to the VPS.

The optional browser regression submits the actual login form to a temporary loopback server and checks Chrome's generated Origin header and session persistence. Install `.[test,browser]`, then run `WM_TEST_BROWSER=chrome python -m pytest -q tests/test_control.py` with installed Google Chrome (PowerShell: `$env:WM_TEST_BROWSER='chrome'`). For bundled Chromium, run `python -m playwright install chromium` and set `WM_TEST_BROWSER=chromium`. CI runs this test with Chromium; ordinary unit tests skip it when the variable is unset.

Unit tests cover release integrity/path traversal, plans, package version preservation, authentication/CSRF, session-bound one-use confirmation, throttling/secret redaction, deployment transactions and listener policy. CI runs ordinary tests on Windows/Ubuntu and real root deployment/provisioning tests only on fresh disposable Ubuntu VMs. Never mark a real server as a disposable test system.

For local systemd integration in Docker (Linux containers):

```bash
docker build -t wordpress-manager-deploy-test -f tests/deploy.Dockerfile .
docker run -d --name wm-deploy-test --privileged --cgroupns=private --tmpfs /run --tmpfs /tmp --mount "type=bind,source=$(pwd),target=/repo,readonly" wordpress-manager-deploy-test
docker exec wm-deploy-test sh -c "printf 'disposable Ubuntu deployment test\n' > /run/wm-disposable-test"
docker exec wm-deploy-test python3 /repo/tests/deploy_integration.py --disposable-system
docker exec wm-deploy-test python3 /repo/tests/stack_integration.py --disposable-system
docker stop wm-deploy-test
docker rm wm-deploy-test
```

The privileged flag is only for this disposable systemd guest. Do not mount host system directories or publish ports. Tests check fresh/repeat install, failures, root ownership, localhost listeners, upgrade/rollback/recovery, authenticated provisioning, private phpMyAdmin, existing PHP configuration preservation and data-preserving uninstall. The guarded stack test creates real production/staging WordPress fixtures with MariaDB users/data, a dedicated site account/FPM pool, local TLS, backups and a cron backup schedule. Dry run, default uninstall, repeated removal and explicit recovery purge must preserve HTTP/HTTPS responses, database access/content/grants, file hashes, shared package versions and running service PIDs. Actual PHP 8.4/8.5 binary preservation is covered by simulated inventories; those third-party runtime combinations and provider first boot are not certified by this test run.
