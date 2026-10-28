# Security policy

This release is a read-only, tunnel-only console for a trusted operator. It must not be publicly exposed. It is not a replacement for host patching, independent backups, SSH controls or firewall review.

Report vulnerabilities privately to the repository owner through an available private channel. Do not put passwords, keys, database dumps, customer information or live server configuration in public issues. No external security contact or response-time commitment has been established.

Only the current release is maintained. Review dependency updates and run the test suite before deploying. Keep the service account without sudo or membership in database, site-owner or privileged administration groups. The account must not own the executable code in the production deployment.

## Current limitations

- Local processes and users with server shell access can reach loopback. There is no application authentication layer.
- Service state does not prove application health, tenant isolation or website integrity.
- Update information is cached and can be missing or stale. WordPress updates are not queried yet.
- The web process's bounded access log is volatile. The journal's retention policy controls durable read-event retention.
- No backup, restore, installer or deployment action exists in the MVP. No protection is claimed for unmanaged sites or pre-existing phpMyAdmin configuration.

Any future write-capable release must add authenticated sessions, CSRF protection, exact-operation confirmations and a durable privileged audit ledger before enabling mutations. SSH access alone does not discharge these requirements for a write-capable web app.
