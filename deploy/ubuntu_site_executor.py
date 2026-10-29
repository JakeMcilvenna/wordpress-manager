"""Native Ubuntu website executor *prototype* for an isolated systemd guest.

This root-only helper deliberately REFUSES every host without the exact
disposable CI marker. It is not callable by the Flask dashboard or the
production control socket. Its generated resources resemble a native VPS:
Linux site user, host Nginx vhost, isolated PHP-FPM pool, MariaDB user/database,
real WP-CLI installation, and an explicit root-owned ownership journal.

The production adapter MUST stay disabled until a separately reviewed
authenticated and allowlisted control protocol, backups, recovery and
production-specific listener/TLS policies have been implemented.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import subprocess
import sys
import tempfile

# The restricted Ubuntu executor uses *system Python*, intentionally without
# Flask. Loading this single stdlib-only shared contract by its reviewed file
# path avoids importing wordpress_manager.__init__ (which imports Flask).
# Reuse the ordinary package module when already imported by unit tests.
def _load_site_contract():
    module = sys.modules.get("wordpress_manager.site_operations")
    if module is not None:
        return module
    source = Path(__file__).resolve().parents[1] / "wordpress_manager" / "site_operations.py"
    spec = importlib.util.spec_from_file_location("_wm_native_site_contract", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Shared website contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves the declaring module.
    spec.loader.exec_module(module)
    return module

_site_contract = _load_site_contract()
WebsitePlan = _site_contract.WebsitePlan
WebsiteSpec = _site_contract.WebsiteSpec
WebsiteService = _site_contract.WebsiteService
WebsiteOperationUnavailable = _site_contract.WebsiteOperationUnavailable

DISPOSABLE_MARKER = Path("/run/wm-disposable-test")
DISPOSABLE_TEXT = "disposable Ubuntu deployment test\n"
ROOT = Path("/srv/wordpress-manager-sites")
STATE = Path("/var/lib/wordpress-manager-sites")
RECORDS = STATE / "records"
LOCK = STATE / ".lock"
NGINX_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled")
PHP_ROOT = Path("/etc/php")
CONFIRMATION = "CREATE NATIVE TEST SITE"
PORT = 18080
CORE_VERSION = "7.1.4"
ENV = {"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
       "HOME": "/nonexistent", "LANG": "C.UTF-8", "WP_CLI_CACHE_DIR": "/tmp"}
ID = re.compile(r"wm-[0-9a-f]{12}")


def disposable_only():
    """No path through this executor can write to a real VPS."""
    if os.geteuid() != 0:
        raise PermissionError("Native website test requires root in a disposable guest")
    if not DISPOSABLE_MARKER.is_file() or DISPOSABLE_MARKER.is_symlink():
        raise PermissionError("Native site provisioning is disabled outside the disposable Ubuntu test guest")
    if DISPOSABLE_MARKER.read_text() != DISPOSABLE_TEXT:
        raise PermissionError("Unexpected disposable test guest marker")
    if not Path("/.dockerenv").is_file():
        raise PermissionError("Native website executor requires a disposable container, not a VPS host")
    system = Path("/etc/os-release").read_text()
    if not re.search(r'(?m)^ID=ubuntu$', system) or not re.search(r'(?m)^VERSION_ID="?24\.04"?$', system):
        raise PermissionError("Only disposable Ubuntu 24.04 LTS guests are supported")


def site_id_for(domain):
    return "wm-" + hashlib.sha256(domain.encode("ascii")).hexdigest()[:12]


def owned_paths(site_id, php):
    if not ID.fullmatch(site_id) or php not in ("8.3", "8.4", "8.5"):
        raise ValueError("Unsupported native website identity")
    return {
        "site": ROOT / site_id,
        "nginx": NGINX_AVAILABLE / (site_id + ".conf"),
        "enabled": NGINX_ENABLED / (site_id + ".conf"),
        "fpm": PHP_ROOT / php / "fpm/pool.d" / (site_id + ".conf"),
        "record": RECORDS / (site_id + ".json"),
    }


def make_plan(spec):
    """Pure planning, not an authorization to run on production."""
    if not isinstance(spec, WebsiteSpec):
        raise ValueError("Invalid website specification")
    if spec.php not in ("8.3", "8.4", "8.5"):
        raise ValueError("PHP version is unsupported on Ubuntu")
    # Avoid accidentally installing public domains from a disposable guest.
    if not spec.domain.endswith(".test"):
        raise ValueError("Disposable native website domains must end in .test")
    site_id = site_id_for(spec.domain)
    paths = owned_paths(site_id, spec.php)
    user = "wms" + site_id[3:13]
    database = "wm_" + site_id[3:13]
    return WebsitePlan(
        spec=spec, site_id=site_id,
        preview_url=f"http://{spec.domain}:{PORT}/",
        backend="ubuntu-disposable",
        resources=(str(paths["site"]), str(paths["nginx"]),
                   str(paths["enabled"]), str(paths["fpm"]),
                   str(paths["record"]), "linux-user:" + user,
                   "mariadb-database:" + database, "mariadb-user:" + database),
        confirmation=CONFIRMATION,
    )


def _run(*args, input_text=None, timeout=120, safe_download_diagnostic=False):
    """Fixed argv; never a shell or untrusted executable.

    Only core download (which receives NO credentials) may include a bounded
    diagnostic in disposable CI. Password-bearing WP-CLI and MariaDB calls
    still reveal neither output nor command arguments on error.
    """
    try:
        completed = subprocess.run(
            list(args), check=True, text=True, input=input_text,
            capture_output=True, timeout=timeout, env=ENV,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = "Native guest command failed: " + str(args[0])
        if (safe_download_diagnostic and
                isinstance(exc, subprocess.CalledProcessError) and
                len(args) >= 6 and args[0] == "/usr/sbin/runuser" and
                args[4] == "/usr/local/bin/wp" and
                args[5:7] == ("core", "download")):
            # Whitelisted non-secret command only; bounded and single-line.
            safe = (exc.stderr or "")[-750:].replace("\\r", " ").replace("\\n", " ").strip()
            if safe:
                detail += ": " + safe
        raise RuntimeError(detail) from None


def _atomic_json(path, data):
    """Durable root-only record; no secrets or sensitive command output."""
    raw = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, tmp = tempfile.mkstemp(prefix=".owned-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _directory(path, mode):
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != 0:
            raise RuntimeError("Unexpected native site directory: operator review required")
        if path.stat().st_mode & 0o022:
            raise RuntimeError("Native site directory is writable by others")
    else:
        path.mkdir(mode=mode)
    path.chmod(mode)


def _preflight(plan):
    disposable_only()
    if not isinstance(plan, WebsitePlan) or plan != make_plan(plan.spec):
        raise ValueError("Native website plan mismatch")
    paths = owned_paths(plan.site_id, plan.spec.php)
    for path in paths.values():
        if os.path.lexists(path):
            raise ValueError("Website resources already exist; never overwrite or adopt them")
    user = "wms" + plan.site_id[3:13]
    try:
        pwd.getpwnam(user)
    except KeyError:
        pass
    else:
        raise ValueError("Site Linux account already exists")
    if not Path("/usr/local/bin/wp").is_file():
        raise ValueError("WP-CLI is not installed")
    for path in (Path("/usr/sbin/nginx"), Path("/usr/sbin/php-fpm" + plan.spec.php),
                 Path("/usr/bin/mariadb"), Path("/usr/sbin/useradd")):
        if not path.is_file():
            raise ValueError("Required Ubuntu website-stack component missing")
    if _run("/usr/bin/mariadb", "--batch", "--skip-column-names",
            input_text=f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='wm_{plan.site_id[3:13]}';\n"):
        raise ValueError("Site database name already exists")
    if _run("/usr/bin/mariadb", "--batch", "--skip-column-names",
            input_text=f"SELECT User FROM mysql.user WHERE User='wm_{plan.site_id[3:13]}';\n"):
        raise ValueError("Site database user already exists")
    # Do not silently override existing Nginx domain ownership.
    for root in (NGINX_AVAILABLE, Path("/etc/nginx/conf.d")):
        if root.is_dir():
            for path in root.iterdir():
                if path.is_file() and not path.is_symlink() and path.suffix == ".conf":
                    if re.search(r"\bserver_name\s+" + re.escape(plan.spec.domain) + r"\s*;", path.read_text()):
                        raise ValueError("Domain is already configured in Nginx")
    return paths


def nginx_config(plan):
    site = owned_paths(plan.site_id, plan.spec.php)["site"]
    sock = f"/run/php/{plan.site_id}.sock"
    return f"""# wordpress-manager native disposable test site: {plan.site_id}
