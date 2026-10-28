# WordPress Manager

A lightweight, private operations console for a self-hosted WordPress server. Built with Flask, locally vendored HTMX and plain HTML/CSS. No Node build, external fonts, CDN at runtime, database connection, cloud service or monitoring agent.

**v0.1 is read-only.** It detects local software, reports service health, and displays CPU, RAM, disk and uptime. Nothing installs software, restarts services, provisions sites, or changes WordPress. Websites, Staging and Backups have explicit empty states describing the next increments.

## Run locally

Requires Python 3.11+ (Ubuntu 24.04 includes Python 3.12).

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m wordpress_manager
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[test]'
.\.venv\Scripts\python -m wordpress_manager
```

Open **http://127.0.0.1:8080**. The launcher binds only to IPv4 loopback with debugging disabled. Linux data comes from the machine running Python. A Windows preview clearly labels itself and marks Linux-only measurements unavailable; it does not connect to your VPS.

If reusing `/home/jake/wordpress-manager/.venv`, activate/use that environment rather than recreating it. Installing this Python project does not change Nginx, PHP or MariaDB. Use a separate checkout if that directory contains existing work.

## Install on the VPS

Start with the manual unprivileged command above and verify the dashboard through an SSH tunnel. The production service below uses a dedicated account, root-owned application code, Gunicorn (one worker, two threads), a read-only filesystem and no Linux capabilities. No Nginx reverse proxy is necessary.

**The following administrator commands are an optional, operator-approved deployment procedure, not an automatic installer. Review them first.** They create a dashboard account and service only. Do not run them against an existing `/opt/wordpress-manager` installation; use a reviewed release upgrade instead.

From a clean checkout at the release you intend to deploy:

```bash
# Create a fresh destination; mkdir deliberately fails if it already exists.
sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin wm-dashboard
sudo mkdir /opt/wordpress-manager
git archive --format=tar HEAD -o /tmp/wordpress-manager-release.tar
sudo tar --extract --file=/tmp/wordpress-manager-release.tar --directory=/opt/wordpress-manager --no-same-owner
sudo python3 -m venv /opt/wordpress-manager/.venv
sudo /opt/wordpress-manager/.venv/bin/python -m pip install '/opt/wordpress-manager[production]'
sudo install -m 0644 /opt/wordpress-manager/deploy/wordpress-manager.service /etc/systemd/system/wordpress-manager.service
sudo systemd-analyze verify /etc/systemd/system/wordpress-manager.service
sudo systemctl daemon-reload
sudo systemctl enable --now wordpress-manager.service
sudo systemctl status wordpress-manager.service --no-pager
sudo ss -ltnp '( sport = :8080 )'
```

The listener must be **127.0.0.1:8080**, never `0.0.0.0:8080`. Do not open a Hetzner/UFW rule for port 8080 or publicly proxy it. If `python3-venv` is absent, have the operator approve installation of the matching Ubuntu package. Do not alter the existing PHP repository or select a replacement PHP version to deploy the dashboard.

From your computer, use your already configured SSH authentication:

```bash
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 jake@YOUR_SERVER
```

If local 8080 is occupied, use `127.0.0.1:8081:127.0.0.1:8080` and browse local port 8081. No private keys are uploaded to the application. SSH handles authentication. Any user already able to execute code on the server may be able to access its loopback listener; this MVP is designed for a trusted single-operator VPS, not hostile shared-shell hosting.

## What is measured

| Signal | Source | Limitations |
| --- | --- | --- |
| Packages | `/usr/bin/dpkg-query` | Package-managed installations; custom/manual paths may be undetected. Displays installed package versions, not proof the running process has been restarted onto that version. |
| Nginx, MariaDB, PHP-FPM | `systemctl show` | All versioned installed FPM pools are detected dynamically. Active state is process health, not an HTTP/database transaction check. |
| PHP CLI | `/usr/bin/php --version`, falling back to installed CLI package metadata | CLI and FPM are separate entries; package fallback does not establish the selected CLI binary. |
| WP-CLI | `/usr/local/bin/wp --info --no-color` | Runs from `/` with `WP_CLI_CONFIG_PATH=/dev/null`; never loads a site's WordPress installation. Other installation paths need an explicit code change. |
| CPU | `/proc/stat`, sample delta | First sample says Sampling. Excludes guest double counting; no artificial waiting. |
| Memory | `/proc/meminfo` | Total minus available, including reclaimable cache correctly. |
| Disk | Filesystem containing `/` | Not a sum of every mounted volume. Windows preview measures its current drive. |
| Uptime/load | `/proc/uptime`, `/proc/loadavg` | Load shown for 1/5/15 minutes, not a CPU percentage. |
| OS updates | Local update-notifier cache | May be absent or stale. No `apt update` or network check. Unknown does not mean current. |
| Reboot required | `/var/run/reboot-required` | Included in snapshot API; not a substitute for update review. |

Snapshot collection is serialized and cached for 15 seconds; the overview refreshes every 30 seconds. Each command has a four-second timeout. Failures show Unknown/Unavailable. Manual refresh respects the cache to avoid excessive subprocesses. With JavaScript disabled, Refresh status reloads the page normally.

## Security boundary

- The application refuses to start as root on Unix. No `sudo`, package installation, shell interpolation, SQL, arbitrary executable endpoint, configurable document-root browsing or writes to server configuration.
- Fixed absolute executables and validated service names. Probes receive no request-controlled arguments, credentials or inherited application environment.
- Both loopback remote addresses and trusted localhost `Host` headers are enforced. Forwarded headers are not trusted. Cross-site Fetch Metadata requests are rejected.
- Restrictive CSP (no inline code/eval), same-origin resource policy, frame blocking, no CORS, no third-party runtime requests, no-store responses.
- `POST`, `PUT`, `PATCH` and `DELETE` are rejected. No login/session secrets are necessary for this tunnel-only read-only release.
- phpMyAdmin is **detected only**. It is not installed, configured, linked, proxied or exposed. Existing phpMyAdmin exposure, firewall state and certificate validity remain **unverified**.
- The Security page retains the last 100 read events **in this process**. Events omit URLs, query strings and credentials. Endpoint/status events also go to the service journal (`journalctl -u wordpress-manager`). Retention and off-server archival are operator responsibilities. This is not yet a durable deployment audit ledger.

See [SECURITY.md](SECURITY.md) and [the provisioning design](docs/PROVISIONING.md). Flask's [host validation guidance](https://flask.palletsprojects.com/en/stable/web-security/#host-header-validation) and [Gunicorn deployment guidance](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/) informed the service boundary.

## Test

```bash
.venv/bin/python -m pytest -q
```

Tests exercise all routes, CPU/memory calculations, missing software, timeouts, concurrent caching, service name injection, root rejection, non-loopback access, forged forwarded headers, untrusted hosts, cross-site requests, mutation blocking, security headers, escaping, audit bounds and local HTMX assets. CI runs on Ubuntu and Windows with Python 3.11/3.12. Tests use injected observations and never change system services.

## Project layout

```text
wordpress_manager/
  __init__.py       App factory, private access policy, read-only routes
  collector.py      Read-only observations, parsers, bounded command runner
  templates/       Server-rendered views and HTMX fragment
  static/          CSS, minimal error feedback, vendored HTMX
