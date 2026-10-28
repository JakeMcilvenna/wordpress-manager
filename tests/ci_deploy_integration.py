"""Actual SSH/sudo/managed-upgrade attack tests. Disposable Ubuntu ONLY.

Creates a short-lived test key inside the guest, never real deployment credentials.
Run between deploy_integration.py and stack_integration.py on the same clean guest.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tarfile
import tempfile

from deploy_integration import command, check_health, shared_snapshot, state

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/opt/wordpress-manager')
CONFIG = Path('/etc/wordpress-manager-ci')
STATE = Path('/var/lib/wordpress-manager-ci')
COMMIT = 'a' * 40


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-system', action='store_true', required=True)
    parser.parse_args()
    assert os.geteuid() == 0 and Path('/run/wm-disposable-test').read_text() == 'disposable Ubuntu deployment test\n'
    assert not BASE.exists() and not CONFIG.exists(), 'Fresh disposable system required'
    command('/usr/bin/apt-get', 'update')
    command('/usr/bin/apt-get', 'install', '-y', '--no-install-recommends', 'openssh-server', 'openssh-client', 'sudo')
    command('/usr/bin/systemctl', 'start', 'ssh.service')
    command('/bin/bash', REPO / 'deploy/install.sh', '--trusted-checkout', '--non-interactive')
    before = shared_snapshot()
    credentials = Path('/var/lib/wordpress-manager-control/credentials.json').read_bytes()
    web = Path('/etc/wordpress-manager/web.json').read_bytes()
    sentinel = Path('/var/www/wm-ci-client-data')
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b'private existing client fixture')
    sentinel.chmod(0o600)
    with tempfile.TemporaryDirectory(prefix='wm-ci-test-', dir='/var/tmp') as temporary:
        root = Path(temporary)
        key = root / 'test-key'
        command('/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key)
        command('/usr/bin/python3', '-I', REPO / 'deploy/ci/setup.py', '--reviewed-checkout', '--public-key', str(key) + '.pub')
        assert not (CONFIG / 'enabled').exists()
        command('/usr/bin/systemctl', 'restart', 'ssh.service')
        # No network key discovery: known_hosts comes directly from this guest's
        # trusted host public key, just as the real operator uses the VPS console.
        known = root / 'known_hosts'
        host_key = Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
        known.write_text('127.0.0.1 ' + ' '.join(host_key[:2]) + '\n')
        ssh = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-i', str(key),
               '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'GlobalKnownHostsFile=/dev/null', '-o', f'UserKnownHostsFile={known}',
               '-o', 'ConnectTimeout=5', 'wm-deploy@127.0.0.1']
        def remote(cmd, data=b'', success=False):
            result = subprocess.run([*ssh, cmd], input=data, capture_output=True, timeout=1500)
            assert (result.returncode == 0) == success, (cmd, result.stdout, result.stderr)
            return result
        for cmd in ('id', 'cat /var/www/wm-ci-client-data', 'sudo -s', 'bash',
                    'scp -t /tmp', 'internal-sftp', 'wm-deploy ' + COMMIT + ' ' + 'b' * 64 + ';id'):
            remote(cmd)
        # A wrong host key must fail before an authenticated request is sent.
        wrong = root / 'wrong_hosts'
        wrong.write_text('127.0.0.1 ' + ' '.join(Path(str(key) + '.pub').read_text().split()[:2]) + '\n')
        wrong_ssh = [f'UserKnownHostsFile={wrong}' if s == f'UserKnownHostsFile={known}' else s for s in ssh]
        result = subprocess.run([*wrong_ssh, 'id'], capture_output=True, timeout=20)
        assert result.returncode == 255 and b'HOST KEY' in result.stderr.upper()
        forwarding = subprocess.run([*ssh[:-1], '-o', 'ExitOnForwardFailure=yes', '-R', '0:127.0.0.1:8080',
                                     ssh[-1], 'id'], capture_output=True, timeout=20)
        assert forwarding.returncode != 0 and b'forwarding failed' in forwarding.stderr
        command('/usr/bin/sftp', '-b', '/dev/null', '-i', key, '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known}',
                'wm-deploy@127.0.0.1', success=False)
        for args in (('/bin/sh',), ('/usr/local/sbin/wm-ci-upgrade', '--rollback'),
                     ('/usr/local/sbin/wm-ci-upgrade', '--release-source', '/tmp'),
                     ('/usr/bin/python3', '-c', 'print(1)')):
            command('/usr/sbin/runuser', '-u', 'wm-deploy', '--', '/usr/bin/sudo', '-n', *args, success=False)
        for path in (sentinel, Path('/var/lib/wordpress-manager-control/credentials.json'),
                     STATE / 'deployments.jsonl'):
            command('/usr/sbin/runuser', '-u', 'wm-deploy', '--', '/usr/bin/cat', path, success=False)
        assert command('/usr/bin/id', '-nG', 'wm-deploy').strip() == 'wm-deploy'
        print('PASS: actual SSH command/SFTP/forwarding/host-key restrictions and sudo argument allowlist', flush=True)

        source = root / 'source'
        for name in ('deploy', 'wordpress_manager', 'docs'):
            shutil.copytree(REPO / name, source / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('README.md', 'SECURITY.md', 'pyproject.toml'):
            shutil.copyfile(REPO / name, source / name)
        release = runpy.run_path(str(REPO / 'deploy/release.py'))
        archive = root / 'release.tar.gz'
        def build(commit=COMMIT):
            release['build'](source, archive, commit)
            return archive.read_bytes()
        def deploy(data, success=False, commit=COMMIT):
            return remote(f'wm-deploy {commit} {hashlib.sha256(data).hexdigest()}', data, success)
        original = state()
        deploy(build())  # Both manual and push delivery must obey server disable gate.
        assert state() == original
        (CONFIG / 'enabled').write_text('enabled\n')
        (CONFIG / 'enabled').chmod(0o600)
        remote(f'wm-deploy {COMMIT} {"0" * 64}', b'wrong digest')
        deploy(b'not a tar archive')
        deploy(build('b' * 40))
        # A manifest-consistent artifact still cannot replace root code/dependencies.
        for name in ('manage.py', 'upgrade.sh', 'provision.py', 'release.py', 'requirements.lock', 'wordpress-manager.service'):
            target = source / 'deploy' / name
            good = target.read_bytes()
            payload = b"from pathlib import Path\nPath('/root/wm-ci-executed').touch()\nraise SystemExit(0)\n"
            if name.endswith('.sh'):
                payload = b'#!/bin/sh\n/usr/bin/touch /root/wm-ci-executed\nexit 0\n'
            elif name.endswith('.lock'):
                payload = b'-e /var/tmp/untrusted-build-hook\n'
            elif name.endswith('.service'):
                payload = b'[Service]\nUser=root\nExecStart=/usr/bin/touch /root/wm-ci-executed\n'
            target.write_bytes(payload)
            deploy(build())
            target.write_bytes(good)
            assert state() == original and not Path('/root/wm-ci-executed').exists()
        traversal = io.BytesIO()
        with tarfile.open(fileobj=traversal, mode='w:gz') as tar:
            entry = tarfile.TarInfo('../../root/wm-ci-escape')
            entry.size = 4
            tar.addfile(entry, io.BytesIO(b'evil'))
        deploy(traversal.getvalue())
        assert not Path('/root/wm-ci-escape').exists()
        data = build()
        # Corrupt a file while retaining the manifest and recomputing outer digest.
        altered = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar, tarfile.open(fileobj=altered, mode='w:gz') as out:
            for entry in tar:
                body = tar.extractfile(entry).read()
                if entry.name == 'wordpress_manager/__init__.py':
                    body += b'\n# tampered\n'
                    entry.size = len(body)
                out.addfile(entry, io.BytesIO(body))
        deploy(altered.getvalue())
        assert state() == original
        print('PASS: disabled server, malformed archives, traversal, commit/manifest mismatch and root-policy replacement all fail closed', flush=True)

        # Use the real production sender, including pinned fingerprint verification.
        build()
        fingerprint = command('/usr/bin/ssh-keygen', '-l', '-E', 'sha256', '-f', '/etc/ssh/ssh_host_ed25519_key.pub').split()[1]
        env = {**os.environ, 'GITHUB_SHA': COMMIT, 'GITHUB_REF': 'refs/heads/production', 'DEPLOY_ENABLED': 'true',
               'DEPLOY_HOST': '127.0.0.1', 'DEPLOY_PORT': '22', 'DEPLOY_HOST_KEY': ' '.join(host_key[:2]),
               'DEPLOY_HOST_FINGERPRINT': fingerprint, 'DEPLOY_SSH_KEY': key.read_text()}
        result = subprocess.run(['/usr/bin/python3', REPO / 'deploy/ci/send.py', archive], env=env,
                                cwd=root, capture_output=True, text=True, timeout=1500)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads((root / 'deployment-receipt.json').read_text())
        assert receipt['outcome'] == 'success' and receipt['commit'] == COMMIT
        assert state()['previous'] == original['current']
        assert json.loads((BASE / 'current/release.json').read_text())['git_commit'] == COMMIT
        check_health()
        healthy = state()
        # Existing global lock blocks the remote root operation without a switch.
        import fcntl
        with Path('/run/wordpress-manager-deploy.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(build())
        assert state() == healthy
        with (STATE / 'receiver.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(build())
        assert state() == healthy
        package = source / 'wordpress_manager/__init__.py'
        good = package.read_text()
        package.write_text(good.replace('return jsonify(status="ok", mode="private")', 'return jsonify(status="broken", mode="private")'))
        deploy(build())
        assert state() == healthy and not (BASE / 'transaction.json').exists()
        check_health()
        command('/bin/bash', '/usr/local/lib/wordpress-manager-ci/policy/deploy/upgrade.sh', '--rollback', '--approve-upgrade')
        assert state()['current'] == original['current']
        check_health()
        assert Path('/var/lib/wordpress-manager-control/credentials.json').read_bytes() == credentials
        assert Path('/etc/wordpress-manager/web.json').read_bytes() == web
        assert sentinel.read_bytes() == b'private existing client fixture'
        assert shared_snapshot() == before
        events = [json.loads(line) for line in (STATE / 'deployments.jsonl').read_text().splitlines()]
        assert any(e['outcome'] == 'success' and e['release_id'] == receipt['release_id'] for e in events)
        assert events[-1]['outcome'] == 'failed' and all(e.get('timestamp') and e['commit'] for e in events)
        print('PASS: pinned production sender, healthy activation/receipt, both locks, failed-health rollback, administrator rollback, unchanged credentials/site data/shared stack', flush=True)
    (CONFIG / 'enabled').unlink()
    command('/bin/bash', REPO / 'deploy/uninstall.sh', '--yes', '--purge-recovery')
    assert not BASE.exists()


if __name__ == '__main__':
    main()
