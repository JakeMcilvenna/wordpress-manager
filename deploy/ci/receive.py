#!/usr/bin/python3
"""Root-controlled, stdin-only upgrade operation. Never execute uploaded tooling."""
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

HOME = Path('/usr/local/lib/wordpress-manager-ci')
STATE = Path('/var/lib/wordpress-manager-ci')
CONFIG = Path('/etc/wordpress-manager-ci')
BASE = Path('/opt/wordpress-manager')
MAX_ARCHIVE = 64 * 1024 * 1024
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8'}


def trusted(path, directory=False):
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != 0 or info.st_mode & 0o022):
        raise ValueError('Unsafe trusted path')


def private_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise ValueError('Unsafe private file')
    return fd


def record(event):
    event = {**event, 'timestamp': datetime.now(timezone.utc).isoformat()}
    fd = private_file(STATE / 'deployments.jsonl')
    try:
        os.write(fd, (json.dumps(event, sort_keys=True) + '\n').encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return event


def read_request(stream):
    line = stream.readline(256)
    if not line.endswith(b'\n'):
        raise ValueError('Invalid request header')
    header = json.loads(line)
    if (set(header) != {'commit', 'sha256'}
            or not re.fullmatch(r'[a-f0-9]{40}', header['commit'])
            or not re.fullmatch(r'[a-f0-9]{64}', header['sha256'])):
        raise ValueError('Invalid request identity')
    data = stream.read(MAX_ARCHIVE + 1)
    if not data or len(data) > MAX_ARCHIVE:
        raise ValueError('Invalid artifact size')
    if hashlib.sha256(data).hexdigest() != header['sha256']:
        raise ValueError('Artifact checksum mismatch')
    return header, data


def verify_policy(source, manifest):
    """Pin every deployment file, including provisioner code and wheel hashes."""
    trusted(CONFIG / 'policy.json')
    policy = json.loads((CONFIG / 'policy.json').read_text())
    actual = {name: digest for name, digest in manifest['files'].items() if name.startswith('deploy/')}
    if actual != policy:
        raise ValueError('Privileged deployment policy changed; administrator review required')
    for name, expected in policy.items():
        pinned = HOME / 'policy' / name
        trusted(pinned)
        if hashlib.sha256(pinned.read_bytes()).hexdigest() != expected:
            raise ValueError('Pinned policy integrity failure')


def current():
    trusted(BASE / 'state.json')
    state = json.loads((BASE / 'state.json').read_text())
    name = state['current']
    if not re.fullmatch(r'[0-9][A-Za-z0-9_.+-]{0,100}', name):
        raise ValueError('Invalid managed release ID')
    return name


def activate(source, log):
    # The executable and its imports are server-owned policy, never uploaded code.
    result = subprocess.run(['/bin/bash', str(HOME / 'policy/deploy/upgrade.sh'),
                             '--approve-upgrade', '--release-source', str(source)],
                            env=ENV, stdout=log, stderr=log, timeout=1200, check=False)
    if result.returncode:
        raise ValueError('Managed upgrade failed; consult root-only deployment logs/journal')


def deploy(header, data):
    event = {**header, 'attempt': uuid.uuid4().hex, 'release_id': None, 'outcome': 'started'}
    record(event)  # Incomplete attempts survive process/host failure.
    try:
        trusted(CONFIG / 'enabled')
        if (CONFIG / 'enabled').read_text() != 'enabled\n':
            raise ValueError('Server deployment is disabled')
        spec = importlib.util.spec_from_file_location('wm_pinned_release', HOME / 'policy/deploy/release.py')
        trusted(Path(spec.origin))
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        old = current()
        with tempfile.TemporaryDirectory(prefix='verified-', dir=STATE) as temporary:
            source = Path(temporary) / 'source'
            release.unpack(data, header['sha256'], source)
            manifest = release.verify_tree(source)
            if manifest.get('git_commit') != header['commit']:
                raise ValueError('Artifact Git commit mismatch')
            verify_policy(source, manifest)
            fd = private_file(STATE / 'upgrade.log')
            with os.fdopen(fd, 'wb') as log:
                activate(source, log)
            name = current()
            if name == old:
                raise ValueError('Upgrade did not activate a new release')
            trusted(BASE / 'releases' / name / 'release.json')
            metadata = json.loads((BASE / 'releases' / name / 'release.json').read_text())
            if metadata.get('git_commit') != header['commit']:
                raise ValueError('Activated commit mismatch')
            event.update(release_id=name, previous_release_id=old, outcome='success')
    except BaseException:
        event['outcome'] = 'failed'
        # A failed health check is rolled back by the managed upgrader. An
        # interrupted rollback retains its transaction and blocks later upgrades.
        try:
            event['release_id'] = current()
        except (OSError, ValueError, KeyError):
            pass
        record(event)
        raise
    return record(event)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        return 1
    os.umask(0o077)
    try:
        for path in (HOME, STATE, CONFIG):
            for ancestor in reversed((path, *path.parents)):
                trusted(ancestor, directory=True)
        fd = private_file(STATE / 'receiver.lock')
        with os.fdopen(fd, 'wb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            header, data = read_request(sys.stdin.buffer)
            receipt = deploy(header, data)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except BaseException:
        # Uploaded application diagnostics are never returned across the SSH boundary.
        print('Deployment refused or failed; administrator must inspect server audit', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
