# WordPress Manager

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python Flask, HTMX, server-rendered HTML/CSS, established Linux tools. Minimal dependencies.

## Users

The server operator manages independent client WordPress websites on a Hetzner VPS.

## Product Purpose

Provide a secure, lightweight self-hosted management panel. The first release is a read-only server dashboard; installer and isolated site provisioning follow in tested increments.

## Operating Context

Hetzner CX23 (4 GB RAM), Ubuntu 24.04 LTS, existing Nginx, PHP 8.5.12/FPM, MariaDB 10.11, WP-CLI 2.12.0, Flask 3.1.3. Access through an SSH tunnel to a localhost listener. The development workspace is Windows; its readings must never masquerade as readings from the VPS.

## Capabilities and Constraints

Detect software versions, service health, and resources. Navigation: Server Setup, Websites, Staging, Backups, Security. Production and staging require isolated Linux users, FPM pools, databases and Nginx configuration. Staging is optional and created only after production. Dashboard never runs as root. Privileged writes belong in a narrowly restricted separate service. Destructive operations require backup and confirmation; production databases must not be blindly overwritten. Management services stay private. No credentials in source control. User authorized creating a GitHub repository; privacy is an implementation assumption.

## Product Principles

- Show verified facts and explicitly unknown states.
- Keep production changes deliberate, reviewable, recoverable, and audited.
- Separate observation from privileged execution.
- Keep per-client data and processes isolated.

## Open Decisions

No VPS connection details have been supplied. Live deployment is outside the current local build. Brand direction is unconfirmed; a compact light operations console with dark navigation was proposed through the question tool. Website inventories are initially empty until explicitly registered; do not fabricate sites or infer ownership from arbitrary document roots.
