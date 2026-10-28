#!/usr/bin/python3
"""Root-only deployment CLI. No application imports, remote execution or web API.

Paths and executables are deliberately fixed; tests replace them in-process only.
All deployment mutations are serialized and journaled before activation.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import uuid
import secrets

BASE = Path("/opt/wordpress-manager")
UNIT = Path("/etc/systemd/system/wordpress-manager.service")
LOCK = Path("/run/wordpress-manager-deploy.lock")
SOURCE = Path(__file__).resolve().parent.parent
PROC = Path("/proc")
SERVICE = "wordpress-manager.service"
ACCOUNT = "wm-dashboard"
CONTROL = Path("/var/lib/wordpress-manager-control")
WEB_CONFIG = Path("/etc/wordpress-manager")
PROVISION_UNIT = Path("/etc/systemd/system/wordpress-manager-provisioner.service")
CONTROL_SOCKET = Path('/run/wordpress-manager/control.sock')
LOG = Path("/var/log/wordpress-manager-deploy.log")
SCHEMA = 1
ROOT_UID = 0
ROOT_GID = 0
PATH_ROOT = Path('/')
SITE_CONFIG_ROOTS = tuple(Path(p) for p in ('/etc/nginx', '/etc/php', '/etc/cron.d',
    '/etc/crontab', '/var/spool/cron/crontabs', '/etc/systemd/system'))
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent",
       "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PIP_CONFIG_FILE": "/dev/null",
       "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
RELEASE_ID = re.compile(r"[0-9][A-Za-z0-9_.+-]{0,100}")
# An intentional service policy change must update this digest after review.
UNIT_SHA256 = "101e3e000ca3f370b299483cb3c1e0fc16c56c32fd5581976faac4ebbca01465"
PROVISION_SHA256 = "ef0fba2729dd3089bb7004b807cbd895bbcf3f2299d3f2cace74ac02b4a6cf41"


class DeploymentError(RuntimeError):
    pass


def fail(message):
    raise DeploymentError(message)


def run(*args, timeout=120, **kwargs):
    print("Running: " + " ".join(str(a) for a in args))
    return subprocess.run([str(a) for a in args], check=True, env=ENV,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, **kwargs).stdout.strip()


def systemctl(*args):
    return run("/usr/bin/systemctl", *args, SERVICE)


def exists(path):
    return path.exists() or path.is_symlink()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trusted(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022:
        fail(f"Unsafe ownership, permissions or file type: {path}")


def platform_check(removing=False):
    if os.geteuid() != 0:
        fail("Run this deployment command with sudo (the web application never runs as root).")
    values = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                  if "=" in line)
    if values.get("ID", "").strip('"') != "ubuntu" or values.get("VERSION_ID", "").strip('"') != "24.04":
        fail("Only Ubuntu 24.04 is supported.")
    if not Path("/run/systemd/system").is_dir():
        fail("A running systemd system is required.")
    if platform.machine() != "x86_64":
        fail("Only Ubuntu 24.04 amd64 has been integration tested and is supported.")
    memory = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
    if not removing and (memory < 768 * 1024 or shutil.disk_usage(BASE.parent).free < 2 * 1024**3):
        fail("At least 768 MiB RAM and 2 GiB free space on /opt are required (2 GiB RAM recommended for stack provisioning).")
    for path in (BASE.parent, UNIT.parent, LOCK.parent):
        for ancestor in reversed((path, *path.parents)):
            trusted(ancestor, directory=True)


@contextmanager
def deployment_lock(read_only=False):
    import fcntl
    if read_only and not exists(LOCK):
        fail('No deployment lock exists; cannot safely preview concurrently with installation. Nothing changed.')
    fd = os.open(LOCK, (os.O_RDONLY if read_only else os.O_CREAT | os.O_RDWR) | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o077:
            fail("Unsafe deployment lock.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another deployment is running.")
        yield
    finally:
        os.close(fd)  # Keep the inode: unlinking a lock can allow concurrent owners.


def atomic_bytes(path, content, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".wm-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, data):
    atomic_bytes(path, (json.dumps(data, sort_keys=True, indent=2) + "\n").encode())


def read_json(path):
    trusted(path)
    return json.loads(path.read_text())


def release_path(release):
    if not isinstance(release, str) or not RELEASE_ID.fullmatch(release):
        fail("Invalid release identifier.")
    path = BASE / "releases" / release
    trusted(BASE / "releases", directory=True)
    trusted(path, directory=True)
    return path


def managed():
    trusted(BASE, directory=True)
    marker = read_json(BASE / ".managed.json")
    if marker.get("schema") != SCHEMA or marker.get("application") != SERVICE:
        fail("Unrecognized installation; refusing to modify it.")
    return marker


def read_state():
    managed()
    if exists(BASE / "transaction.json"):
        fail("Unfinished deployment. Run upgrade.sh --recover --approve-upgrade first.")
    state = read_json(BASE / "state.json")
    if state.get("schema") != SCHEMA:
        fail("Unsupported installation schema.")
    current = release_path(state["current"])
    if not (BASE / "current").is_symlink() or (BASE / "current").readlink() != Path("releases") / current.name:
        fail("Current release link does not match state; inspect before recovery.")
    validate_unit()
    validate_account(managed()["uid"])
    return state


def validate_unit():
    trusted(UNIT)
    if digest(UNIT) != UNIT_SHA256:
        fail("Installed systemd unit differs from the reviewed security policy.")
    dropins = systemctl("show", "--property=DropInPaths", "--value")
    fragment = systemctl("show", "--property=FragmentPath", "--value")
    if dropins or fragment != str(UNIT):
        fail("Unexpected systemd drop-ins or service fragment; review them manually.")


def account_record():
    import pwd
    try:
        return pwd.getpwnam(ACCOUNT)
    except KeyError:
        return None


def validate_account(expected_uid):
    import grp
    record = account_record()
    if record is None or not isinstance(expected_uid, int) or record.pw_uid != expected_uid:
        fail("Dashboard account identity changed; refusing to proceed.")
    group = grp.getgrnam(ACCOUNT)
    shadow = run("/usr/bin/getent", "shadow", ACCOUNT).split(":")
    if not (0 < record.pw_uid < 1000 and record.pw_gid == group.gr_gid
            and record.pw_dir == "/nonexistent" and record.pw_shell == "/usr/sbin/nologin"
            and set(os.getgrouplist(ACCOUNT, record.pw_gid)) == {record.pw_gid}
            and not group.gr_mem and len(shadow) > 1 and shadow[1].startswith(("!", "*"))):
        fail("Dashboard account must be a locked system user with no supplementary groups.")


def source_snapshot():
    """Read only approved source files; never copy a venv, .git, secrets or symlinks."""
    files = {}
    fixed = ["pyproject.toml", "README.md", "deploy/manage.py", "deploy/install.sh",
             "deploy/upgrade.sh", "deploy/uninstall.sh", "deploy/requirements.lock",
             "deploy/wordpress-manager.service", "deploy/provision.py", "deploy/release.py",
             "deploy/wordpress-manager-provisioner.service", "deploy/cloud-init.yaml"]
    package = SOURCE / "wordpress_manager"
    if package.is_symlink() or not package.is_dir() or (SOURCE / "deploy").is_symlink():
        fail("Source package and deploy directories must be real directories.")
    paths = [SOURCE / name for name in fixed]
    for directory, dirs, names in os.walk(package, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                fail("Symlink in source package.")
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        paths.extend(Path(directory) / name for name in names if not name.endswith((".pyc", ".pyo")))
    for path in paths:
        if not stat.S_ISREG(path.lstat().st_mode):
            fail(f"Source must contain regular files only: {path}")
        files[path.relative_to(SOURCE).as_posix()] = path.read_bytes()
    if hashlib.sha256(files["deploy/wordpress-manager.service"]).hexdigest() != UNIT_SHA256:
        fail("Source unit differs from the reviewed security policy.")
    if hashlib.sha256(files["deploy/wordpress-manager-provisioner.service"]).hexdigest() != PROVISION_SHA256:
        fail("Source provisioner differs from the reviewed security policy.")
    project = tomllib.loads(files["pyproject.toml"].decode())["project"]
    version = project["version"]
    if project["name"] != "wordpress-manager" or not RELEASE_ID.fullmatch(version):
        fail("Invalid project name or version.")
    if "wordpress_manager/__init__.py" not in files:
        fail("Application package is missing.")
    return version, files


def bootstrap():
    # No repository changes, distro upgrade, recommends or unrelated package list.
    run("/usr/bin/apt-get", "update", timeout=600)
    run("/usr/bin/apt-get", "install", "--yes", "--no-install-recommends", "--no-upgrade",
        "python3", "python3-venv", "ca-certificates", "iproute2", timeout=600)


def configure_control(marker, files, enabled):
    marker = managed()
    import grp
    gid = grp.getgrnam(ACCOUNT).gr_gid
    # Record ownership before each creation. Existing paths were rejected before any mutation.
    marker["control"] = True
    write_json(BASE / ".managed.json", marker)
    CONTROL.mkdir(mode=0o700)
    WEB_CONFIG.mkdir(mode=0o750)
    marker['directories'] = {str(p): identity(p) for p in (CONTROL, WEB_CONFIG)}
    write_json(BASE / '.managed.json', marker)
    os.chown(WEB_CONFIG, 0, gid)
    password, salt = secrets.token_urlsafe(24), secrets.token_hex(16)
    write_json(CONTROL / "credentials.json", {"salt": salt, "hash": hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 600000).hex()})
    atomic_bytes(CONTROL / "initial-password", (password + "\n").encode())
    write_json(WEB_CONFIG / "web.json", {"secret_key": secrets.token_hex(32), "provisioning": enabled})
    os.chown(WEB_CONFIG / "web.json", 0, gid)
    (WEB_CONFIG / "web.json").chmod(0o640)
    atomic_bytes(PROVISION_UNIT, files["deploy/wordpress-manager-provisioner.service"], 0o644)


def control_service(action):
    marker = managed()
    if marker.get("control") and exists(PROVISION_UNIT):
        trusted(PROVISION_UNIT)
        if digest(PROVISION_UNIT) != PROVISION_SHA256:
            fail("Provisioner unit differs from the reviewed security policy")
        if action != "disable":
            if run("/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", PROVISION_UNIT.name):
                fail("Unexpected provisioner drop-ins")
            if run('/usr/bin/systemctl', 'show', '--property=FragmentPath', '--value', PROVISION_UNIT.name) != str(PROVISION_UNIT):
                fail('Unexpected provisioner service fragment')
        run("/usr/bin/systemctl", action, *(('--now',) if action in ('enable', 'disable') else ()), PROVISION_UNIT.name)


def cleanup_control(marker):
    if not marker.get("control"):
        return
    control_service("disable")
    if exists(PROVISION_UNIT):
        PROVISION_UNIT.unlink()
        run('/usr/bin/systemctl', 'daemon-reload')
    for path in (WEB_CONFIG, CONTROL):
        if exists(path):
            trusted(path, directory=True)
            shutil.rmtree(path)


def build_release(version, files):
    name = f"{version}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    path = BASE / "releases" / name
    path.mkdir(mode=0o755)
    marker = managed()
    marker.setdefault('releases', {})[name] = {'directory': identity(path), 'files': None}
    write_json(BASE / '.managed.json', marker)
    # Build at its final path: venv shebangs must never be moved afterwards.
    for relative, content in files.items():
        destination = path / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        destination.write_bytes(content)
        destination.chmod(0o755 if destination.suffix == '.sh' else 0o644)
    run("/usr/bin/python3", "-I", "-m", "venv", path / ".venv", timeout=180)
    python = path / ".venv/bin/python"
    run(python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
        "--no-cache-dir", "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
        "--require-hashes", "--no-deps", "-r", path / "deploy/requirements.lock", timeout=600)
    run(python, "-I", "-m", "pip", "check")
    site = Path(run(python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"))
    if not site.is_relative_to(path) or site.is_symlink():
        fail("Unexpected virtual environment package path.")
    shutil.copytree(path / "wordpress_manager", site / "wordpress_manager")
    # Remove group/other writes from every regular file and directory, keep venv executables.
    for directory, dirs, names in os.walk(path):
        Path(directory).chmod(0o755)
        for entry in names:
            item = Path(directory) / entry
            if not item.is_symlink():
                item.chmod(0o755 if item.stat().st_mode & 0o111 else 0o644)
    manifest = {"schema": SCHEMA, "version": version,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "packages": run(python, "-I", "-m", "pip", "freeze", "--all").splitlines()}
    if (SOURCE / 'release-manifest.json').is_file():
        commit = json.loads((SOURCE / 'release-manifest.json').read_text()).get('git_commit')
        if commit is not None:
            if not re.fullmatch(r'[a-f0-9]{40}', commit):
                fail('Invalid release Git commit')
            manifest['git_commit'] = commit
    write_json(path / "release.json", manifest)
    marker = managed()
    marker['releases'][name]['files'] = tree_inventory(path)
    write_json(BASE / '.managed.json', marker)
    return name


def as_dashboard(*args, **kwargs):
    return run("/usr/sbin/runuser", "--user", ACCOUNT, "--", *args, **kwargs)


def validate_release(name):
    path = release_path(name)
    manifest = read_json(path / "release.json")
    if manifest.get("schema") != SCHEMA:
        fail("Unsupported release manifest.")
    for relative, expected in manifest["files"].items():
        item = path / relative
        if not item.is_relative_to(path) or ".." in Path(relative).parts:
            fail("Invalid manifest path.")
        trusted(item)
        if digest(item) != expected:
            fail(f"Release checksum mismatch: {relative}")
    if digest(path / "deploy/wordpress-manager.service") != UNIT_SHA256:
        fail("Release systemd policy mismatch.")
    for directory, dirs, names in os.walk(path):
        trusted(Path(directory), directory=True)
        for entry in (*dirs, *names):
            item = Path(directory) / entry
            if item.is_symlink():
                # Only the standard venv interpreter/lib64 links are permitted.
                if item not in [path / ".venv" / p for p in ("bin/python", "bin/python3", "bin/python3.12", "lib64")]:
                    fail(f"Unexpected release symlink: {item}")
                if not item.resolve().is_relative_to(path):
                    if item.resolve() != Path("/usr/bin/python3").resolve():
                        fail("Unexpected interpreter symlink.")
            elif item.is_dir():
                trusted(item, directory=True)
            else:
                trusted(item)
    as_dashboard(path / ".venv/bin/python", "-I", "-m", "pip", "check", cwd=path)
    as_dashboard(path / ".venv/bin/gunicorn", "--config", "/dev/null", "--check-config",
                 "--bind", "127.0.0.1:8080", "--no-control-socket", "wordpress_manager:create_app()", cwd=path)
    with tempfile.TemporaryDirectory(prefix="wm-verify-", dir=LOCK.parent) as temporary:
        unit = Path(temporary) / SERVICE
        unit.write_text((path / "deploy" / SERVICE).read_text().replace(str(BASE / "current"), str(path)))
        run("/usr/bin/systemd-analyze", "verify", unit)


def switch_current(name):
    release_path(name)
    temporary = BASE / ".current-next"
    if exists(temporary):
        if not temporary.is_symlink():
            fail("Unexpected activation temporary file.")
        temporary.unlink()
    temporary.symlink_to(Path("releases") / name)
    os.replace(temporary, BASE / "current")
    sync_directory(BASE)


def port_available():
    if run("/usr/bin/ss", "-H", "-ltn", "( sport = :8080 )"):
        fail("TCP port 8080 is already in use; no existing listener will be stopped.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Match Gunicorn's bind semantics: closed connections in TIME_WAIT are
        # harmless, while an existing listening socket still prevents binding.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 8080))


def health_once(name):
    if systemctl("is-active") != "active":
        fail("Service is not active.")
    pid = int(systemctl("show", "--property=MainPID", "--value"))
    if pid <= 1 or (PROC / str(pid) / "cwd").resolve() != release_path(name):
        fail("Service is not running the selected release.")
    uid_line = next(line for line in (PROC / str(pid) / "status").read_text().splitlines() if line.startswith("Uid:"))
    if {int(uid) for uid in uid_line.split()[1:]} != {managed()["uid"]}:
        fail("Service is not running as the restricted dashboard account.")
    if managed().get('control'):
        if run('/usr/bin/systemctl', 'is-active', PROVISION_UNIT.name) != 'active':
            fail('Control service is not active')
        info = CONTROL_SOCKET.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0
                or info.st_gid != account_record().pw_gid or stat.S_IMODE(info.st_mode) != 0o660):
            fail('Control socket must be root-owned, dashboard-group-only and mode 0660')
    listeners = run("/usr/bin/ss", "-H", "-ltnp", "( sport = :8080 )").splitlines()
    if not listeners or any(line.split()[3] != "127.0.0.1:8080" or f"pid={pid}," not in line for line in listeners):
        fail("Listener must belong to this service and bind exclusively to 127.0.0.1:8080.")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8080/healthz", timeout=2) as response:
        if response.status != 200 or json.loads(response.read(1024)) != {"status": "ok", "mode": "private"}:
            fail("Application health check failed.")


def wait_healthy(name):
    deadline = time.monotonic() + 30
    stable = 0
    error = "not started"
    while time.monotonic() < deadline:
        try:
            health_once(name)
            stable += 1
            if stable == 3:
                return
        except (DeploymentError, OSError, ValueError, subprocess.SubprocessError) as exc:
            error, stable = str(exc), 0
        time.sleep(1)
    fail(f"Service did not become healthy: {error}")


def remove_account(marker):
    if marker.get("uid") is None and account_record() is not None:
        fail("An unrecorded dashboard account exists; inspect it manually. Recovery journal retained.")
    if marker.get("uid") is not None and account_record() is not None:
        validate_account(marker["uid"])
        # Never --remove: no homes, site files, mail or shared data are deleted.
        run("/usr/sbin/userdel", ACCOUNT)


def cleanup_install():
    marker = managed()
    cleanup_control(marker)
    if exists(UNIT):
        trusted(UNIT)
        if digest(UNIT) != UNIT_SHA256:
            fail("Refusing cleanup of an unexpected unit; installation journal retained.")
        systemctl("disable", "--now")
        UNIT.unlink()
        sync_directory(UNIT.parent)
        run("/usr/bin/systemctl", "daemon-reload")
    remove_account(marker)
    shutil.rmtree(BASE)
    sync_directory(BASE.parent)


def install(enabled=False):
    if exists(BASE) and exists(BASE / ".managed.json"):
        state = read_state()
        version, files = source_snapshot()
        manifest = read_json(release_path(state['current']) / 'release.json')
        if manifest['files'] == {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}:
            validate_release(state['current'])
            wait_healthy(state['current'])
            print("Existing installation is healthy and unchanged. Configuration preserved.")
            return
        fail("Existing installation uses different source. Use an explicitly approved upgrade.")
    if (exists(BASE) or exists(UNIT) or account_record() is not None
            or any(exists(p) for p in (CONTROL, WEB_CONFIG, PROVISION_UNIT))):
        fail("Existing installation/account detected. Nothing overwritten; use an explicitly approved upgrade.")
    if systemctl("show", "--property=FragmentPath", "--value") or systemctl("show", "--property=DropInPaths", "--value"):
        fail("Existing systemd service detected; refusing installation.")
    if (run('/usr/bin/systemctl', 'show', '--property=FragmentPath', '--value', PROVISION_UNIT.name)
            or run('/usr/bin/systemctl', 'show', '--property=DropInPaths', '--value', PROVISION_UNIT.name)
            or exists(CONTROL_SOCKET.parent)):
        fail('Existing control service or runtime directory detected; refusing installation')
    version, files = source_snapshot()
    BASE.mkdir(mode=0o755)
    marker = {"schema": SCHEMA, "application": SERVICE, "uid": None,
              "ownership_version": 1, "directories": {}, "releases": {}}
    try:
        write_json(BASE / ".managed.json", marker)
        write_json(BASE / "transaction.json", {"kind": "install"})
        bootstrap()
        port_available()
        run("/usr/sbin/useradd", "--system", "--user-group", "--no-create-home", "--home-dir",
            "/nonexistent", "--shell", "/usr/sbin/nologin", "--password", "!", ACCOUNT)
        marker["uid"] = account_record().pw_uid
        write_json(BASE / ".managed.json", marker)
        validate_account(marker["uid"])
        (BASE / "releases").mkdir(mode=0o755)
        name = build_release(version, files)
        validate_release(name)
        switch_current(name)
        configure_control(marker, files, enabled)
        atomic_bytes(UNIT, files["deploy/wordpress-manager.service"], mode=0o644)
        run("/usr/bin/systemctl", "daemon-reload")
        validate_unit()
        control_service("enable")
        systemctl("enable", "--now")
        wait_healthy(name)
        write_json(BASE / "state.json", {"schema": SCHEMA, "current": name, "previous": None})
        (BASE / "transaction.json").unlink()
        sync_directory(BASE)
        print(f"Installed {name}.\nFrom your computer: ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8080:127.0.0.1:8080 ADMIN@SERVER\nBrowse http://127.0.0.1:8080.\nSign in as admin; retrieve the generated password with: sudo cat {CONTROL}/initial-password")
    except BaseException:
        if exists(BASE / ".managed.json"):
            cleanup_install()
        else:
            # This process just created the directory. Never recursively delete
            # an unmarked tree; rmdir succeeds only if it is still empty.
            BASE.rmdir()
        raise


def restore_transaction(transaction):
    old = transaction["old"]
    validate_unit()
    validate_account(managed()["uid"])
    validate_release(old["current"])
    switch_current(old["current"])
    write_json(BASE / "state.json", old)
    control_service("restart")
    systemctl("restart")
    wait_healthy(old["current"])
    (BASE / "transaction.json").unlink()
    sync_directory(BASE)
    print(f"Restored healthy release {old['current']}.")


def upgrade(rollback=None):
    state = read_state()
    if rollback is not None:
        target = state.get("previous") if rollback == "previous" else rollback
        if not target:
            fail("No previous release is recorded.")
        if target == state["current"]:
            fail("Requested release is already current.")
        validate_release(target)
    else:
        version, files = source_snapshot()
        target = build_release(version, files)
        validate_release(target)
    # No service interruption until validation completes. Keep old releases intact.
    transaction = {"kind": "upgrade", "old": state, "target": target}
    write_json(BASE / "transaction.json", transaction)
    try:
        switch_current(target)
        control_service("restart")
        systemctl("restart")
        wait_healthy(target)
        write_json(BASE / "state.json", {"schema": SCHEMA, "current": target, "previous": state["current"]})
        (BASE / "transaction.json").unlink()
        sync_directory(BASE)
        print(f"Activated healthy release {target}; previous: {state['current']}.")
    except BaseException:
        restore_transaction(transaction)
        raise


def recover():
    managed()
    transaction = read_json(BASE / "transaction.json")
    if transaction.get("kind") == "install":
        cleanup_install()
        print("Removed interrupted installation. You can run install.sh again.")
    elif transaction.get("kind") == "upgrade":
        restore_transaction(transaction)
    elif transaction.get("kind") == "uninstall":
        uninstall(purge_recovery=transaction.get('purge_recovery', False))
        print("Completed interrupted panel uninstall.")
    else:
        fail("Unknown transaction; inspect it manually.")


def identity(path):
    info = path.lstat()
    return {'device': info.st_dev, 'inode': info.st_ino}


def checked_directory(path, recorded):
    # Validate every ancestor before traversing: a replaced parent symlink must
    # never turn a fixed panel path into a website or another mounted filesystem.
    parents = []
    for parent in (path, *path.parents):
        parents.append(parent)
        if parent == PATH_ROOT:
            break
    for parent in reversed(parents):
        trusted(parent, directory=True)
    if identity(path) != recorded:
        fail(f'Resource identity changed; inspect manually: {path}')


def tree_inventory(root):
    """Inventory without following links or crossing mounts; includes venv links."""
    trusted(root, directory=True)
    device = root.lstat().st_dev
    mounted = set()
    mountinfo = Path('/proc/self/mountinfo')
    if mountinfo.exists():
        mounted = {re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4])
                   for line in mountinfo.read_text().splitlines()}
    result = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in (*dirs, *names):
            item = Path(directory) / name
            info = item.lstat()
            if str(item) in mounted or info.st_dev != device or os.path.ismount(item):
                fail(f'Mounted resource inside panel tree; refusing removal: {item}')
            if info.st_uid != ROOT_UID or (not item.is_symlink() and info.st_mode & 0o022):
                fail(f'Unsafe ownership or permissions: {item}')
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                fail(f'Unexpected resource type: {item}')
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                fail(f'Hard-linked resource; refusing removal: {item}')
            result[item.relative_to(root).as_posix()] = {
                **identity(item), 'type': stat.S_IFMT(info.st_mode)}
    return result


def removal_unit(path, owned):
    """Check both disk and systemd's loaded definition, including missing units."""
    expected = UNIT_SHA256 if path == UNIT else PROVISION_SHA256
    fragment = run('/usr/bin/systemctl', 'show', '--property=FragmentPath', '--value', path.name)
    dropins = run('/usr/bin/systemctl', 'show', '--property=DropInPaths', '--value', path.name)
    if dropins or (fragment and fragment != str(path)):
        fail(f'Unexpected service fragment or drop-ins: {path.name}')
    if exists(path):
        if not owned:
            fail(f'Unrecorded panel unit: {path}')
        trusted(path)
        if digest(path) != expected:
            fail(f'Unit differs from the reviewed security policy: {path}')
    elif fragment and not owned:
        fail(f'Unrecorded loaded service: {path.name}')
    for link in unit_enable_links(path):
        if not owned:
            fail(f'Unrecorded enablement link: {link}')
    # Sites must not have dependencies on either panel service. systemd also
    # reports the normal boot target, which is expected and safe to leave.
    if fragment:
        reverse = run('/usr/bin/systemctl', 'list-dependencies', '--reverse', '--all',
                      '--plain', '--no-legend', path.name)
        for name in reverse.split():
            if name.endswith(('.service', '.socket', '.timer', '.path')) and name not in (SERVICE, PROVISION_UNIT.name):
                fail(f'{name} depends on {path.name}; decouple it before uninstalling')


