# Full local Docker development

This setup is **not a production release** and never modifies the Hetzner VPS.
Use Docker Desktop with Linux containers / WSL 2 on your Windows development PC.

## Start the local development stack

From the repository root in PowerShell, after updating your local branch:

```powershell
docker compose -f compose.preview.yml -f compose.stack.yml up --build
```

Open these independent localhost pages:

| Address | Service | What you can test |
|---|---|---|
| http://127.0.0.1:8090 | WordPress Manager | Real Flask login/logout, CSRF/session and UI; development-only simulated privileged setup |
| http://127.0.0.1:8091 | WordPress | Real WordPress served by Nginx/PHP 8.5-FPM, using MariaDB 10.11 |

At first startup, the WordPress URL displays the WordPress installation wizard.
Complete it using **new local development credentials**. Do not reuse passwords
or data from production. The initial database can take some time to initialise.

Once signed into the manager, use the **Check local WordPress stack** link in
its development banner. That screen makes a real, read-only HTTP check from
the manager container to the Nginx/WordPress container.

The manager's **local preview** login is `admin` / `preview-only`; it does
not use or affect Hetzner credentials. Choose a different local-only password by
setting `WM_PREVIEW_PASSWORD` for the dashboard service if you need one. This
is intended only for a private desktop preview on loopback.

The dashboard uses the **real application's authentication routes** and secure
session/CSRF checks, with a constrained in-memory development auth backend.
That backend's setup plan and apply screens currently provide a **simulation
only**; the implementation must not run privileged installation commands.
Site creation, backups and staging are not yet implemented in the manager UI.
As each feature is added, connect its local-development adapter to this
isolated WordPress/database stack and validate actual effects. Do **not** give
the dashboard the Docker daemon socket or host root access.

## Test with real WP-CLI

After completing the WordPress browser installer, in a second PowerShell:

```powershell
docker compose -f compose.preview.yml -f compose.stack.yml run --rm wpcli core version
docker compose -f compose.preview.yml -f compose.stack.yml run --rm wpcli plugin list
docker compose -f compose.preview.yml -f compose.stack.yml exec database mariadb --version
```

The WP-CLI image uses the same PHP major/minor version as the WordPress FPM
image and the WP-CLI 2.12.0 series.

Source changes to `wordpress_manager/` are bind-mounted read-only into
the manager's development container. Edit in Codex/VS Code and refresh
http://127.0.0.1:8090; Python changes trigger the development reload.
The WordPress/database files live in **named Docker volumes**, not on the
Hetzner server, so they survive ordinary container restarts.

## If Docker Nginx shows as stopped

The local stack uses the upstream **unprivileged Nginx** image. Its master
does not need to switch user identities, so the Compose security restriction
`cap_drop: [ALL]` can remain enabled. Do not remove the container security
restrictions or publish Nginx/database ports on public interfaces.

To inspect failures in PowerShell:

```powershell
docker compose -f compose.preview.yml -f compose.stack.yml logs --tail=80 nginx
docker compose -f compose.preview.yml -f compose.stack.yml ps -a
```

After updating the repository with the unprivileged image fix, recreate only
the local Nginx service (without resetting WordPress or database volumes):

```powershell
docker compose -f compose.preview.yml -f compose.stack.yml pull nginx
docker compose -f compose.preview.yml -f compose.stack.yml up -d --no-deps --force-recreate nginx
```

Then open http://127.0.0.1:8091. Do not use `down --volumes` to troubleshoot
a service startup issue: that intentionally deletes your local test websites.

## Stop, reset and limits

```powershell
docker compose -f compose.preview.yml -f compose.stack.yml down
```

The command above retains test WordPress files and database data.

To intentionally **delete only the local Compose volumes and test sites**,
after verifying your Compose project context:

```powershell
docker compose -f compose.preview.yml -f compose.stack.yml down --volumes
```

Do not run the reset command against any unrelated Compose project.

The manager container uses Ubuntu 24.04 and the production-locked Python
wheels. WordPress PHP 8.5-FPM, MariaDB 10.11 and Nginx run in separate
containers. Their **versions and functions** resemble Hetzner, but these are
not identical Ubuntu APT packages, and normal Docker Compose does not run
the host's systemd services. The actual privileged Ubuntu installation,
restricted SSH transport, permissions, rollback and package integration
are still validated by the existing disposable-system CI suites before
production deployment.

Neither port 3306 (MariaDB) nor port 9000 (FastCGI) is published; only
127.0.0.1:8090 (manager) and 127.0.0.1:8091 (WordPress) are mapped.
Development passwords are hardcoded local fixtures, never production secrets.
No Hetzner keys, service credentials, Docker daemon socket or production
website volumes should be mounted into this environment.
