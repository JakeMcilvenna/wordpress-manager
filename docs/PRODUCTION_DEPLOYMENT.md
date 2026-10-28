# Production CI/CD

This procedure is for an operator to run later. Development of this feature does
not contact the VPS, create real deployment credentials, change GitHub settings,
or enable deployment. Ubuntu 24.04 amd64 and an existing healthy **managed** panel
installation are required. Keep your normal administrator SSH session and tunnel.

## What runs

- Push to `main` (and other development branches): CI only. Pull requests: CI only.
- Push to `production`: `production.yml` calls the same `ci.yml` at that exact
  commit. All Windows/Linux unit matrices, real Chromium login, disposable systemd
  lifecycle, SSH/sudo boundary, stack/site preservation, and cloud-init checks must
  pass. Only then can the release job build/upload an artifact.
- Manual **Production deployment → Run workflow → production** follows exactly
  the same gates. Selecting any other branch runs verification but cannot deploy.
  Keep these workflow files on the default branch so GitHub shows manual dispatch.
- The release is exported from immutable Git objects, never the working tree. Two
  builds must have identical bytes. The manifest carries the full commit and every
  file SHA256. CI checks the outer digest, file manifest and commit again before SSH.
- Only the current run's named artifact is downloaded. No `workflow_run`, external
  artifact IDs, caller-supplied commit inputs, path filters, or skipped-test override
  can bypass the dependency on successful CI.

`production` is the deployment branch; keep `main` for development/testing. Protect
both branches, require PR reviews and successful checks, and protect changes to
`.github/workflows/`, `deploy/`, and dependency pins with your normal code review
policy. Prevent force pushes to production. The SSH key authorizes application
delivery; the server trusts the protected workflow to establish that tests passed.
A SHA256 manifest alone is not a publisher signature or an independent CI attestation.

## Privileged boundary

The system user `wm-deploy` has a locked password, no home, no supplementary groups,
and `/bin/sh` solely because OpenSSH invokes its forced command through the login
shell. SSH never grants a usable shell. Root-owned key policy and an effective
`Match User` block reject shell commands, SFTP/SCP, PTYs, user rc files, password
login, agent/socket/TCP/X11 forwarding and tunnels. There is no file-download API,
client-data group, site-data ownership, dashboard session, or site/database credential
in this account or GitHub. Existing world-readable host files are not made private
by creating this account; preserve the existing site permission policy.

Only `wm-deploy COMMIT SHA256` with two fixed-format hexadecimal values is accepted.
The unprivileged forced command bounds and hashes stdin. Sudo permits exactly
`/usr/local/sbin/wm-ci-upgrade` **with no arguments**. That root-owned wrapper invokes
isolated Python with a fixed script and fixed environment. No shell evaluation,
caller-supplied executable/path/environment, upload directory or uploaded root
entrypoint exists. The root receiver independently bounds/hashes the request,
extracts into a root-only private staging directory, verifies all files and commit,
then compares **every `deploy/` file** against a root-owned policy inventory.

The controller runs the pinned, server-owned `deploy/upgrade.sh` with
`--release-source` pointing at verified private staging. It never runs uploaded
deployment scripts. The managed upgrader uses its trusted sibling verifier, fixed
systemd units, hash-pinned binary wheels and existing lock/transaction/health logic.
The application is copied into a new root-owned release and runs as `wm-dashboard`;
application validation also drops to that account. Uploaded Python packaging/build
hooks are never run as root. Root provisioner code, requirement hashes and systemd
policy must match the reviewed baseline. A commit changing any deployment file is
intentionally refused until an administrator separately reviews/updates the baseline.
GitHub cannot update that baseline through this operation.

No APT, provisioning, PHP, Nginx, MariaDB, SQL, WordPress, website or database mutation
is invoked by the CI receiver or forward upgrade. Only panel releases and its two
existing services are managed. Credentials and web/session configuration remain
outside releases and are neither regenerated nor replaced. The dashboard still
binds **127.0.0.1:8080**, reached only through the existing administrator SSH tunnel;
the CI account cannot create a tunnel. No public listener, reverse proxy or firewall
change is part of setup or deployment.

## Exact GitHub configuration

Create a GitHub environment named **`production`**. Restrict deployment branches to
the selected branch **`production`** only (exclude tags). Environment reviewers are
optional: an approval gate pauses each automatic deployment until approved; leave
it off for unattended pushes if your organization permits that. Set these values;
do not add root/admin SSH keys or panel/site passwords.

