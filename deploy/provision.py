#!/usr/bin/python3
"""Fixed stack operations over a peer-credential checked Unix socket. No shell API.

Run only from root-owned installed releases. Plans, one-use confirmations, login
throttling and jobs live in a synchronous SQLite ledger, outside web ownership.
"""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import struct
import sys
import subprocess
import threading
import time

# -I ignores PYTHONDONTWRITEBYTECODE. Keep helper imports from adding cache
# files to root-owned, inventoried releases, including when this module is used.
sys.dont_write_bytecode = True

STATE = Path("/var/lib/wordpress-manager-control")
STACK = Path("/var/lib/wordpress-manager-stack")
SOCKET = Path("/run/wordpress-manager/control.sock")
LOCK = Path("/run/wordpress-manager-deploy.lock")
CONFIG = Path("/etc/wordpress-manager/web.json")
UNITS = Path("/etc/systemd/system")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
       "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
ARTIFACTS = {
    "wpcli": ("https://github.com/wp-cli/wp-cli/releases/download/v2.12.0/wp-cli-2.12.0.phar",
              "ce34ddd838f7351d6759068d09793f26755463b4a4610a5a5c0a97b68220d85c"),
    "phpmyadmin": ("https://files.phpmyadmin.net/phpMyAdmin/5.2.3/phpMyAdmin-5.2.3-english.tar.xz",
                   "6bc0a920df4397b6b25a088428697ccb7514b8f19eb02def5f193dd8f8256f57"),
}
CONFIRM = "INSTALL MISSING COMPONENTS"