def unit_enable_links(path):
    links = []
    for directory in path.parent.iterdir():
        if directory.name.endswith(('.wants', '.requires')):
            link = directory / path.name
            if exists(link):
                trusted(directory, directory=True)
                if not link.is_symlink() or link.readlink() not in (path, Path('..') / path.name):
                    fail(f'Unexpected service enablement resource: {link}')
                links.append(link)
    return links


def removable_account(marker):
    """Retain a repurposed account; never delete site users, homes or their data."""
    record = account_record()
    if record is None:
        return False
    try:
        validate_account(marker.get('uid'))
    except (DeploymentError, OSError, subprocess.SubprocessError) as exc:
        print(f'RETAIN account {ACCOUNT}: {exc}')
        return False
    # Look for ownership outside panel resources, including separate data disks.
    # A failure to inspect any real filesystem conservatively retains the user.
    unsafe = []
    def scan_error(error):
        unsafe.append(str(error))
    excluded = {Path('/proc'), Path('/sys'), Path('/dev'), BASE, CONTROL, WEB_CONFIG,
                CONTROL_SOCKET.parent}
    for directory, dirs, files in os.walk('/', followlinks=False, onerror=scan_error):
        dirs[:] = [name for name in dirs if Path(directory) / name not in excluded]
        for name in (*dirs, *files):
            item = Path(directory) / name
            try:
                info = item.lstat()
                if info.st_uid == record.pw_uid or info.st_gid == getattr(record, 'pw_gid', -1):
                    print(f'RETAIN account {ACCOUNT}: owns external resource {item}')
                    return False
            except OSError as exc:
                unsafe.append(str(exc))
    if unsafe:
        print(f'RETAIN account {ACCOUNT}: external ownership scan was incomplete')
        return False
    return True


