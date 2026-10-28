"""Real filesystem transactions with a simulated OS command boundary.

Never run package managers, modify accounts, or touch host services in this suite.
The opt-in disposable Ubuntu VM test covers the actual command integration.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux deployment filesystem semantics")
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wm_deploy_test", REPO / "deploy/manage.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    source = tmp_path / "checkout"
    for name in ("deploy", "wordpress_manager"):
        shutil.copytree(REPO / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("README.md", "pyproject.toml"):
        shutil.copy2(REPO / name, source / name)
    base = tmp_path / "opt/wordpress-manager"
    unit = tmp_path / "etc/systemd/system/wordpress-manager.service"
    lock = tmp_path / "run/lock/wordpress-manager-deploy.lock"
    for parent in (base.parent, unit.parent, lock.parent):
        parent.mkdir(parents=True)
    for name, value in {"BASE": base, "UNIT": unit, "LOCK": lock, "SOURCE": source,
                        "PROC": tmp_path / "proc", "ROOT_UID": os.getuid()}.items():
        monkeypatch.setattr(m, name, value)
    # The production unit always uses fixed paths; the simulator doesn't run it.
    host = SimpleNamespace(account=None, active=False, enabled=False, calls=[], failure=None,
                           fragment="", dropins="", now=0, listener="127.0.0.1:8080", pid=500)
    monkeypatch.setattr(m, "platform_check", lambda **kwargs: None)
    monkeypatch.setattr(m, 'PATH_ROOT', tmp_path)
    monkeypatch.setattr(m, 'ROOT_GID', os.getgid())
    monkeypatch.setattr(m, 'SITE_CONFIG_ROOTS', (tmp_path / 'site-config',))
    host.removable_account = m.removable_account
    monkeypatch.setattr(m, 'removable_account', lambda marker: host.account is not None)
    monkeypatch.setattr(m, "configure_control", lambda *args: None)
    monkeypatch.setattr(m, "CONTROL", tmp_path / 'control')
    monkeypatch.setattr(m, "WEB_CONFIG", tmp_path / 'webconfig')
    monkeypatch.setattr(m, "PROVISION_UNIT", tmp_path / 'provision.service')
    monkeypatch.setattr(m, "CONTROL_SOCKET", tmp_path / 'control-runtime/control.sock')
    monkeypatch.setattr(m, "LOG", tmp_path / 'deploy.log')
    monkeypatch.setattr(m, "account_record", lambda: host.account)
    def account(uid):
        if host.account is None or uid != 999:
            m.fail("Account mismatch")
    monkeypatch.setattr(m, "validate_account", account)
    monkeypatch.setattr(m, "port_available", lambda: None)
    monkeypatch.setattr(m.time, "monotonic", lambda: host.now)
    monkeypatch.setattr(m.time, "sleep", lambda seconds: setattr(host, "now", host.now + seconds))

    def command(*args, **kwargs):
        args = tuple(str(a) for a in args)
        host.calls.append(args)
        exe = Path(args[0]).name
        def error():
            raise subprocess.CalledProcessError(1, args, stderr="simulated command failure")
        if exe == "apt-get" and host.failure == "apt":
            error()
        if exe == "useradd":
            host.account = SimpleNamespace(pw_uid=999)
        elif exe == "userdel":
            host.account = None
        elif exe == "python3" and "venv" in args:
            venv = Path(args[-1])
            (venv / "bin").mkdir(parents=True)
            for script in ("python", "gunicorn"):
                (venv / "bin" / script).write_text("fake executable\n")
        elif "pip" in args and "install" in args and host.failure == "pip":
            error()
        elif "-c" in args:
            site = Path(args[0]).parents[1] / "lib/python3.12/site-packages"
            site.mkdir(parents=True)
            return str(site)
        elif exe == "runuser" and "--check-config" in args:
            if "INVALID_APP" in (Path(kwargs["cwd"]) / "wordpress_manager/__init__.py").read_text():
                error()
        elif exe == "systemctl":
            if "--property=DropInPaths" in args:
                return host.dropins
            if "--property=FragmentPath" in args:
                if args[-1] != m.SERVICE:
                    return str(m.PROVISION_UNIT) if m.PROVISION_UNIT.exists() else ''
                return str(unit) if unit.exists() else host.fragment
            if "--property=MainPID" in args:
                return str(host.pid)
            if "is-active" in args:
                return "active" if host.active else "failed"
            if "enable" in args or "restart" in args:
                current = (base / "current").resolve()
                if "FAIL_START" in (current / "wordpress_manager/__init__.py").read_text():
                    error()
                proc = m.PROC / str(host.pid)
                proc.mkdir(parents=True, exist_ok=True)
                (proc / "cwd").unlink(missing_ok=True)
                (proc / "cwd").symlink_to(current)
                (proc / "status").write_text("Uid:\t999\t999\t999\t999\n")
                host.active = True
                host.enabled = True
            if "disable" in args:
                host.active = host.enabled = False
        elif exe == "ss":
            return f'LISTEN 0 2048 {host.listener} 0.0.0.0:* users:(("gunicorn",pid={host.pid},fd=5))'
        return ""
    monkeypatch.setattr(m, "run", command)
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            package = (base / "current/wordpress_manager/__init__.py").read_text()
            return json.dumps({"status": "bad" if "FAIL_HEALTH" in package else "ok", "mode": "private"}).encode()
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **kw: Response()))
    host.m = m
    return host


def installed(host):
    host.m.install()
    return host.m.read_state()


def modify_source(host, marker):
    path = host.m.SOURCE / "wordpress_manager/__init__.py"
    path.write_text(path.read_text() + "\n# " + marker + "\n")


def test_clean_install_is_root_owned_private_and_enabled(deployment):
    h, m = deployment, deployment.m
    state = installed(h)
    assert h.active and h.enabled and h.account.pw_uid == 999
    assert state["previous"] is None
    assert (m.BASE / "current").readlink() == Path("releases") / state["current"]
    assert not (m.BASE / "transaction.json").exists()
    assert (m.BASE / "state.json").stat().st_mode & 0o777 == 0o600
    assert m.digest(m.UNIT) == m.UNIT_SHA256
    apt = [call for call in h.calls if Path(call[0]).name == "apt-get"]
    assert apt[-1][1:] == ("install", "--yes", "--no-install-recommends", "--no-upgrade", "python3", "python3-venv", "ca-certificates", "iproute2")
    pip = next(call for call in h.calls if "pip" in call and "install" in call)
    assert all(flag in pip for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--isolated"))
    useradd = next(call for call in h.calls if Path(call[0]).name == "useradd")
    assert all(flag in useradd for flag in ("--system", "--no-create-home", "--user-group"))


def test_repeat_install_preserves_configuration(deployment):
    h, m = deployment, deployment.m
    state = installed(h)
    calls = list(h.calls)
    m.install()
    assert m.read_state() == state and h.active
    assert not any(Path(call[0]).name in ('apt-get', 'useradd', 'userdel') for call in h.calls[len(calls):])


def test_cleanup_reloads_systemd_when_only_control_unit_was_created(deployment):
    h, m = deployment, deployment.m
    installed(h)
    m.UNIT.unlink()  # Interruption after control setup but before dashboard unit publication.
    marker = m.managed()
    marker['control'] = True
    m.write_json(m.BASE / '.managed.json', marker)
    m.CONTROL.mkdir(mode=0o700)
    m.WEB_CONFIG.mkdir(mode=0o750)
    m.PROVISION_UNIT.write_bytes((m.SOURCE / 'deploy/wordpress-manager-provisioner.service').read_bytes())
    h.calls.clear()
    m.cleanup_install()
    assert not any(path.exists() for path in (m.BASE, m.PROVISION_UNIT, m.CONTROL, m.WEB_CONFIG))
    assert ('/usr/bin/systemctl', 'daemon-reload') in h.calls


@pytest.mark.parametrize("existing", ["directory", "symlink", "unit", "account", "loaded-service", "dropin"])
def test_unmanaged_installations_are_never_overwritten(deployment, existing):
    h, m = deployment, deployment.m
    if existing == "directory":
        m.BASE.mkdir()
    elif existing == "symlink":
        m.BASE.symlink_to(m.BASE.parent / "missing")
    elif existing == "unit":
        m.UNIT.write_text("unmanaged")
    elif existing == "account":
        h.account = SimpleNamespace(pw_uid=1000)
    elif existing == "loaded-service":
        h.fragment = "/lib/systemd/system/wordpress-manager.service"
    else:
        h.dropins = "/etc/systemd/system/wordpress-manager.service.d/custom.conf"
    with pytest.raises(m.DeploymentError):
        m.install()
    assert not any(Path(call[0]).name in ("apt-get", "useradd", "userdel") for call in h.calls)


@pytest.mark.parametrize("failure", ["apt", "pip", "INVALID_APP", "FAIL_START", "FAIL_HEALTH"])
def test_failed_install_cleans_only_owned_panel_artifacts(deployment, failure):
    h, m = deployment, deployment.m
    sentinel = m.BASE.parent / "wordpress-site.sql"
    sentinel.write_bytes(b"customer data")
    h.failure = failure
    modify_source(h, failure)
    with pytest.raises((m.DeploymentError, subprocess.CalledProcessError)):
        m.install()
    assert not m.BASE.exists() and not m.UNIT.exists()
    assert h.account is None and not h.active and not h.enabled
    assert sentinel.read_bytes() == b"customer data"


def test_successful_upgrade_retains_previous_release_and_unit(deployment):
    h, m = deployment, deployment.m
    old = installed(h)
    unit = m.UNIT.read_bytes()
    modify_source(h, "new release")
    m.upgrade()
    state = m.read_state()
    assert state["current"] != old["current"] and state["previous"] == old["current"]
    assert m.release_path(old["current"]).is_dir() and m.UNIT.read_bytes() == unit
    m.upgrade("previous")
    assert m.read_state()["current"] == old["current"]
    assert h.active


@pytest.mark.parametrize("failure", ["FAIL_START", "FAIL_HEALTH"])
def test_upgrade_failure_rolls_back_and_checks_old_service(deployment, failure):
    h, m = deployment, deployment.m
    old = installed(h)
    modify_source(h, failure)
    with pytest.raises((m.DeploymentError, subprocess.CalledProcessError)):
        m.upgrade()
    assert m.read_state() == old
    m.health_once(old["current"])
    assert len(list((m.BASE / "releases").iterdir())) == 2
    assert not (m.BASE / "transaction.json").exists()


def test_invalid_candidate_never_restarts_existing_service(deployment):
    h, m = deployment, deployment.m
    old = installed(h)
    modify_source(h, "INVALID_APP")
    before = len(h.calls)
    with pytest.raises(subprocess.CalledProcessError):
        m.upgrade()
    assert not any("restart" in call or "disable" in call for call in h.calls[before:])
    assert m.read_state() == old and h.active


def test_interrupted_upgrade_requires_explicit_recovery(deployment):
    h, m = deployment, deployment.m
    old = installed(h)
    version, files = m.source_snapshot()
    target = m.build_release(version, files)
    m.write_json(m.BASE / "transaction.json", {"kind": "upgrade", "old": old, "target": target})
    m.switch_current(target)  # Simulate power loss immediately after pointer switch.
    with pytest.raises(m.DeploymentError, match="Unfinished deployment"):
        m.upgrade()
    assert m.main(["upgrade", "--recover", "--approve-upgrade"]) == 0
    assert m.read_state() == old
    m.health_once(old["current"])


def test_failed_rollback_preserves_journal_for_later_recovery(deployment, monkeypatch):
    h, m = deployment, deployment.m
    old = installed(h)
    modify_source(h, "FAIL_HEALTH")
    original = m.wait_healthy
    monkeypatch.setattr(m, "wait_healthy", lambda name: m.fail("health unavailable"))
    with pytest.raises(m.DeploymentError):
        m.upgrade()
    assert (m.BASE / "transaction.json").exists()
    monkeypatch.setattr(m, "wait_healthy", original)
    m.recover()
    assert m.read_state() == old


def test_interrupted_install_recovery_removes_only_owned_artifacts(deployment):
    h, m = deployment, deployment.m
    installed(h)
    m.write_json(m.BASE / "transaction.json", {"kind": "install"})
    m.recover()
    assert not m.BASE.exists() and not m.UNIT.exists() and h.account is None and not h.active
    m.install()
    assert h.active


def test_interrupted_uninstall_can_resume_after_unit_removal(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    original = m.remove_account
    monkeypatch.setattr(m, "remove_account", lambda marker: m.fail("simulated interruption"))
    with pytest.raises(m.DeploymentError, match="interruption"):
        m.uninstall()
    assert not m.UNIT.exists() and (m.BASE / "transaction.json").exists()
    monkeypatch.setattr(m, "remove_account", original)
    m.recover()
    assert (m.BASE / 'uninstalled.json').exists() and h.account is None
    m.uninstall()  # A completed uninstall is idempotent.
    m.uninstall(purge_recovery=True)
    assert not m.BASE.exists()


def test_install_marker_write_failure_does_not_leave_directory(deployment, monkeypatch):
    h, m = deployment, deployment.m
    monkeypatch.setattr(m, "write_json", lambda *args: m.fail("disk full"))
    with pytest.raises(m.DeploymentError, match="disk full"):
        m.install()
    assert not m.BASE.exists() and not h.account


def test_unrecorded_account_is_never_deleted_by_recovery(deployment):
    h, m = deployment, deployment.m
    installed(h)
    m.write_json(m.BASE / ".managed.json", {"schema": 1, "application": m.SERVICE, "uid": None})
    m.write_json(m.BASE / "transaction.json", {"kind": "install"})
    with pytest.raises(m.DeploymentError, match="unrecorded"):
        m.recover()
    assert h.account is not None and (m.BASE / "transaction.json").exists()


def test_source_checksums_reject_changed_rollback_target(deployment):
    h, m = deployment, deployment.m
    old = installed(h)
    m.upgrade()
    current = m.read_state()
    path = m.release_path(old["current"]) / "wordpress_manager/__init__.py"
    path.write_text(path.read_text() + "\n# changed after release\n")
    with pytest.raises(m.DeploymentError, match="checksum mismatch"):
        m.upgrade("previous")
    assert m.read_state() == current


def test_uninstall_leaves_sites_databases_and_shared_components(deployment):
    h, m = deployment, deployment.m
    installed(h)
    sentinels = [m.BASE.parent / name for name in ("www/site/index.php", "mysql/site.ibd", "nginx/nginx.conf", "php/php.ini", "phpmyadmin/config.inc.php")]
    for path in sentinels:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"untouched")
    h.calls.clear()
    m.uninstall(purge_recovery=True)
    assert not m.BASE.exists() and not m.UNIT.exists() and h.account is None and not h.active
    assert all(path.read_bytes() == b"untouched" for path in sentinels)
    assert not any(Path(call[0]).name == "apt-get" for call in h.calls)
    assert not any("--remove" in call or "-r" in call for call in h.calls if Path(call[0]).name == "userdel")


def recovery_files(m):
    m.CONTROL.mkdir(mode=0o700)
    m.WEB_CONFIG.mkdir(mode=0o750)
    (m.CONTROL / 'ledger.sqlite').write_bytes(b'audit history')
    (m.WEB_CONFIG / 'web.json').write_bytes(b'credentials and settings')
    marker = m.managed()
    marker['directories'] = {str(p): m.identity(p) for p in (m.CONTROL, m.WEB_CONFIG)}
    m.write_json(m.BASE / '.managed.json', marker)


def test_default_removal_retains_recovery_data_and_can_purge_later(deployment):
    h, m = deployment, deployment.m
    state = installed(h)
    recovery_files(m)
    m.LOG.write_bytes(b'existing log')
    m.uninstall()
    assert not m.UNIT.exists() and not (m.BASE / 'releases').exists()
    assert m.read_json(m.BASE / 'state.json') == state
    assert (m.CONTROL / 'ledger.sqlite').read_bytes() == b'audit history'
    assert (m.WEB_CONFIG / 'web.json').read_bytes() == b'credentials and settings'
    m.uninstall()
    m.uninstall(purge_recovery=True)
    m.uninstall(purge_recovery=True)
    assert not any(p.exists() for p in (m.BASE, m.CONTROL, m.WEB_CONFIG))
    assert m.LOG.read_bytes() == b'existing log'


def snapshot_tree(root):
    result = {}
    for p in root.rglob('*'):
        if not p.is_symlink():
            info = p.lstat()
            result[str(p.relative_to(root))] = ((info.st_mode, info.st_ino, info.st_uid,
                info.st_gid, info.st_mtime_ns, info.st_ctime_ns), p.read_bytes() if p.is_file() else None)
    return result


def test_dry_run_has_no_writes_or_mutating_commands(deployment):
    h, m = deployment, deployment.m
    installed(h)
    recovery_files(m)
    before = snapshot_tree(m.BASE.parent.parent)
    h.calls.clear()
    assert m.main(['uninstall', '--dry-run']) == 0
    assert snapshot_tree(m.BASE.parent.parent) == before
    assert not m.LOG.exists() and not m.LOCK.exists()
    assert not any(any(a in call for a in ('disable', 'stop', 'userdel', 'daemon-reload')) for call in h.calls)
    assert h.active


@pytest.mark.parametrize('kind', ['unit', 'release', 'config', 'unknown-file', 'unknown-config', 'hardlink', 'ancestor'])
def test_unsafe_resources_block_before_any_service_change(deployment, kind):
    h, m = deployment, deployment.m
    state = installed(h)
    recovery_files(m)
    site = m.BASE.parent / 'customer-site'
    site.mkdir()
    data = site / 'index.php'
    data.write_bytes(b'customer website')
    release = m.release_path(state['current'])
    if kind == 'unit':
        m.UNIT.unlink()
        m.UNIT.symlink_to(data)
    elif kind == 'release':
        shutil.rmtree(release)
        release.symlink_to(site, target_is_directory=True)
    elif kind == 'config':
        shutil.rmtree(m.CONTROL)
        m.CONTROL.symlink_to(site, target_is_directory=True)
    elif kind == 'unknown-file':
        (release / 'customer.sql').write_bytes(b'unknown data')
    elif kind == 'unknown-config':
        (m.CONTROL / 'customer.sql').write_bytes(b'website database')
    elif kind == 'hardlink':
        os.link(data, release / 'customer.php')
    else:
        original = m.BASE / 'releases'
        original.rename(m.BASE / 'moved-releases')
        original.symlink_to(m.BASE / 'moved-releases', target_is_directory=True)
    h.calls.clear()
    with pytest.raises(m.DeploymentError):
        m.uninstall(purge_recovery=True)
    assert h.active and data.read_bytes() == b'customer website'
    assert not (m.BASE / 'transaction.json').exists()
    assert not any('disable' in call or 'userdel' in call for call in h.calls)


def test_mount_inside_release_blocks_removal(deployment, monkeypatch):
    h, m = deployment, deployment.m
    state = installed(h)
    mount = m.release_path(state['current']) / '.venv'
    monkeypatch.setattr(m.os.path, 'ismount', lambda path: path == mount)
    with pytest.raises(m.DeploymentError, match='Mounted resource'):
        m.uninstall()
    assert h.active


def test_missing_files_and_interrupted_deployment_do_not_require_healthy_panel(deployment):
    h, m = deployment, deployment.m
    state = installed(h)
    (m.release_path(state['current']) / 'wordpress_manager/__init__.py').unlink()
    (m.BASE / 'state.json').unlink()
    m.write_json(m.BASE / 'transaction.json', {'kind': 'upgrade', 'target': 'incomplete'})
    m.uninstall()
    journal = m.read_json(m.BASE / 'uninstalled.json')
    assert journal['transaction']['previous_transaction']['kind'] == 'upgrade'
    assert not m.UNIT.exists() and not h.active


def test_provisioner_dropins_are_checked_before_stopping_panel(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    original = m.run
    def run(*args, **kwargs):
        if '--property=DropInPaths' in args and args[-1] == m.PROVISION_UNIT.name:
            return 'operator-override.conf'
        return original(*args, **kwargs)
    monkeypatch.setattr(m, 'run', run)
    with pytest.raises(m.DeploymentError, match='drop-ins'):
        m.uninstall()
    assert h.active


def test_dependent_site_service_blocks_uninstall(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    original = m.run
    def run(*args, **kwargs):
        if 'list-dependencies' in args:
            return 'wordpress-manager.service\ncustomer-site.service\nmulti-user.target'
        return original(*args, **kwargs)
    monkeypatch.setattr(m, 'run', run)
    with pytest.raises(m.DeploymentError, match='decouple'):
        m.uninstall()
    assert h.active


def test_stop_failure_keeps_code_and_recovery_journal(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    original = m.run
    def run(*args, **kwargs):
        if 'disable' in args:
            raise subprocess.CalledProcessError(1, args)
        return original(*args, **kwargs)
    monkeypatch.setattr(m, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        m.uninstall()
    assert m.UNIT.exists() and (m.BASE / 'releases').exists()
    assert m.read_json(m.BASE / 'transaction.json')['kind'] == 'uninstall'


@pytest.mark.parametrize('confirmation', ['REMOVE PANEL', 'no'])
def test_interactive_uninstall_requires_exact_confirmation(deployment, monkeypatch, confirmation):
    h, m = deployment, deployment.m
    installed(h)
    monkeypatch.setattr(m.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr('builtins.input', lambda prompt: confirmation)
    result = m.main(['uninstall'])
    assert result == (0 if confirmation == 'REMOVE PANEL' else 1)
    assert m.UNIT.exists() == (confirmation != 'REMOVE PANEL')


def test_partial_manifest_installation_can_be_removed(deployment):
    h, m = deployment, deployment.m
    m.BASE.mkdir()
    m.write_json(m.BASE / '.managed.json', {'schema': m.SCHEMA, 'application': m.SERVICE,
        'ownership_version': 1, 'uid': None, 'directories': {}, 'releases': {}})
    m.write_json(m.BASE / 'transaction.json', {'kind': 'install'})
    m.uninstall()
    assert (m.BASE / 'uninstalled.json').exists()
    assert not h.active


def test_repurposed_account_is_retained(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    monkeypatch.setattr(m, 'removable_account', h.removable_account)
    monkeypatch.setattr(m, 'validate_account', lambda uid: m.fail('changed account identity'))
    m.uninstall()
    assert h.account is not None and not m.UNIT.exists() and not h.active


def test_account_owning_external_website_is_retained(deployment, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip('Requires chown in a disposable root test guest')
    h, m = deployment, deployment.m
    installed(h)
    site = m.BASE.parent / 'website'
    site.mkdir()
    file = site / 'index.php'
    file.write_bytes(b'production website')
    os.chown(file, 999, -1)
    monkeypatch.setattr(m, 'removable_account', h.removable_account)
    walk = m.os.walk
    monkeypatch.setattr(m.os, 'walk', lambda root, **kwargs: walk(site if root == '/' else root, **kwargs))
    m.uninstall()
    assert h.account is not None and file.read_bytes() == b'production website'
    assert not m.UNIT.exists()


def test_missing_unit_still_removes_recorded_enable_link(deployment):
    h, m = deployment, deployment.m
    installed(h)
    directory = m.UNIT.parent / 'multi-user.target.wants'
    directory.mkdir()
    link = directory / m.UNIT.name
    link.symlink_to(m.UNIT)
    m.UNIT.unlink()
    m.uninstall()
    assert not link.is_symlink()


def test_purge_resumes_after_configuration_tree_was_removed(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    recovery_files(m)
    original = m.shutil.rmtree
    def remove(path, *args, **kwargs):
        if path == m.CONTROL:
            m.fail('interrupted before control purge')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(m.shutil, 'rmtree', remove)
    with pytest.raises(m.DeploymentError, match='interrupted'):
        m.uninstall(purge_recovery=True)
    assert not m.WEB_CONFIG.exists() and m.CONTROL.exists()
    assert m.read_json(m.BASE / 'transaction.json')['purge_recovery'] is True
    monkeypatch.setattr(m.shutil, 'rmtree', original)
    m.uninstall(purge_recovery=True)
    assert not m.BASE.exists() and not m.CONTROL.exists()


def test_final_purge_interruption_leaves_only_safe_empty_directory(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    original = Path.rmdir
    def rmdir(path, *args, **kwargs):
        if path == m.BASE:
            m.fail('final directory interruption')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'rmdir', rmdir)
    with pytest.raises(m.DeploymentError, match='interruption'):
        m.uninstall(purge_recovery=True)
    assert not list(m.BASE.iterdir()) and not h.active and h.account is None
    m.uninstall(purge_recovery=True)  # Safe no-op; no ownership is inferred.


def test_repeated_removal_retains_original_interrupted_deployment_journal(deployment):
    h, m = deployment, deployment.m
    installed(h)
    m.write_json(m.BASE / 'transaction.json', {'kind': 'upgrade', 'target': 'pending'})
    m.uninstall()
    m.uninstall()
    assert m.read_json(m.BASE / 'uninstalled.json')['transaction']['previous_transaction'] == {'kind': 'upgrade', 'target': 'pending'}


@pytest.mark.parametrize('content', ['cron', 'fpm', 'link'])
def test_panel_dependent_site_configuration_blocks_before_stopping(deployment, content):
    h, m = deployment, deployment.m
    installed(h)
    config = m.SITE_CONFIG_ROOTS[0]
    config.mkdir()
    if content == 'cron':
        (config / 'backup').write_text(f'0 3 * * * root {m.BASE}/current/backup.py\n')
    elif content == 'fpm':
        (config / 'site-pool.conf').write_text(f'[site]\nuser = {m.ACCOUNT}\n')
    else:
        (config / 'website-root').symlink_to(m.BASE / 'current')
    with pytest.raises(m.DeploymentError, match='references'):
        m.uninstall()
    assert h.active and m.UNIT.exists() and not (m.BASE / 'transaction.json').exists()


def test_purge_retains_minimal_account_manifest_for_repeatable_removal(deployment, monkeypatch):
    h, m = deployment, deployment.m
    installed(h)
    recovery_files(m)
    monkeypatch.setattr(m, 'removable_account', lambda marker: False)
    m.uninstall(purge_recovery=True)
    m.uninstall(purge_recovery=True)
    assert h.account is not None and (m.BASE / '.managed.json').exists()
    assert not any(p.exists() for p in (m.CONTROL, m.WEB_CONFIG, m.BASE / 'state.json'))
    assert m.managed()['releases'] == m.managed()['directories'] == {}
    h.account = None  # Operator later resolves the externally owned account.
    m.uninstall(purge_recovery=True)
    assert not m.BASE.exists()


@pytest.mark.parametrize("argv", [["upgrade"], ["upgrade", "--recover"], ["upgrade", "--rollback"], ["uninstall"]])
def test_explicit_approval_required_before_work(deployment, argv):
    h, m = deployment, deployment.m
    with pytest.raises(SystemExit) as error:
        m.main(argv)
    assert error.value.code == 2 and not h.calls and not m.BASE.exists()


def test_changed_security_policy_and_dropins_are_rejected(deployment):
    h, m = deployment, deployment.m
    installed(h)
    h.dropins = "override.conf"
    with pytest.raises(m.DeploymentError, match="drop-ins"):
        m.upgrade()
    h.dropins = ""
    m.UNIT.write_text(m.UNIT.read_text().replace("User=wm-dashboard", "User=root"))
    with pytest.raises(m.DeploymentError, match="security policy"):
        m.uninstall()


def test_symlinks_and_world_writable_code_rejected(deployment):
    h, m = deployment, deployment.m
    state = installed(h)
    path = m.release_path(state["current"]) / "wordpress_manager/__init__.py"
    path.chmod(0o666)
    with pytest.raises(m.DeploymentError, match="Unsafe ownership"):
        m.validate_release(state["current"])
    path.chmod(0o644)
    path.unlink()
    path.symlink_to(m.SOURCE / "wordpress_manager/__init__.py")
    with pytest.raises(m.DeploymentError, match="Unsafe ownership"):
        m.validate_release(state["current"])


@pytest.mark.parametrize("release", ["../../etc", "/etc", "releases/a", "", None])
def test_release_path_cannot_escape_installation(deployment, release):
    m = deployment.m
    with pytest.raises(m.DeploymentError, match="Invalid release"):
        m.release_path(release)


def test_public_or_foreign_listener_fails_health(deployment):
    h, m = deployment, deployment.m
    state = installed(h)
    h.listener = "0.0.0.0:8080"
    with pytest.raises(m.DeploymentError, match="exclusively"):
        m.health_once(state["current"])
    h.listener = "127.0.0.1:8080"
    h.pid = 501
    proc = m.PROC / "501"
    proc.mkdir()
    (proc / "cwd").symlink_to(m.release_path(state["current"]))
    (proc / "status").write_text("Uid:\t0\t0\t0\t0\n")
    with pytest.raises(m.DeploymentError, match="restricted dashboard"):
        m.health_once(state["current"])


def test_deployment_lock_excludes_concurrent_processes(deployment):
    m = deployment.m
    if os.getuid() != 0:
        pytest.skip("Lock ownership intentionally requires root")
    with m.deployment_lock():
        with pytest.raises(m.DeploymentError, match="Another deployment"):
            with m.deployment_lock():
                pass


def test_shell_entrypoints_parse_and_use_fixed_interpreter():
    for command in ("install", "upgrade", "uninstall"):
        path = REPO / f"deploy/{command}.sh"
        subprocess.run(["/bin/bash", "-n", str(path)], check=True)
        result = subprocess.run(["/bin/bash", str(path), "--help"], text=True, capture_output=True)
        assert result.returncode == 0 and "Root-only deployment CLI" in result.stdout


def test_canonical_unit_keeps_all_existing_restrictions():
    text = (REPO / "deploy/wordpress-manager.service").read_text()
    for setting in ("User=wm-dashboard", "Group=wm-dashboard", "--bind 127.0.0.1:8080",
                    "--no-control-socket", "--config /dev/null", "NoNewPrivileges=true", "PrivateTmp=true",
                    "PrivateDevices=true", "ProtectSystem=strict", "ProtectHome=true", "ProtectKernelTunables=true",
                    "ProtectKernelModules=true", "ProtectControlGroups=true", "RestrictSUIDSGID=true",
                    "RestrictRealtime=true", "LockPersonality=true", "CapabilityBoundingSet=\n",
                    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "UMask=0077"):
        assert setting in text


def test_port_check_allows_immediate_reinstall_after_closed_connections(monkeypatch):
    spec = importlib.util.spec_from_file_location("wm_port_test", REPO / "deploy/manage.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    real_socket = socket.socket
    with real_socket() as listener, real_socket() as client:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        address = listener.getsockname()
        listener.listen()
        client.settimeout(2)
        client.connect(address)
        accepted, _ = listener.accept()
        accepted.close()  # Active server close puts its port into TIME_WAIT.
        assert client.recv(1) == b""
    with real_socket() as probe:
        with pytest.raises(OSError):
            probe.bind(address)  # Reproduce the previous installer's failure.
    class Probe(real_socket):
        def bind(self, requested):
            assert requested == ("127.0.0.1", 8080)
            return super().bind(address)
    monkeypatch.setattr(m.socket, "socket", Probe)
    monkeypatch.setattr(m, "run", lambda *args: "")
    m.port_available()
    monkeypatch.setattr(m, "run", lambda *args: "LISTEN ...")
    with pytest.raises(m.DeploymentError, match="already in use"):
        m.port_available()
