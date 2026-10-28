"""Panel lifecycle test: ONLY for a fresh, disposable Ubuntu 24.04 guest.

Intentionally not collected by pytest. CI supplies the explicit guard file;
never create that guard on a real server or workstation.
"""
import argparse
import hashlib
import json
import os
import runpy
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

BASE = Path("/opt/wordpress-manager")
UNIT = Path("/etc/systemd/system/wordpress-manager.service")
REPO = Path(__file__).resolve().parents[1]


def command(*args, success=True):
    result = subprocess.run([str(a) for a in args], text=True, capture_output=True, timeout=900,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
    if (result.returncode == 0) != success:
        raise AssertionError(f"Unexpected exit {result.returncode}: {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout if success else result.stdout + result.stderr


def cli(source, name, *args, success=True):
    if name == 'install':
        args = ('--non-interactive', '--trusted-checkout', *args)
    elif name == 'upgrade':
        args = ('--trusted-checkout', *args)
    return command("/bin/bash", source / f"deploy/{name}.sh", *args, success=success)


def state():
    return json.loads((BASE / "state.json").read_text())


def check_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8080/healthz", timeout=3) as response:
        assert json.load(response) == {"status": "ok", "mode": "private"}
    listeners = command("ss", "-H", "-ltnp", "( sport = :8080 )").splitlines()
    assert listeners and all(line.split()[3] == "127.0.0.1:8080" for line in listeners)
    uid = int(command("id", "-u", "wm-dashboard"))
    pid = command("systemctl", "show", "--property=MainPID", "--value", "wordpress-manager.service").strip()
    assert Path(f"/proc/{pid}").stat().st_uid == uid != 0
    assert Path(f"/proc/{pid}/cwd").resolve() == (BASE / "current").resolve()
    for directory, dirs, files in os.walk(BASE):
        for name in [".", *dirs, *files]:
            path = Path(directory) / name
            if not path.is_symlink():
                info = path.stat()
                assert info.st_uid == 0 and not info.st_mode & 0o022, str(path)


def shared_snapshot():
    configs = {}
    for root in ("/etc/nginx", "/etc/php", "/etc/mysql", "/etc/phpmyadmin"):
        for path in Path(root).rglob("*"):
            if path.is_file() and not path.is_symlink():
                configs[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    packages = command("dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n")
    relevant = [line for line in packages.splitlines() if line.startswith(("nginx", "php", "mariadb", "mysql"))]
    return configs, relevant


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-system", action="store_true", required=True)
    parser.parse_args()
    assert os.geteuid() == 0
    guard = Path("/run/wm-disposable-test")
    assert guard.is_file() and guard.read_text() == "disposable Ubuntu deployment test\n"
    assert not BASE.exists() and not UNIT.exists(), "Requires a clean disposable guest"
    assert subprocess.run(["getent", "passwd", "wm-dashboard"], capture_output=True).returncode != 0
    before = shared_snapshot()
    sentinel = Path("/var/www/wm-deploy-test-site/index.php")
    database = Path("/var/lib/wm-deploy-test-database/site.sql")
    for path in (sentinel, database):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Existing customer data -- do not touch\n")
    # Docker's /tmp tmpfs is noexec; /var/tmp exercises executable release modes.
    with tempfile.TemporaryDirectory(prefix="wm-integration-", dir='/var/tmp') as temporary:
        source = Path(temporary) / "release"
        source.mkdir()
        for name in ("deploy", "wordpress_manager"):
            shutil.copytree(REPO / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("README.md", "pyproject.toml"):
            shutil.copy2(REPO / name, source / name)
        package = source / "wordpress_manager/__init__.py"
        good = package.read_text()
        package.write_text(good + "\nraise RuntimeError('injected installation failure')\n")
        failure = cli(source, "install", success=False)
        assert "injected installation failure" in failure, failure
        assert not BASE.exists() and not UNIT.exists()
        assert subprocess.run(["getent", "passwd", "wm-dashboard"], capture_output=True).returncode != 0
        print("PASS: failed installation cleans owned artifacts", flush=True)

        package.write_text(good)
        # Real interruption after APT completes, before account/service creation.
        manager = source / 'deploy/manage.py'
        manager_good = manager.read_text()
        ready = Path(temporary) / 'interrupt-ready'
        manager.write_text(manager_good.replace('        bootstrap()\n        port_available()',
            f'        bootstrap()\n        Path({str(ready)!r}).touch()\n        time.sleep(120)\n        port_available()'))
        with (Path(temporary) / 'interrupt.log').open('w') as output:
            process = subprocess.Popen(['/bin/bash', str(source / 'deploy/install.sh'), '--non-interactive', '--trusted-checkout'], stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 120
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.2)
                assert ready.exists(), 'Installer never reached interrupt point'
                process.terminate()
                assert process.wait(timeout=30) != 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        manager.write_text(manager_good)
        assert not BASE.exists() and not UNIT.exists()
        assert not Path('/etc/wordpress-manager').exists() and not Path('/var/lib/wordpress-manager-control').exists()
        print('PASS: real SIGTERM interruption cleans partial panel installation', flush=True)
        cli(source, "install")
        check_health()
        original = state()
        credentials = Path('/var/lib/wordpress-manager-control/credentials.json').read_bytes()
        webconfig = Path('/etc/wordpress-manager/web.json').read_bytes()
        original_pid = command("systemctl", "show", "--property=MainPID", "--value", "wordpress-manager.service")
        assert "healthy and unchanged" in cli(source, "install")
        assert state() == original
        assert command("systemctl", "show", "--property=MainPID", "--value", "wordpress-manager.service") == original_pid
        print("PASS: clean installation, loopback, ownership and repeat-install protection", flush=True)

        package.write_text(good + "\nraise RuntimeError('injected invalid candidate')\n")
        assert "injected invalid candidate" in cli(source, "upgrade", "--approve-upgrade", success=False)
        assert state() == original
        assert command("systemctl", "show", "--property=MainPID", "--value", "wordpress-manager.service") == original_pid
        print("PASS: invalid upgrade never interrupts service", flush=True)

        package.write_text(good.replace('return jsonify(status="ok", mode="private")', 'return jsonify(status="broken", mode="private")'))
        assert "Application health check failed" in cli(source, "upgrade", "--approve-upgrade", success=False)
        assert state() == original and not (BASE / "transaction.json").exists()
        check_health()
        print("PASS: failed runtime health triggers verified rollback", flush=True)

        package.write_text(good + "\n# integration upgrade\n")
        cli(source, "upgrade", success=False)
        assert state() == original
        cli(source, "upgrade", "--approve-upgrade")
        upgraded = state()
        assert upgraded["previous"] == original["current"] and upgraded["current"] != original["current"]
        check_health()
        cli(source, "upgrade", "--rollback", "--approve-upgrade")
        assert state()["current"] == original["current"]
        check_health()
        print("PASS: approved upgrade and explicit rollback", flush=True)
        assert Path('/var/lib/wordpress-manager-control/credentials.json').read_bytes() == credentials
        assert Path('/etc/wordpress-manager/web.json').read_bytes() == webconfig

        old = state()
        (BASE / "transaction.json").write_text(json.dumps({"kind": "upgrade", "old": old, "target": upgraded["current"]}))
        temporary_link = BASE / ".integration-next"
        temporary_link.symlink_to(Path("releases") / upgraded["current"])
        os.replace(temporary_link, BASE / "current")
        cli(source, "upgrade", "--approve-upgrade", success=False)
        cli(source, "upgrade", "--recover", "--approve-upgrade")
        assert state() == old
        check_health()
        print("PASS: interrupted activation recovery", flush=True)

        cli(source, 'uninstall', '--dry-run')
        check_health()
        assert Path('/var/lib/wordpress-manager-control/credentials.json').read_bytes() == credentials
        cli(source, "uninstall", "--yes")
        cli(source, "uninstall", "--yes")
        assert (BASE / 'uninstalled.json').exists() and not (BASE / 'releases').exists()
        assert Path('/var/lib/wordpress-manager-control/credentials.json').read_bytes() == credentials
        cli(source, 'uninstall', '--yes', '--purge-recovery')
        assert not BASE.exists() and not UNIT.exists()
        assert subprocess.run(["getent", "passwd", "wm-dashboard"], capture_output=True).returncode != 0
        assert sentinel.read_bytes() == database.read_bytes() == b"Existing customer data -- do not touch\n"
        assert shared_snapshot() == before
        print("PASS: uninstall preserves site data, database data and shared components", flush=True)
        cli(source, "install")  # Must also work while old TCP connections are in TIME_WAIT.
        check_health()
        cli(source, "uninstall", "--yes", '--purge-recovery')
        assert not BASE.exists() and not UNIT.exists()
        assert shared_snapshot() == before
        assert sentinel.read_bytes() == database.read_bytes() == b"Existing customer data -- do not touch\n"
        print("PASS: immediate clean reinstall after uninstall", flush=True)
        # Exercise the exact published-archive path, without --trusted-checkout.
        release = runpy.run_path(str(REPO / 'deploy/release.py'))
        archive = Path(temporary) / 'candidate.tar.gz'
        expected = release['build'](REPO, archive)
        verified = Path(temporary) / 'verified-release'
        release['unpack'](archive.read_bytes(), expected, verified)
        command('/bin/bash', verified / 'deploy/install.sh', '--non-interactive')
        check_health()
        packed_state = state()
        command('/bin/bash', verified / 'deploy/install.sh', '--non-interactive')
        assert state() == packed_state
        command('/bin/bash', verified / 'deploy/upgrade.sh', '--approve-upgrade')
        check_health()
        command('/bin/bash', verified / 'deploy/upgrade.sh', '--rollback', '--approve-upgrade')
        assert state()['current'] == packed_state['current']
        command(verified / 'deploy/uninstall.sh', '--yes', '--purge-recovery')
        assert not BASE.exists() and shared_snapshot() == before
        print('PASS: verified published artifact install, repeat install, upgrade and rollback', flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        subprocess.run(["journalctl", "-u", "wordpress-manager.service", "-n", "100", "--no-pager"])
        raise