def check_site_dependencies():
    """Reject obvious operator-created dependencies; never rewrite site config."""
    needles = (str(BASE).encode(), SERVICE.encode(), PROVISION_UNIT.name.encode(), ACCOUNT.encode())
    def scan_error(error):
        fail(f'Cannot inspect site dependencies: {error}')
    for root in SITE_CONFIG_ROOTS:
        paths = [root] if root.is_file() else []
        if root.is_dir():
            for directory, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
                paths.extend(Path(directory) / name for name in (*dirs, *files))
        for path in paths:
            if path in (UNIT, PROVISION_UNIT) or (path.is_symlink() and path.resolve() in (UNIT, PROVISION_UNIT)):
                continue
            if path.is_symlink() and any(needle in str(path.resolve()).encode() for needle in needles):
                fail(f'Configuration link references panel resources: {path}')
            if path.is_file():
                with path.open('rb') as handle:
                    # Configs should be small. An oversized/unreadable file must
                    # be reviewed rather than silently omitting a dependency.
                    content = handle.read(2 * 1024 * 1024 + 1)
                if len(content) > 2 * 1024 * 1024:
                    fail(f'Configuration too large to inspect safely: {path}')
                if any(needle in content for needle in needles):
                    fail(f'Configuration references the panel; decouple before removal: {path}')


