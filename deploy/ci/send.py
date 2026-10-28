#!/usr/bin/env python3
"""CI client: exact-commit artifact verification and fingerprint-pinned SSH."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def pinned_host_key(text, fingerprint, host, port):
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith('#')]
    if len(lines) != 1 or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint):
        raise ValueError('Exactly one trusted host key and SHA256 fingerprint required')
    parts = lines[0].split()
    if len(parts) != 2 or parts[0] != 'ssh-ed25519':
        raise ValueError('Expected pinned ssh-ed25519 public host key (type and base64 only)')
    actual = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(parts[1], validate=True)).digest()).decode().rstrip('=')
    if actual != fingerprint:
        raise ValueError('Pinned SSH host fingerprint mismatch')
    endpoint = host if int(port) == 22 else f'[{host}]:{port}'
    return f'{endpoint} {parts[0]} {parts[1]}\n'


def main():
    commit = os.environ['GITHUB_SHA']
    if (os.environ.get('GITHUB_REF') != 'refs/heads/production'
            or os.environ.get('DEPLOY_ENABLED') != 'true'
            or not re.fullmatch(r'[a-f0-9]{40}', commit)):
        raise ValueError('Production deployment is not explicitly enabled for this ref')
    artifact = Path(sys.argv[1])
    data = artifact.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if Path(str(artifact) + '.sha256').read_text().split() != [digest, artifact.name]:
        raise ValueError('Artifact checksum sidecar mismatch')
    spec = importlib.util.spec_from_file_location('ci_release', Path(__file__).resolve().parents[1] / 'release.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    host, port = os.environ['DEPLOY_HOST'], os.environ['DEPLOY_PORT']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', host) or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('Invalid deployment SSH endpoint')
    known = pinned_host_key(os.environ['DEPLOY_HOST_KEY'], os.environ['DEPLOY_HOST_FINGERPRINT'], host, port)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        release.unpack(data, digest, root / 'verified')
        if release.verify_tree(root / 'verified').get('git_commit') != commit:
            raise ValueError('Artifact does not match the tested Git commit')
        key, hosts, config = (root / name for name in ('identity', 'known_hosts', 'ssh_config'))
        key.write_text(os.environ['DEPLOY_SSH_KEY'] + '\n')
        key.chmod(0o600)
        hosts.write_text(known)
        config.write_text('')
        result = subprocess.run(['ssh', '-F', str(config), '-T', '-p', port, '-i', str(key),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={hosts}',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UpdateHostKeys=no',
            '-o', 'HostKeyAlgorithms=ssh-ed25519', '-o', 'ClearAllForwardings=yes',
            '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
            f'wm-deploy@{host}', f'wm-deploy {commit} {digest}'], input=data,
            capture_output=True, timeout=1500, check=True)
        receipt = json.loads(result.stdout)
        if (receipt.get('commit') != commit or receipt.get('sha256') != digest
                or receipt.get('outcome') != 'success' or not receipt.get('release_id')
                or not receipt.get('timestamp')):
            raise ValueError('Invalid server deployment receipt')
        Path('deployment-receipt.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
        print(json.dumps(receipt, sort_keys=True))


if __name__ == '__main__':
    main()