def command(*args):
    return subprocess.run(list(args), check=True, capture_output=True, text=True,
                          env=ENV, timeout=900).stdout.strip()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def db():
    connection = sqlite3.connect(STATE / "ledger.sqlite", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def audit(action, detail):
    # Only bounded, structured metadata; never passwords, capabilities or outputs.
    with db() as connection:
        connection.execute("INSERT INTO audit(time,action,detail) VALUES(?,?,?)",
                           (time.time(), action, canonical(detail)))


def initialize():
    STATE.mkdir(mode=0o700, exist_ok=True)
    info = STATE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("Unsafe control state directory")
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS audit(time REAL, action TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, expires REAL);
        CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, owner TEXT, expires REAL, body TEXT, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, state TEXT, detail TEXT);
        """)
        # A service restart never silently retries a partially executed package job.
        interrupted = connection.execute("SELECT id FROM jobs WHERE state='running'").fetchall()
        for row in interrupted:
            connection.execute("UPDATE jobs SET state='interrupted',detail=? WHERE id=?",
                               ("Inspect dpkg --audit and the journal; create a new plan after repair.", row["id"]))
    # Tables are inert outside the disposable native Ubuntu test guest.
    with db() as connection:
        _site_protocol().initialize(connection)
    os.chmod(STATE / "ledger.sqlite", 0o600)
    for row in interrupted:
        audit("interrupted", {"job": row["id"]})


@contextmanager
def lock():
    import fcntl
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Unsafe deployment lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def inventory():
    output = command("/usr/bin/dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Status}\n")
    packages = {}
    for line in output.splitlines():
        name, version, status = line.split("\t")
        if status == "installed":
            packages[name.split(":")[0]] = version
        elif status not in ("not-installed", "config-files"):
            raise ValueError("dpkg has unfinished packages; repair them before planning")
    configs = {}
    for root in ("/etc/nginx", "/etc/php", "/etc/mysql", "/etc/phpmyadmin"):
        for path in sorted(Path(root).rglob("*")):
            if path.is_symlink():
                configs[str(path)] = "link:" + str(path.readlink())
            elif path.is_file():
                configs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    probes = {p: os.path.lexists(p) for p in (
        "/usr/local/bin/wp", "/usr/bin/wp", "/usr/local/nginx", "/usr/local/sbin/nginx",
        "/usr/local/bin/php", "/usr/local/sbin/php-fpm", "/usr/local/mysql", "/opt/lampp",
        "/usr/share/phpmyadmin", str(STACK))}
    return {"packages": packages, "configs": configs, "paths": probes}


def apt_changes(requested, installed):
    # Official Ubuntu signed metadata only. No PPAs, key installation or insecure APT options.
    policy = command("/usr/bin/apt-cache", "policy")
    releases = [line.strip() for line in policy.splitlines() if line.strip().startswith("release ")]
    if not releases or any("o=Ubuntu," not in line and not line.endswith("o=Ubuntu")
                           for line in releases if "a=now" not in line):
        raise ValueError("Provisioning requires Ubuntu-origin APT repositories only; review third-party repositories manually")
    output = command("/usr/bin/apt-get", "--simulate", "--no-install-recommends", "install", *requested)
    changes = {}
    for line in output.splitlines():
        if line.startswith(("Remv ", "Conf ")):
            if line.startswith("Remv "):
                raise ValueError("APT would remove a package")
            continue
        if not line.startswith("Inst "):
            continue
        match = re.match(r"Inst ([a-z0-9+.:~-]+) (?:\[[^\]]+\] )?\(([^ ]+) ", line)
        if not match:
            raise ValueError("Unrecognized APT plan")
        name, version = match.groups()
        if name.split(":")[0] in installed:
            raise ValueError("APT would modify an installed package: " + name)
        if not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version):
            raise ValueError("Unrecognized package version")
        changes[name] = version
    return changes


def make_plan(php):
    if php not in ("8.3", "8.4", "8.5"):
        raise ValueError("Unsupported PHP version")
    baseline = inventory()
    installed, configs, paths = (baseline[k] for k in ("packages", "configs", "paths"))
    if php != "8.3" and any(f"php{php}-{suffix}" not in installed for suffix in
                             ("fpm", "cli", "mysql", "xml", "curl", "mbstring", "zip", "gd", "intl")):
        raise ValueError("Only Ubuntu PHP 8.3 can be installed. Other supported PHP versions must already have all required extensions")
    for component, package, prefix in (("Nginx", "nginx", "/etc/nginx/"),
                                        ("MariaDB", "mariadb-server", "/etc/mysql/")):
        if package not in installed and any(p.startswith(prefix) for p in configs):
            raise ValueError(component + " has unmanaged or partial configuration; manual review required")
    if "mariadb-server" not in installed and any(n.startswith(("mysql-server", "percona-server")) for n in installed):
        raise ValueError("Existing database server must be reviewed manually")
    if any(paths[p] for p in ("/usr/local/nginx", "/usr/local/sbin/nginx", "/usr/local/bin/php",
                              "/usr/local/sbin/php-fpm", "/usr/local/mysql", "/opt/lampp")):
        raise ValueError("Custom server installation detected; manual review required")
    if paths[str(STACK)] and not (STACK / 'ready.json').is_file():
        raise ValueError("Incomplete private phpMyAdmin installation; inspect the failed job before retrying")
    if f"php{php}-fpm" not in installed and any(p.startswith(f"/etc/php/{php}/fpm/") for p in configs):
        raise ValueError("Unmanaged PHP configuration; manual review required")
    packages = ["nginx", "mariadb-server", "certbot", "python3-certbot-nginx"]
    packages += [f"php{php}-{s}" for s in ("fpm", "cli", "mysql", "xml", "curl", "mbstring", "zip", "gd", "intl")]
    requested = [name for name in packages if name not in installed]
    changes = apt_changes(requested, installed) if requested else {}
    # Both the requested packages and ALL transitive dependencies are pinned in the confirmation.
    if any(name not in changes for name in requested):
        raise ValueError("APT did not resolve every missing component")
    artifacts = []
    if not (paths["/usr/local/bin/wp"] or paths["/usr/bin/wp"]):
        artifacts.append("wpcli")
    if not paths["/usr/share/phpmyadmin"] and not paths[str(STACK)]:
        artifacts.append("phpmyadmin")
    plan = {"php": php, "baseline": baseline, "packages": changes, "artifacts": artifacts,
            "preserved_php": sorted(n for n in installed if re.fullmatch(r"php[0-9]+\.[0-9]+-fpm", n)),
            "notice": "Existing software/configuration is preserved. No TLS issuance or website/database creation. Existing phpMyAdmin exposure remains unverified.",
            "artifact_pins": {name: ARTIFACTS[name] for name in artifacts}}
    return plan


def artifact(name):
    spec = importlib.util.spec_from_file_location("wm_release", Path(__file__).with_name("release.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    url, sha = ARTIFACTS[name]
    data = module.download(url)
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("Artifact integrity check failed")
    return data


def private_admin(data, php):
    """A separate Nginx instance and FPM pool; no files in /etc/nginx or /etc/php."""
    import io
    import tarfile
    names = ("wordpress-manager-dbadmin-php.service", "wordpress-manager-dbadmin.service")
    if os.path.lexists(STACK) or any(os.path.lexists(UNITS / n) for n in names):
        raise ValueError("Private management paths already exist; inspect the retained job")
    for name in names:
        if (command('/usr/bin/systemctl', 'show', '--property=FragmentPath', '--value', name)
                or command('/usr/bin/systemctl', 'show', '--property=DropInPaths', '--value', name)):
            raise ValueError('Existing private management unit or drop-in requires manual review')
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8081))
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
        members = archive.getmembers()
        total = sum(m.size for m in members)
        if total > 128 * 1024 * 1024 or len(members) > 10000:
            raise ValueError("phpMyAdmin archive too large")
        for member in members:
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts or not (member.isfile() or member.isdir()):
                raise ValueError("Unsafe phpMyAdmin archive")
        STACK.mkdir(mode=0o755)
        STACK.chmod(0o755)
        (STACK / "owned.json").write_text(canonical({"units": names, "php": php, "sha256": ARTIFACTS["phpmyadmin"][1]}))
        public = STACK / "public"
        public.mkdir()
        public.chmod(0o755)
        for member in members:
            relative = Path(*Path(member.name).parts[1:])
            if not relative.parts:
                continue
            path = public / relative
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o755)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
                path.chmod(0o644)
        for directory in public.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o755)
        # Cookie auth: no SQL credentials or control user. Root socket login is deliberately unavailable.
        (public / "config.inc.php").write_text("<?php\n$cfg['blowfish_secret'] = '" + secrets.token_hex(16) + "';\n"
            "$cfg['Servers'][1]['auth_type'] = 'cookie';\n$cfg['Servers'][1]['host'] = 'localhost';\n"
            "$cfg['Servers'][1]['AllowNoPassword'] = false;\n$cfg['Servers'][1]['AllowRoot'] = false;\n"
            "$cfg['TempDir'] = '/var/lib/wm-private-admin';\n")
        (public / "config.inc.php").chmod(0o640)
        import pwd
        os.chown(public / "config.inc.php", 0, pwd.getpwnam("www-data").pw_gid)
    fpm = f"""[global]