def removal_plan(purge_recovery=False):
    empty = False
    if exists(BASE):
        trusted(BASE, directory=True)
        empty = not any(BASE.iterdir())
    if empty:
        print(f'RETAIN empty unrecorded directory: {BASE} (possible final purge interruption)')
    marker = managed() if exists(BASE) and not empty else None
    if marker and marker.get('ownership_version') != 1:
        fail('Installation lacks a resource manifest. Preserve it and review/migrate ownership manually; nothing removed.')
    for path in (UNIT, PROVISION_UNIT):
        removal_unit(path, marker is not None and (path == UNIT or marker.get('control', False)))
    if marker is None:
        if account_record() is not None or any(exists(p) for p in (CONTROL, WEB_CONFIG, CONTROL_SOCKET.parent)):
            fail('Unrecorded panel resources remain; manual ownership review required.')
        return None
    check_site_dependencies()
    # Paths are fixed in code; never follow arbitrary paths from a manifest.
    allowed = {str(CONTROL), str(WEB_CONFIG)}
    if set(marker.get('directories', {})) - allowed:
        fail('Unknown directory in resource manifest')
    for path in (CONTROL, WEB_CONFIG):
        if exists(path):
            checked_directory(path, marker.get('directories', {}).get(str(path)))
            inventory = tree_inventory(path)
            allowed_files = ({'web.json'} if path == WEB_CONFIG else
                {'credentials.json', 'initial-password', 'ledger.sqlite',
                 'ledger.sqlite-wal', 'ledger.sqlite-shm', 'ledger.sqlite-journal'})
            if set(inventory) - allowed_files or any(
                    entry['type'] != stat.S_IFREG for entry in inventory.values()):
                fail(f'Unrecorded configuration resources; retain and inspect: {path}')
    releases = BASE / 'releases'
    if exists(releases):
        trusted(releases, directory=True)
        if set(p.name for p in releases.iterdir()) - set(marker['releases']):
            fail('Unrecorded release directory; nothing removed')
    for name, owned in marker['releases'].items():
        if not isinstance(name, str) or not RELEASE_ID.fullmatch(name):
            fail('Invalid release in ownership manifest')
        path = releases / name
        if exists(path):
            checked_directory(path, owned['directory'])
            actual = tree_inventory(path)
            if owned['files'] is None or any(owned['files'].get(n) != entry for n, entry in actual.items()):
                fail(f'Unrecorded or replaced files in release {name}; nothing removed')
    for path in (BASE / 'current', BASE / '.current-next'):
        if exists(path):
            if not path.is_symlink() or path.readlink().parts[:1] != ('releases',) or len(path.readlink().parts) != 2 or path.readlink().name not in marker['releases']:
                fail(f'Unexpected release pointer: {path}')
    known = {'.managed.json', 'state.json', 'transaction.json', 'uninstalled.json',
             'current', '.current-next', 'releases'}
    if {p.name for p in BASE.iterdir()} - known:
        fail('Unrecorded files in installation directory; nothing removed')
    for name in known - {'current', '.current-next', 'releases'}:
        if exists(BASE / name):
            trusted(BASE / name)
    if exists(CONTROL_SOCKET.parent):
        trusted(CONTROL_SOCKET.parent, directory=True)
        if not marker.get('control') or set(p.name for p in CONTROL_SOCKET.parent.iterdir()) - {CONTROL_SOCKET.name}:
            fail('Unrecorded resources in control runtime directory')
        if exists(CONTROL_SOCKET):
            info = CONTROL_SOCKET.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != ROOT_UID:
                fail('Unexpected provisioning socket ownership/type')
    print('REMOVE: panel services and units, provisioning socket, recorded code and virtual environments')
    for path in (UNIT, PROVISION_UNIT, CONTROL_SOCKET):
        print(f'  {path}')
    for name in marker['releases']:
        print(f'  {BASE / "releases" / name}')
    print(f'ACCOUNT: remove {ACCOUNT} only if its identity and external ownership checks pass after stopping services')
    print(('REMOVE explicitly approved recovery data: ' if purge_recovery else 'RETAIN recovery data: ') +
          ', '.join(str(p) for p in (BASE, CONTROL, WEB_CONFIG)))
    print(f'RETAIN logs: {LOG} and system journal')
    print('RETAIN all websites, staging, databases/users, site accounts/pools, Nginx site configs, SSL/renewals, backups/schedules, packages, WP-CLI and phpMyAdmin (including its independent units/port 8081)')
    return marker