| Kind | Scope | Name | Value |
| --- | --- | --- | --- |
| Secret | environment `production` | `PRODUCTION_DEPLOY_SSH_KEY` | Dedicated Ed25519 private key for `wm-deploy` only |
| Variable | repository | `PRODUCTION_DEPLOY_ENABLED` | `false` initially; exact `true` only after server setup/review |
| Variable | environment `production` | `PRODUCTION_DEPLOY_HOST` | VPS IP or DNS name, no username/scheme |
| Variable | environment `production` | `PRODUCTION_DEPLOY_PORT` | SSH port as decimal, e.g. `22` |
| Variable | environment `production` | `PRODUCTION_SSH_HOST_KEY` | Trusted host public key: `ssh-ed25519 BASE64`, no hostname/comment |
| Variable | environment `production` | `PRODUCTION_SSH_HOST_FINGERPRINT` | Trusted `SHA256:...` fingerprint of that same host key |

No PAT, root SSH key, deploy password, client data or dashboard password is needed.
`GITHUB_TOKEN` has only `contents: read`. Environment secrets are used only in the
deployment job; verification jobs do not receive the deployment key. Actions are
pinned to full upstream commit SHAs; review and update those pins deliberately.

Example commands on the **operator workstation**, once the operator chooses to
set up real credentials (not run during local development). `OWNER/REPO`, `SERVER`
and the trusted host-key files are operator-supplied values:

```bash
gh variable set PRODUCTION_DEPLOY_ENABLED --repo OWNER/REPO --body false
ssh-keygen -t ed25519 -f ~/.ssh/wm-production-ci -C github-actions-wm-production
# For unattended CI, choose an empty passphrase; protect this dedicated key.
# Copy ONLY ~/.ssh/wm-production-ci.pub to /root/wm-deploy.pub using admin access.
# Do not upload the private key to the VPS or repository.
gh secret set PRODUCTION_DEPLOY_SSH_KEY --repo OWNER/REPO --env production < ~/.ssh/wm-production-ci
gh variable set PRODUCTION_DEPLOY_HOST --repo OWNER/REPO --env production --body SERVER
gh variable set PRODUCTION_DEPLOY_PORT --repo OWNER/REPO --env production --body 22
gh variable set PRODUCTION_SSH_HOST_KEY --repo OWNER/REPO --env production --body "$(cat trusted-host-key.txt)"
gh variable set PRODUCTION_SSH_HOST_FINGERPRINT --repo OWNER/REPO --env production --body "$(cat trusted-host-fingerprint.txt)"
```

## Exact server setup (administrator, later)

1. Leave the GitHub enable variable `false`. Obtain a reviewed checkout of the
   intended production commit at **`/root/wm-ci-reviewed`** using your normal
   administrator-controlled source-transfer process. Do not run this setup from an
   Actions upload or grant Actions access to this path. Review the setup/controller,
   SSH/sudo policies, existing managed installation and the release manifest/pins.
   Existing/custom installations require review; setup refuses to adopt existing
   CI resources/account or a mismatched privileged runtime.
2. Use the trusted Hetzner console or an already verified administrative session to
   read the SSH host key. Do not use `ssh-keyscan` or first-connection acceptance:

   ```bash
   sudo awk '{print $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
   ```

   Put the first command's single line in `trusted-host-key.txt` locally and the
   second command's `SHA256:...` field alone in `trusted-host-fingerprint.txt`.
   The client recomputes that fingerprint and uses a temporary dedicated
   `known_hosts`, `StrictHostKeyChecking=yes`, Ed25519-only host authentication,
   disabled agent/forwarding, and no user/global SSH configuration. Key changes fail
   closed; re-verify via the trusted console before changing both GitHub variables.
3. Run the following through administrator access, **not Actions**. OpenSSH server,
   client, sudo, Python 3.12 and Python venv support must already be installed.
   Provision missing deployment prerequisites separately; the application deployment
   operation installs no OS packages. An active SSH service supplies `/run/sshd`.

   ```bash
   sudo systemctl is-active ssh.service wordpress-manager.service
   sudo test -f /opt/wordpress-manager/.managed.json
   sudo test ! -e /opt/wordpress-manager/transaction.json
   sudo /usr/bin/python3 -I -B /root/wm-ci-reviewed/deploy/ci/setup.py \
     --reviewed-checkout --public-key /root/wm-deploy.pub
   sudo visudo -cf /etc/sudoers
   sudo /usr/sbin/sshd -t
   sudo /usr/sbin/sshd -T -C user=wm-deploy,host=localhost,addr=127.0.0.1
   sudo -l -U wm-deploy
   sudo id wm-deploy
   sudo test ! -e /etc/wordpress-manager-ci/enabled
   sudo systemctl reload ssh.service
   ```

   Keep the original administrator session open and verify a second administrator
   session and the original dashboard tunnel before closing it. Setup validates
   effective SSH restrictions and leaves the server gate absent/disabled. If setup
   fails partway, inspect its root-owned partial resources; do not enable or retry
   by deleting paths without reviewing them.
