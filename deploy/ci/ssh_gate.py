#!/usr/bin/python3
"""Unprivileged SSH forced command. No shell, filesystem upload or path arguments."""
import hashlib
import json
import os
import re
import subprocess
import sys

MAX_ARCHIVE = 64 * 1024 * 1024
COMMAND = re.compile(r'wm-deploy ([a-f0-9]{40}) ([a-f0-9]{64})')


def request(command, stream):
    match = COMMAND.fullmatch(command)
    if not match:
        raise ValueError('Only wm-deploy COMMIT SHA256 is allowed')
    data = stream.read(MAX_ARCHIVE + 1)
    if not data or len(data) > MAX_ARCHIVE:
        raise ValueError('Invalid artifact size')
    commit, digest = match.groups()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Artifact checksum mismatch')
    return json.dumps({'commit': commit, 'sha256': digest}).encode() + b'\n' + data


def main():
    try:
        payload = request(os.environ.get('SSH_ORIGINAL_COMMAND', ''), sys.stdin.buffer)
        return subprocess.run(['/usr/bin/sudo', '-n', '--', '/usr/local/sbin/wm-ci-upgrade'],
                              input=payload, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                              check=False).returncode
    except (ValueError, OSError):
        print('Deployment request refused', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