def stop_removed_service(path):
    # Missing disk fragments are normal after an interrupted unlink. Loaded
    # services still need stopping. Do not ignore stop/disable failures.
    fragment = run('/usr/bin/systemctl', 'show', '--property=FragmentPath', '--value', path.name)
    if exists(path) or fragment:
        run('/usr/bin/systemctl', 'disable', '--now', path.name)
    elif run('/usr/bin/systemctl', 'show', '--property=ActiveState', '--value', path.name) in ('active', 'activating', 'deactivating', 'reloading'):
        run('/usr/bin/systemctl', 'stop', path.name)
    for link in unit_enable_links(path):
        link.unlink()
        sync_directory(link.parent)
    for prop in ('ActiveState', 'UnitFileState'):
        state = run('/usr/bin/systemctl', 'show', f'--property={prop}', '--value', path.name)
        if state in ('active', 'activating', 'deactivating', 'reloading', 'enabled', 'enabled-runtime', 'linked', 'linked-runtime'):
            fail(f'{path.name} remains {state}; journal retained')


def uninstall(dry_run=False, purge_recovery=False):
    marker = removal_plan(purge_recovery)
    if marker is None:
        print('WordPress Manager is already absent.')
        return
    if dry_run:
        print('Dry run complete; no changes made. Account deletion is conditional on the final ownership scan.')
        return
    previous = read_json(BASE / 'transaction.json') if exists(BASE / 'transaction.json') else None
    if previous and previous.get('kind') == 'uninstall':
        if bool(previous.get('purge_recovery')) != purge_recovery:
            fail('Resume with the same --purge-recovery setting recorded in transaction.json')
    else:
        if previous is None and exists(BASE / 'uninstalled.json'):
            previous = read_json(BASE / 'uninstalled.json')['transaction'].get('previous_transaction')
        write_json(BASE / 'transaction.json', {'kind': 'uninstall', 'purge_recovery': purge_recovery,
                                              'previous_transaction': previous})
    for path in (PROVISION_UNIT, UNIT):
        stop_removed_service(path)
    # Recheck the filesystem after both writers have stopped.
    removal_plan(purge_recovery)
    for path in (PROVISION_UNIT, UNIT):
        if exists(path):
            path.unlink()
            sync_directory(path.parent)
    run('/usr/bin/systemctl', 'daemon-reload')
    for path in (PROVISION_UNIT, UNIT):
        stop_removed_service(path)
    if exists(CONTROL_SOCKET):
        CONTROL_SOCKET.unlink()
    if exists(CONTROL_SOCKET.parent):
        CONTROL_SOCKET.parent.rmdir()
    for name in marker['releases']:
        path = BASE / 'releases' / name
        if exists(path):
            shutil.rmtree(path)  # Complete identity/inventory check above; no links followed.
    for name in ('current', '.current-next'):
        (BASE / name).unlink(missing_ok=True)
    if exists(BASE / 'releases'):
        (BASE / 'releases').rmdir()
    # Recovery files must not retain a deleted group's access or orphaned GIDs.
    for path in (WEB_CONFIG, CONTROL):
        if exists(path):
            for directory, dirs, files in os.walk(path, followlinks=False):
                for item in (Path(directory), *(Path(directory) / n for n in (*dirs, *files))):
                    if not item.is_symlink():
                        os.chown(item, ROOT_UID, ROOT_GID)
            path.chmod(0o700)
    if removable_account(marker):
        remove_account(marker)
    write_json(BASE / 'uninstalled.json', {'completed': datetime.now(timezone.utc).isoformat(),
                                         'transaction': read_json(BASE / 'transaction.json')})
    if purge_recovery:
        for path in (WEB_CONFIG, CONTROL):
            if exists(path):
                shutil.rmtree(path)
        if account_record() is not None:
            # Keep minimal non-secret ownership evidence for a retained account.
            # Otherwise a second purge could no longer distinguish it from an
            # unmanaged account. Recovery payloads are still explicitly purged.
            write_json(BASE / '.managed.json', {**marker, 'releases': {}, 'directories': {}})
            write_json(BASE / 'uninstalled.json', {'retained_account': ACCOUNT,
                'transaction': {'kind': 'uninstall', 'purge_recovery': True, 'previous_transaction': None}})
            for name in ('state.json', 'transaction.json'):
                (BASE / name).unlink(missing_ok=True)
            sync_directory(BASE)
            print(f'RETAIN minimal ownership manifest in {BASE} for account {ACCOUNT}; recovery payloads purged')
        else:
            for name in ('state.json', 'uninstalled.json', 'transaction.json', '.managed.json'):
                (BASE / name).unlink(missing_ok=True)
            BASE.rmdir()
    else:
        (BASE / 'transaction.json').unlink()
        sync_directory(BASE)
    print('Removed WordPress Manager. Websites, databases, SSL and shared services preserved.')