server {{
    listen 127.0.0.1:{PORT};
    server_name {plan.spec.domain};
    root {site};
    index index.php index.html;
    location / {{ try_files $uri $uri/ /index.php?$args; }}
    location ~ /\\. {{ deny all; }}
    location = /wp-config.php {{ deny all; }}
    location ~ \\.php$ {{
        try_files $uri =404;
        include /etc/nginx/fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        fastcgi_pass unix:{sock};
    }}
}}
"""


def fpm_config(plan):
    user = "wms" + plan.site_id[3:13]
    return f"""[{plan.site_id}]
user = {user}
group = {user}
listen = /run/php/{plan.site_id}.sock
listen.owner = www-data
listen.group = www-data
listen.mode = 0660
pm = ondemand
pm.max_children = 2
pm.process_idle_timeout = 10s
security.limit_extensions = .php
"""


def _write_exclusive(path, content, mode=0o644):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _wordpress_config(database, password):
    # Password and authentication salts live only in the owned site config.
    # Never put credentials in process arguments, ledger or logs.
    def escaped(value):
        return value.replace("\\", "\\\\").replace("'", "\\'")
    salts = "\n".join(
        f"define('{name}', '{secrets.token_hex(48)}');"
        for name in ("AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
                     "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT")
    )
    return ("<?php\n"
            f"define('DB_NAME', '{database}');\n"
            f"define('DB_USER', '{database}');\n"
            f"define('DB_PASSWORD', '{escaped(password)}');\n"
            "define('DB_HOST', 'localhost');\n"
            "define('DB_CHARSET', 'utf8mb4');\n"
            "define('DB_COLLATE', '');\n"
            f"{salts}\n"
            "$table_prefix = 'wp_';\n"
            "define('WP_DEBUG', false);\n"
            "if (!defined('ABSPATH')) define('ABSPATH', __DIR__ . '/');\n"
            "require_once ABSPATH . 'wp-settings.php';\n")


def create(plan, confirmation):
    """Execute only in a guarded disposable Ubuntu systemd test guest.

    All effects are journaled before they start. Failed partial operations
    remain marked needs-review: never delete potentially unmanaged resources.
    """
    if confirmation != CONFIRMATION:
        raise ValueError("Explicit native test-site confirmation is required")
    paths = _preflight(plan)
    _directory(ROOT, 0o755)
    _directory(STATE, 0o700)
    _directory(RECORDS, 0o700)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Revalidate after obtaining the lock to defeat competing requests.
        paths = _preflight(plan)
        record = {"site_id": plan.site_id, "domain": plan.spec.domain,
                  "php": plan.spec.php, "title": plan.spec.title,
                  "resources": list(plan.resources),
                  "state": "provisioning"}
        _atomic_json(paths["record"], record)
        try:
            user = "wms" + plan.site_id[3:13]
            database = "wm_" + plan.site_id[3:13]
            _run("/usr/sbin/useradd", "--system", "--user-group",
                 "--no-create-home", "--home-dir", str(paths["site"]),
                 "--shell", "/usr/sbin/nologin", user)
            identity = pwd.getpwnam(user)
            paths["site"].mkdir(mode=0o755)
            os.chown(paths["site"], identity.pw_uid, identity.pw_gid)
            paths["site"].chmod(0o755)
            db_password = secrets.token_hex(24)
            _run("/usr/bin/mariadb", "--batch", input_text=(
                f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
                f"CREATE USER '{database}'@'localhost' IDENTIFIED BY '{db_password}';\n"
                f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{database}'@'localhost';\n"
            ))
            _run("/usr/sbin/runuser", "-u", user, "--", "/usr/local/bin/wp",
                 "core", "download", f"--version={CORE_VERSION}", "--locale=en_US",
                 f"--path={paths['site']}", timeout=240, safe_download_diagnostic=True)
            config = paths["site"] / "wp-config.php"
            _write_exclusive(config, _wordpress_config(database, db_password), 0o640)
            os.chown(config, identity.pw_uid, identity.pw_gid)
            os.chmod(config, 0o640)
            _write_exclusive(paths["fpm"], fpm_config(plan))
            _write_exclusive(paths["nginx"], nginx_config(plan))
            os.symlink(paths["nginx"], paths["enabled"])
            _run("/usr/sbin/php-fpm" + plan.spec.php, "-t")
            _run("/usr/sbin/nginx", "-t")
            _run("/usr/bin/systemctl", "reload", "php" + plan.spec.php + "-fpm")
            _run("/usr/bin/systemctl", "reload", "nginx")
            admin_password = secrets.token_hex(20)
            _run("/usr/sbin/runuser", "-u", user, "--", "/usr/local/bin/wp",
                 "core", "install", f"--path={paths['site']}",
                 f"--url={plan.preview_url.rstrip('/')}",
                 f"--title={plan.spec.title}", "--admin_user=admin",
                 f"--admin_email={plan.spec.email}", "--skip-email",
                 "--prompt=admin_password", "--quiet",
                 input_text=admin_password + "\n", timeout=240)
            record["state"] = "created"
            _atomic_json(paths["record"], record)
            return {"installed": True, "site_id": plan.site_id, "domain": plan.spec.domain,
                    "url": plan.preview_url, "username": "admin",
                    "password": admin_password}
        except BaseException:
            record["state"] = "needs-review"
            _atomic_json(paths["record"], record)
            raise
    finally:
        os.close(fd)


def read_record(site_id):
    disposable_only()
    if not ID.fullmatch(site_id):
        raise ValueError("Invalid site ID")
    record = RECORDS / (site_id + ".json")
    if not record.is_file() or record.is_symlink():
        return None
    value = json.loads(record.read_text())
    if value.get("site_id") != site_id:
        raise ValueError("Site ledger identity mismatch")
    return value


def delete(*args):
    raise NotImplementedError("Native site deletion requires backup, ownership and recovery tests")


def suspend(*args):
    raise NotImplementedError("Native site suspension has not been implemented")


class DisposableUbuntuWebsiteBackend:
    """Implements the shared contract ONLY within a disposable native Ubuntu guest."""

    def inspect(self):
        disposable_only()
        records = self.list_sites()
        return {"installed": bool(records), "sites": len(records)}

    def list_sites(self):
        disposable_only()
        if not RECORDS.is_dir():
            return []
        sites = []
        for record in sorted(RECORDS.glob("wm-*.json")):
            if record.is_symlink():
                raise ValueError("Unsafe site ledger symlink")
            info = read_record(record.stem)
            if info and info.get("state") == "created":
                sites.append({"id": info["site_id"], "domain": info["domain"],
                              "name": info["title"], "state": "healthy",
                              "url": f"http://{info['domain']}:{PORT}/"})
        return sites

    def plan(self, spec):
        disposable_only()
        return make_plan(spec)

    def create(self, plan):
        return create(plan, CONFIRMATION)

    def suspend(self, site_id):
        raise WebsiteOperationUnavailable("Native site suspension is disabled")

    def delete(self, site_id, confirmation):
        raise WebsiteOperationUnavailable("Native site deletion is disabled")
