# WordPress Manager

A private WordPress server-management dashboard for **Ubuntu 24.04**. The goal is to manage multiple isolated websites from one local-first interface, with explicit approval for server changes and safe backup, restore and removal workflows.

**Status:** Work in progress. Native Ubuntu website provisioning, backup/restore and site deletion are not yet ready for production. Development and integration testing use disposable environments only; do not run unfinished site-management features on live servers.

## Local preview

For the lightweight dashboard preview with Docker Desktop:

```bash
docker compose -f compose.preview.yml up --build
```

Open **http://127.0.0.1:8090**. Stop with Ctrl+C and run:

```bash
docker compose -f compose.preview.yml down
```

This is an unprivileged UI preview, **not** a complete WordPress server.

## Development and tests

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m wordpress_manager
.venv/bin/python -m pytest -q
```

On Windows PowerShell, use `.\.venv\Scripts\python` instead of `.venv/bin/python`.

Ubuntu systemd and WordPress integration tests must run **only in disposable Ubuntu 24.04 environments**, never on live infrastructure.

## Architecture and safety

- Flask dashboard, bound to localhost; deployed access is via SSH tunnel.
- Shared validation, planning and approval logic in `wordpress_manager/site_operations.py`.
- Separate adapters for local Docker preview and native Ubuntu operations.
- Restricted server-side control; no arbitrary shell execution through the dashboard.
- Website data must remain independent of the dashboard installation and removal.

## Documentation

- [Website backends and development architecture](docs/WEBSITE_BACKENDS.md)
- [Contributor and safety rules](AGENTS.md)
- [Provisioning and security design](docs/PROVISIONING.md)
- [Production deployment documentation](docs/PRODUCTION_DEPLOYMENT.md)

Production deployment is **not enabled** as part of ongoing prototype development.