4. From the workstation with the dedicated key and a trusted known-hosts file,
   verify denied operations. These commands **must fail** (do not substitute your
   administrator key/account). Supply `-p PORT` if needed:

   ```bash
   ssh -i ~/.ssh/wm-production-ci -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes wm-deploy@SERVER id
   ssh -i ~/.ssh/wm-production-ci -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes wm-deploy@SERVER 'sudo -s'
   ssh -i ~/.ssh/wm-production-ci -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -N -R 0:127.0.0.1:8080 wm-deploy@SERVER
   sftp -i ~/.ssh/wm-production-ci -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes wm-deploy@SERVER
   ```

   Server-side checks through the administrator session:

   ```bash
   sudo runuser -u wm-deploy -- sudo -n /bin/sh
   sudo runuser -u wm-deploy -- sudo -n /usr/local/sbin/wm-ci-upgrade --rollback
   sudo runuser -u wm-deploy -- cat /var/lib/wordpress-manager-control/credentials.json
   # All three must fail. The deployment account must have only its own group.
   ```

5. Finish setup explicitly, using both independent gates:

   ```bash
   # Administrator session on the VPS, only after the checks above:
   printf 'enabled\n' | sudo tee /etc/wordpress-manager-ci/enabled >/dev/null
   sudo chown root:root /etc/wordpress-manager-ci/enabled
   sudo chmod 0600 /etc/wordpress-manager-ci/enabled
   # Operator workstation:
   gh variable set PRODUCTION_DEPLOY_ENABLED --repo OWNER/REPO --body true
   gh workflow run production.yml --repo OWNER/REPO --ref production
   ```

   The manual run executes all tests first and performs a real panel-only upgrade.
   Review the receipt, audit and tunnel login. Subsequent pushes to `production`
   deploy automatically. Enabling the variable does not retroactively run skipped
   deployments. Re-running an old workflow deliberately deploys its tested old SHA.

## Server paths, concurrency and audit

```text
/usr/local/lib/wordpress-manager-ci/{receive.py,ssh_gate.py,policy/deploy/...} root-owned code
/usr/local/sbin/wm-ci-upgrade                                               root-owned wrapper
/etc/ssh/sshd_config.d/00-wordpress-manager-ci.conf                           account-only SSH policy
/etc/sudoers.d/wordpress-manager-ci                                         one no-argument operation
/etc/wordpress-manager-ci/{authorized_keys,policy.json,enabled}              root-controlled policy/gate
/var/lib/wordpress-manager-ci/                                             root-only staging/audit
/var/lib/wordpress-manager-ci/deployments.jsonl                             durable attempt/result events
/var/lib/wordpress-manager-ci/upgrade.log                                   root-only upgrade diagnostics
```

GitHub's concurrency group queues production workflows without cancelling an
in-flight activation. The receiver has its own nonblocking exclusive lock and the
managed upgrader also takes `/run/wordpress-manager-deploy.lock`, shared with stack
jobs and manual deployment. Lock conflicts fail the request, never switch releases.
Do not unlink lock files. GitHub may replace older pending runs with a newer pending
run; a successful active deployment is never intentionally cancelled by this policy.

Each accepted request journals a `started` event before mutation and a `success` or
`failed` event with UTC timestamp, attempt ID, Git SHA, artifact digest and release
ID (null until known). Success also records the previous release. The selected
release's `release.json` records `git_commit`. Malformed SSH commands/checksums that
fail before identity validation are denied without a trusted deployment event;
OpenSSH logs authentication/session attempts. A power loss/SIGKILL can leave a
`started` event without a terminal event: treat that as incomplete, never success.
The server returns only a success receipt, never root/application diagnostics or
client files. GitHub stores that receipt as an artifact; failures remain visible as
failed runs. Root logs are authoritative and are not tamper-proof against root.
Retain/rotate them under your normal root log-retention policy.

```bash
sudo tail -n 20 /var/lib/wordpress-manager-ci/deployments.jsonl
sudo tail -n 100 /var/lib/wordpress-manager-ci/upgrade.log
sudo cat /opt/wordpress-manager/current/release.json
sudo cat /opt/wordpress-manager/state.json
sudo journalctl -u wordpress-manager -u wordpress-manager-provisioner -n 100 --no-pager
```

