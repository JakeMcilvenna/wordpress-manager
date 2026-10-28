#!/usr/bin/python3
"""Administrator-only setup from reviewed local source. Leaves deployment disabled."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess

SOURCE = Path(__file__).resolve().parents[2]
HOME = Path('/usr/local/lib/wordpress-manager-ci')
CONFIG = Path('/etc/wordpress-manager-ci')
STATE = Path('/var/lib/wordpress-manager-ci')


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr.strip()}')
    return result.stdout


def write(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--reviewed-checkout', action='store_true', required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0, 'Administrator/root required'
    os.umask(0o022)
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    assert release['ID'].strip('"') == 'ubuntu' and release['VERSION_ID'].strip('"') == '24.04'
    targets = (HOME, CONFIG, STATE, Path('/usr/local/sbin/wm-ci-upgrade'),
               Path('/etc/sudoers.d/wordpress-manager-ci'), Path('/etc/ssh/sshd_config.d/00-wordpress-manager-ci.conf'))
    for target in targets:
        assert not target.exists() and not target.is_symlink(), f'Refusing existing path: {target}'
        for ancestor in target.parents:
            info = ancestor.lstat()
            assert stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
    try:
        pwd.getpwnam('wm-deploy')
    except KeyError:
        pass
    else:
        raise ValueError('Existing deployment account requires manual review')
    key = args.public_key.read_text().strip()
    assert re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [^\r\n]*)?', key), 'One Ed25519 public key required'
    run('/usr/bin/ssh-keygen', '-l', '-f', str(args.public_key))
    assert Path('/opt/wordpress-manager/state.json').is_file(), 'A managed installation is required'
    # Pin the existing privileged runtime as well as incoming releases. A policy
    # mismatch requires deliberate administrator migration, never automatic adoption.
    for name in ('provision.py', 'requirements.lock', 'wordpress-manager.service',
                 'wordpress-manager-provisioner.service'):
        installed = Path('/opt/wordpress-manager/current/deploy') / name
        assert installed.read_bytes() == (SOURCE / 'deploy' / name).read_bytes().replace(b'\r\n', b'\n'), name
    for path, mode in ((HOME, 0o755), (CONFIG, 0o755), (STATE, 0o700)):
        path.mkdir(mode=mode)
    policy = {}
    for path in sorted((SOURCE / 'deploy').rglob('*')):
        assert not path.is_symlink(), 'No symlinks in reviewed tooling'
        if path.is_dir() or '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
            continue
        name = path.relative_to(SOURCE).as_posix()
        data = path.read_bytes().replace(b'\r\n', b'\n')
        policy[name] = hashlib.sha256(data).hexdigest()
        target = HOME / 'policy' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        write(target, data, 0o644)
    write(CONFIG / 'policy.json', (json.dumps(policy, sort_keys=True, indent=2) + '\n').encode(), 0o600)
    for name in ('receive.py', 'ssh_gate.py'):
        write(HOME / name, (SOURCE / 'deploy/ci' / name).read_bytes().replace(b'\r\n', b'\n'), 0o644)
    write(Path('/usr/local/sbin/wm-ci-upgrade'), (SOURCE / 'deploy/ci/wm-ci-upgrade').read_bytes().replace(b'\r\n', b'\n'), 0o755)
    forced = 'restrict,command="/usr/bin/python3 -I -B /usr/local/lib/wordpress-manager-ci/ssh_gate.py" '
    write(CONFIG / 'authorized_keys', (forced + key + '\n').encode(), 0o644)
    run('/usr/sbin/useradd', '--system', '--user-group', '--home-dir', '/nonexistent', '--shell', '/bin/sh', 'wm-deploy')
    write(targets[-2], (SOURCE / 'deploy/ci/sudoers').read_bytes().replace(b'\r\n', b'\n'), 0o440)
    run('/usr/sbin/visudo', '-cf', '/etc/sudoers')
    write(targets[-1], (SOURCE / 'deploy/ci/sshd.conf').read_bytes().replace(b'\r\n', b'\n'), 0o644)
    run('/usr/sbin/sshd', '-t')
    effective = dict(line.split(' ', 1) for line in run('/usr/sbin/sshd', '-T', '-C',
        'user=wm-deploy,host=localhost,addr=127.0.0.1').splitlines())
    expected = {'forcecommand': '/usr/bin/python3 -I -B /usr/local/lib/wordpress-manager-ci/ssh_gate.py',
                'disableforwarding': 'yes', 'permittty': 'no', 'permituserrc': 'no',
                'passwordauthentication': 'no', 'kbdinteractiveauthentication': 'no',
                'authorizedkeysfile': '/etc/wordpress-manager-ci/authorized_keys',
                'authorizedkeyscommand': 'none', 'trustedusercakeys': 'none',
                'authenticationmethods': 'publickey', 'permituserenvironment': 'no'}
    assert all(effective.get(k) == v for k, v in expected.items()), 'Effective SSH restrictions differ; review before enabling'
    print('Setup prepared and DISABLED. Review configuration, reload ssh, test restrictions, then explicitly enable.')


if __name__ == '__main__':
    main()