def main(argv=None):
    global SOURCE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "upgrade", "uninstall"))
    parser.add_argument("--approve-upgrade", action="store_true", help="Explicitly approve release activation/recovery")
    parser.add_argument('--release-source', type=Path,
                        help='Root-owned verified source for a trusted upgrade controller')
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--rollback", nargs="?", const="previous", metavar="RELEASE_ID")
    choice.add_argument("--recover", action="store_true", help="Recover an interrupted transaction")
    parser.add_argument("--yes", action="store_true", help="Confirm panel-only uninstall")
    parser.add_argument('--dry-run', action='store_true', help='Preview uninstall without changing files or services')
    parser.add_argument('--purge-recovery', action='store_true', help='Explicitly remove retained panel configuration, credentials, audit ledger and state; logs stay')
    parser.add_argument("--non-interactive", action="store_true", help="Explicit automation mode")
    parser.add_argument("--trusted-checkout", action="store_true", help="Trust this reviewed local checkout instead of a verified published release")
    parser.add_argument("--enable-provisioning", action="store_true", help="Allow authenticated, confirmed stack jobs")
    args = parser.parse_args(argv)
    if args.release_source:
        if args.operation != 'upgrade' or args.rollback or args.recover or args.trusted_checkout:
            parser.error('--release-source requires a verified forward upgrade')
        if os.geteuid() != 0 or not args.release_source.is_absolute():
            parser.error('--release-source requires root and an absolute path')
        for ancestor in reversed((args.release_source, *args.release_source.parents)):
            trusted(ancestor, directory=True)
        SOURCE = args.release_source
    if args.operation == "upgrade" and not args.approve_upgrade:
        parser.error("upgrade, rollback and recovery require --approve-upgrade")
    if args.operation != 'uninstall' and (args.dry_run or args.purge_recovery):
        parser.error('--dry-run and --purge-recovery are only for uninstall')
    if args.operation == 'uninstall' and not (args.yes or args.dry_run) and not sys.stdin.isatty():
        parser.error('Unattended uninstall requires explicit --yes; use --dry-run to preview')
    if args.operation != "upgrade" and (args.approve_upgrade or args.rollback is not None or args.recover):
        parser.error("upgrade options require the upgrade command")
    if args.operation != "uninstall" and args.yes:
        parser.error("--yes is only for uninstall")
    if args.enable_provisioning and args.operation != "install":
        parser.error("--enable-provisioning is only for install")
    if args.operation == "install" and not args.non_interactive:
        if not sys.stdin.isatty():
            parser.error("Unattended installation requires --non-interactive")
        if input("Install private WordPress Manager on this Ubuntu server? Type INSTALL: ") != "INSTALL":
            parser.error("Installation cancelled")
    os.umask(0o022)
    def interrupted(signum, frame):
        raise DeploymentError(f"Interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    original_streams = sys.stdout, sys.stderr
    fd = None
    try:
        if args.operation == 'uninstall':
            platform_check(removing=True)
        else:
            platform_check()
        if args.dry_run:
            # No log creation, lock creation, journal writes or service mutations.
            if exists(LOCK):
                with deployment_lock(read_only=True):
                    uninstall(dry_run=True, purge_recovery=args.purge_recovery)
            else:
                # An absent installation has no lock yet. Preview is read-only;
                # actual removal always locks and revalidates everything.
                uninstall(dry_run=True, purge_recovery=args.purge_recovery)
            return 0
        # Root-only durable command and failure log. Never log generated credentials.
        fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        if os.fstat(fd).st_uid != ROOT_UID or os.fstat(fd).st_mode & 0o077:
            os.close(fd)
            fd = None
            fail("Unsafe deployment log")
        class Tee:
            def __init__(self, stream):
                self.stream = stream
            def write(self, value):
                self.stream.write(value)
                os.write(fd, value.encode())
                os.fsync(fd)
            def flush(self):
                self.stream.flush()
        sys.stdout, sys.stderr = Tee(sys.stdout), Tee(sys.stderr)
        print(f"{datetime.now(timezone.utc).isoformat()} operation={args.operation}")
        if args.operation in ('install', 'upgrade') and not (args.rollback or args.recover):
            if (SOURCE / 'release-manifest.json').is_file():
                import importlib.util
                spec = importlib.util.spec_from_file_location('wm_release_verify', Path(__file__).with_name('release.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.verify_tree(SOURCE)
            elif not args.trusted_checkout:
                fail("Use a verified published release, or explicitly select --trusted-checkout for reviewed local source")
        with deployment_lock():
            if args.operation == "install":
                install(args.enable_provisioning)
            elif args.operation == "uninstall":
                if not args.yes:
                    removal_plan(args.purge_recovery)
                    phrase = 'REMOVE PANEL AND RECOVERY DATA' if args.purge_recovery else 'REMOVE PANEL'
                    if input(f'Type {phrase} to confirm: ') != phrase:
                        fail('Uninstall cancelled; no panel resources changed')
                uninstall(purge_recovery=args.purge_recovery)
            elif args.recover:
                recover()
            else:
                upgrade(args.rollback)
    except (DeploymentError, OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr, file=sys.stderr)
        print("Inspect the journal and retained transaction before retrying; see README recovery instructions.", file=sys.stderr)
        return 1
    finally:
        sys.stdout, sys.stderr = original_streams
        if fd is not None:
            os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