## Disable, rollback and recovery

Disable future deliveries first. Removing the server gate rejects requests that
have not yet passed the gate; it does not cancel an upgrade already in progress.

```bash
# Operator workstation:
gh variable set PRODUCTION_DEPLOY_ENABLED --repo OWNER/REPO --body false
# Administrator session:
sudo rm -f /etc/wordpress-manager-ci/enabled
```

The managed upgrader requires three consecutive healthy samples: service is active,
the PID/cwd/account match the selected release, the existing control socket is safe,
the listener is exclusively **127.0.0.1:8080**, and `/healthz` returns the private
healthy response. Failure after switching restores and health-checks the old release.
No previous release is garbage-collected. Prevalidation failure leaves the current
service running. A failed/interrupted recovery retains `transaction.json` and blocks
further forward upgrades rather than silently accepting uncertain state.

Administrator-only rollback uses the **pinned controller**, never a script supplied
through the CI channel. The CI account has no rollback/recover/shell operation:

```bash
sudo bash /usr/local/lib/wordpress-manager-ci/policy/deploy/upgrade.sh --rollback --approve-upgrade
# Or select a known retained release:
sudo bash /usr/local/lib/wordpress-manager-ci/policy/deploy/upgrade.sh --rollback RELEASE_ID --approve-upgrade
# For an unfinished managed transaction, inspect state/logs first:
sudo bash /usr/local/lib/wordpress-manager-ci/policy/deploy/upgrade.sh --recover --approve-upgrade
sudo cat /opt/wordpress-manager/state.json
curl --fail http://127.0.0.1:8080/healthz
sudo ss -ltnp '( sport = :8080 )'
```

Also verify the existing tunnel login with the **unchanged** panel password. Recovery
never requires resetting credentials. A mismatch in account/unit/policy ownership or
a damaged old release needs administrator investigation; do not remove manifests or
journals to bypass it. Rollback changes panel code, not websites, databases or OS
packages. Administrative rollback/recovery is logged in the existing managed log;
compare the retained release's `release.json` with the CI attempt audit for its SHA.

For a deployment-tooling/dependency-policy change, keep both gates disabled and
review/test a fresh checkout and the migration separately. The setup command refuses
an in-place reinstall by design. An administrator must explicitly back up and replace
the controller, policy inventory and relevant installed privileged runtime together,
then repeat SSH/sudo/health checks before enabling. Do not loosen the inventory check
or grant a CI path to update it. No automatic privileged-policy upgrade is supplied.

## Local disposable tests

Never run integration scripts on the VPS or create their guard on a real server.
The containers below have no published ports or host data mounts; privileged mode
is solely for disposable systemd. Mount only the repository read-only.

```bash
python -m pip install -e '.[test,browser]'
python -m playwright install chromium
WM_TEST_BROWSER=chromium python -m pytest -q
docker build -t wm-secure-cicd-test -f tests/deploy.Dockerfile .
docker run -d --name wm-ci-local --privileged --cgroupns=private --tmpfs /run --tmpfs /tmp \
  --mount "type=bind,source=$PWD,target=/repo,readonly" wm-secure-cicd-test
docker exec wm-ci-local sh -c "printf 'disposable Ubuntu deployment test\n' > /run/wm-disposable-test"
docker exec wm-ci-local python3 /repo/tests/deploy_integration.py --disposable-system
docker exec wm-ci-local python3 /repo/tests/ci_deploy_integration.py --disposable-system
docker exec wm-ci-local python3 /repo/tests/stack_integration.py --disposable-system
docker rm -f wm-ci-local
```

The SSH suite creates only a disposable guest key. It tests command/SFTP/forwarding
denial, wrong host keys, sudo argument restrictions, secret-file access denial,
disabled gate, malformed/traversal/corrupt artifacts, commit mismatch, policy/script/
requirements/unit replacement, both deployment locks, actual production sender,
healthy activation/receipt, failed-health rollback, manual rollback, unchanged
credentials and site/shared-component preservation. See [validation](VALIDATION.md)
for the actual local results and limitations.

The following stack suite additionally activates a verified CI release and rolls
it back while real WordPress fixtures serve HTTP/HTTPS. It compares database rows/
grants, site/configuration/backup hashes, accounts, credentials, shared process IDs
and installed package versions before and after both operations.

References: [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
[deployment environments and concurrency](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments),
[OpenSSH forced command and forwarding policy](https://man.openbsd.org/sshd_config).