pid = /run/wordpress-manager-dbadmin/php.pid
error_log = /var/lib/wm-private-admin/fpm-error.log
daemonize = no
[management]
listen = /run/wordpress-manager-dbadmin/php.sock
listen.mode = 0600
pm = ondemand
pm.max_children = 3
pm.process_idle_timeout = 10s
php_admin_value[session.save_path] = /var/lib/wm-private-admin
php_admin_value[upload_tmp_dir] = /var/lib/wm-private-admin
security.limit_extensions = .php
"""
    nginx = fr"""pid /run/wordpress-manager-dbadmin-web/nginx.pid;
error_log stderr;
events {{ worker_connections 128; }}
http {{
include /etc/nginx/mime.types;
access_log off;
client_body_temp_path /var/lib/wm-private-admin/body;
fastcgi_temp_path /var/lib/wm-private-admin/fastcgi;
server {{
listen 127.0.0.1:8081;
server_name localhost;
root {public};
index index.php;
add_header X-Frame-Options DENY always;
location / {{ try_files $uri $uri/ =404; }}
location ~ ^/(?:setup|libraries|templates|vendor|test)/ {{ deny all; }}
location ~ /\. {{ deny all; }}
location ~ \.php$ {{
try_files $uri =404;
include /etc/nginx/fastcgi_params;
fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
fastcgi_pass unix:/run/wordpress-manager-dbadmin/php.sock;
}}
}}
}}
"""
    (STACK / "php.conf").write_text(fpm)
    (STACK / "nginx.conf").write_text(nginx)
    (STACK / "php.conf").chmod(0o644)
    (STACK / "nginx.conf").chmod(0o644)
    common = """[Service]