deploy/            Hardened, optional systemd unit
tests/             Unit and HTTP boundary tests
docs/              Architecture and incremental implementation plan
```

## Next increments

The first planning increment is implemented and tested. Preview proposals without executing anything:

```bash
.venv/bin/python -m wordpress_manager.planning setup
.venv/bin/python -m wordpress_manager.planning site clientone example.com
```

Every proposal contains `executable: false`, a deterministic plan digest, derived resource names and explicit remaining approval/verification gates. The site planner validates identifiers/domains and checks collisions against supplied inventory; it does not inspect the live filesystem or DNS. Plan digests identify content, not authorization. There is no executor, socket or apply flag in this release.

1. Verified read-only MVP on the actual VPS through an SSH tunnel.
2. Package installer: read-only, version-preserving plan; operator approval; separate restricted provisioning service; isolated integration tests before enabling writes.
3. Production site creation: per-site user/FPM/database/Nginx isolation, verified WordPress download, explicit credentials delivery outside logs/source control, failure-safe provisioning journal.
4. Optional protected staging, then file/database backups and verified restoration, then pull/push with separately approved database strategies.

The live installer and site provisioner are not wired into v0.1. Existing services and customer data need a verified inventory and explicit change approval first. No production system was rebuilt or modified during development.

Third-party browser dependency: HTMX 2.0.8, vendored with its [license](wordpress_manager/static/vendor/HTMX-LICENSE). Python dependency versions are bounded in `pyproject.toml`; lock an approved environment for your deployment cadence.