User=www-data
Group=www-data
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
CapabilityBoundingSet=
StateDirectory=wm-private-admin
StateDirectoryMode=0700
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
Restart=on-failure
"""
    configs = ("[Unit]\nDescription=Private phpMyAdmin PHP pool\n" + common +
               f"RuntimeDirectory=wordpress-manager-dbadmin\nExecStart=/usr/sbin/php-fpm{php} --fpm-config {STACK}/php.conf\n[Install]\nWantedBy=multi-user.target\n",
               "[Unit]\nDescription=Loopback-only phpMyAdmin\nAfter=wordpress-manager-dbadmin-php.service\nRequires=wordpress-manager-dbadmin-php.service\n" + common +
               f"RuntimeDirectory=wordpress-manager-dbadmin-web\nExecStart=/usr/sbin/nginx -c {STACK}/nginx.conf -g 'daemon off;'\n[Install]\nWantedBy=multi-user.target\n")
    for name, content in zip(names, configs):
        fd = os.open(UNITS / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
    command("/usr/bin/systemctl", "daemon-reload")
    for name in names:
        command("/usr/bin/systemctl", "enable", "--now", name)
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(20):
        try:
            with opener.open("http://127.0.0.1:8081/", timeout=2) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(1)
    else:
        raise ValueError("Private phpMyAdmin did not become healthy; inspect its units")
    listeners = command("/usr/bin/ss", "-H", "-ltn", "( sport = :8081 )").splitlines()
    if not listeners or any(line.split()[3] != "127.0.0.1:8081" for line in listeners):
        raise ValueError("Unsafe phpMyAdmin listener")
    (STACK / 'ready.json').write_text(canonical({'php': php, 'listener': '127.0.0.1:8081'}))


def execute(plan):
    with lock():
        if inventory() != plan["baseline"]:
            raise ValueError("Server changed since approval; create a new plan")
        pins = [f"{name}={version}" for name, version in plan["packages"].items()]
        if pins and apt_changes(pins, plan["baseline"]["packages"]) != plan["packages"]:
            raise ValueError("APT plan changed; create a new plan")
        # Verify all external bytes BEFORE the first package modification.
        downloads = {name: artifact(name) for name in plan["artifacts"]}
        if pins:
            # Refuse custom policy rather than replacing it, even temporarily.
            policy = Path("/usr/sbin/policy-rc.d")
            if os.path.lexists(policy):
                raise ValueError("Existing policy-rc.d requires operator review")
            fd = os.open(policy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o755)
            with os.fdopen(fd, "w") as handle:
                handle.write("#!/bin/sh\n# wordpress-manager temporary package service-start guard\nexit 101\n")
            try:
                command("/usr/bin/apt-get", "--yes", "--no-remove", "--no-install-recommends",
                        "-o", "APT::Get::AllowUnauthenticated=false",
                        "-o", "Acquire::AllowInsecureRepositories=false",
                        "-o", "Acquire::AllowDowngradeToInsecureRepositories=false",
                        "-o", "Dpkg::Options::=--force-confold", "install", *pins)
            finally:
                policy.unlink()
        # Verify every pre-existing package version and configuration was preserved.
        after = inventory()
        for key in ("packages", "configs"):
            if any(after[key].get(n) != v for n, v in plan["baseline"][key].items()):
                raise ValueError("Pre-existing inventory changed; inspect the job before proceeding")
        if "wpcli" in downloads:
            fd = os.open("/usr/local/bin/wp", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o755)
            with os.fdopen(fd, "wb") as handle:
                handle.write(downloads["wpcli"])
                handle.flush()
                os.fsync(handle.fileno())
                # The restricted service has UMask=0077. Explicitly grant
                # execute permission to dedicated unprivileged site users,
                # otherwise WP-CLI is created mode 0700 and WordPress cannot
                # be provisioned under a site's own Linux identity.
                os.fchmod(handle.fileno(), 0o755)
        for package, service in (("mariadb-server", "mariadb"), ("nginx", "nginx"),
                                  (f"php{plan['php']}-fpm", f"php{plan['php']}-fpm")):
            if package in plan["packages"]:
                command("/usr/bin/systemctl", "enable", "--now", service)
        if "phpmyadmin" in downloads:
            private_admin(downloads["phpmyadmin"], plan["php"])


def job_worker(job, plan):
    try:
        audit("job-start", {"job": job, "plan": digest(plan)})
        execute(plan)
        state, detail = "succeeded", "Missing components installed. phpMyAdmin, when managed here, uses 127.0.0.1:8081."
    except Exception as error:
        # Do not include subprocess outputs; package scripts could emit secrets.
        state = "failed"
        detail = str(error) if isinstance(error, ValueError) else "System operation failed; inspect the service journal and dpkg --audit."
        if 'phpmyadmin' in plan.get('artifacts', ()) and (STACK / 'owned.json').is_file():
            # Stop only newly created, explicitly recorded units after private-health failure.
            try:
                owned = json.loads((STACK / 'owned.json').read_text())
                names = ('wordpress-manager-dbadmin-php.service', 'wordpress-manager-dbadmin.service')
                if owned.get('units') == list(names):
                    command('/usr/bin/systemctl', 'disable', '--now', *reversed(names))
            except Exception:
                detail += ' Private management units need manual inspection.'
    audit("job-" + state, {"job": job})
    print(f"Provisioning job {job}: {state}; {detail}", flush=True)
    with db() as connection:
        connection.execute("UPDATE jobs SET state=?,detail=? WHERE id=?", (state, detail, job))


def _site_protocol():
    spec = importlib.util.spec_from_file_location(
        "_wm_disposable_site_control", Path(__file__).with_name("site_control.py")
    )
    if spec is None or spec.loader is None:
        raise ValueError("Disposable website control unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def authenticate(password):
    if not isinstance(password, str) or len(password) > 256:
        raise ValueError("Invalid credentials")
    with db() as connection:
        attempts = connection.execute("SELECT count(*) FROM audit WHERE action='login-failed' AND time>?", (time.time() - 60,)).fetchone()[0]
        if attempts >= 5:
            raise ValueError("Too many login attempts; wait one minute")
    secret = json.loads((STATE / "credentials.json").read_text())
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(secret["salt"]), 600000).hex()
    if not hmac.compare_digest(hashed, secret["hash"]):
        audit("login-failed", {})
        raise ValueError("Invalid credentials")
    cap = secrets.token_urlsafe(32)
    with db() as connection:
        connection.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        connection.execute("INSERT INTO sessions VALUES(?,?)", (digest(cap), time.time() + 1800))
    audit("login-success", {})
    return {"capability": cap}


def dispatch(message, root=False):
    if not isinstance(message, dict):
        raise ValueError("Invalid request")
    op = message.get("op")
    fields = {"login": {"password"}, "logout": {"capability"}, "status": {"capability"},
              "plan": {"capability", "php"}, "apply": {"capability", "plan", "confirmation"}}
    fields.update({
        "site-plan": {"capability", "title", "email", "domain", "php"},
        "site-apply": {"capability", "plan", "confirmation"},
        "site-status": {"capability"},
        "site-result": {"capability", "job"},
    })
    if op not in fields or set(message) - fields[op] - {"op"}:
        raise ValueError("Unsupported operation or arguments")
    if op == "login" and not root:
        return authenticate(message.get("password"))
    owner = "root-cli" if root else digest(message.get("capability", ""))
    with db() as connection:
        if not root and not connection.execute("SELECT 1 FROM sessions WHERE token=? AND expires>?", (owner, time.time())).fetchone():
            raise ValueError("Authentication expired; sign in again")
        if op == "logout":
            connection.execute("DELETE FROM sessions WHERE token=?", (owner,))
            return {}
        if op == "status":
            return {"jobs": [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 20")]}
    if op.startswith("site-"):
        # The native executor checks the disposable guest marker before any
        # plan, status, approval, or credential retrieval can proceed.
        return _site_protocol().dispatch(
            op, message, owner, db, audit, canonical, digest, threading
        )
    if not root and not json.loads(CONFIG.read_text()).get("provisioning"):
        raise ValueError("Provisioning is disabled by the administrator")
    if op == "plan":
        plan = make_plan(message.get("php"))
        identifier = secrets.token_hex(16)
        with db() as connection:
            connection.execute("INSERT INTO plans(id,owner,expires,body) VALUES(?,?,?,?)",
                               (identifier, owner, time.time() + 600, canonical(plan)))
        audit("plan-created", {"id": identifier, "digest": digest(plan), "actor": "operator" if not root else "root"})
        return {"id": identifier, "digest": digest(plan), **plan}
    if op == "apply":
        if message.get("confirmation") != CONFIRM:
            raise ValueError("Explicit job confirmation is required")
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM plans WHERE id=? AND owner=? AND expires>? AND used=0",
                                     (message.get("plan"), owner, time.time())).fetchone()
            if row is None:
                raise ValueError("Plan is expired, used or belongs to another session")
            if connection.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                raise ValueError("A provisioning job is already running")
            job = secrets.token_hex(16)
            connection.execute("UPDATE plans SET used=1 WHERE id=?", (row["id"],))
            connection.execute("INSERT INTO jobs VALUES(?, 'running', ?)", (job, "Approved plan " + row["id"]))
        audit("job-approved", {"job": job, "plan": row["id"], "actor": "operator" if not root else "root"})
        if root:
            job_worker(job, json.loads(row["body"]))
        else:
            threading.Thread(target=job_worker, args=(job, json.loads(row["body"])), daemon=True).start()
        return {"job": job}
    raise ValueError("Unsupported operation")


def serve():
    import pwd
    dashboard = pwd.getpwnam("wm-dashboard")
    expected = dashboard.pw_uid
    if SOCKET.exists():
        if not stat.S_ISSOCK(SOCKET.lstat().st_mode) or SOCKET.lstat().st_uid != 0:
            raise ValueError("Unexpected socket path")
        SOCKET.unlink()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(SOCKET))
        os.chown(SOCKET, 0, dashboard.pw_gid)
        os.chmod(SOCKET, 0o660)
        listener.listen(8)
        while True:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(5)
                try:
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != expected:
                        raise ValueError("Unauthorized Unix peer")
                    data = b""
                    while not data.endswith(b"\n") and len(data) <= 4096:
                        block = connection.recv(4097 - len(data))
                        if not block:
                            break
                        data += block
                    if len(data) > 4096:
                        raise ValueError("Request too large")
                    result = {"ok": True, "result": dispatch(json.loads(data))}
                except Exception as error:
                    audit("request-denied", {})
                    result = {"ok": False, "error": str(error) if isinstance(error, ValueError) else "Provisioner unavailable; inspect its journal"}
                try:
                    connection.sendall((canonical(result) + "\n").encode())
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "plan", "apply"))
    parser.add_argument("--php", choices=("8.3", "8.4", "8.5"), default="8.3")
    parser.add_argument("--plan")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--approve-server-changes", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Root required; the dashboard cannot run this CLI")
    # Shared packages/tools must not inherit the panel's dedicated group.
    # Only the explicitly chowned control socket needs dashboard group access.
    os.setgid(0)
    values = Path("/etc/os-release").read_text()
    if 'ID=ubuntu' not in values or 'VERSION_ID="24.04"' not in values:
        parser.error("Only Ubuntu 24.04 is supported")
    if args.command == "serve":
        initialize()
        serve()
    elif args.command == "plan":
        # Deliberately no APT update here: refreshing metadata is an explicit operator action.
        print(json.dumps(dispatch({"op": "plan", "php": args.php}, root=True), indent=2))
    else:
        if not args.non_interactive or not args.approve_server_changes or not args.plan:
            parser.error("apply requires --plan ID --non-interactive --approve-server-changes")
        result = dispatch({"op": "apply", "plan": args.plan, "confirmation": CONFIRM}, root=True)
        with db() as connection:
            job = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (result["job"],)).fetchone())
        print(json.dumps(job))
        return 0 if job["state"] == "succeeded" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
