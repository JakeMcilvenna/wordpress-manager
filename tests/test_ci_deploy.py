"""Protocol, policy and workflow regression tests; no host/server mutations."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location('ci_test_' + name, REPO / 'deploy/ci' / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.mark.parametrize('command', ['', 'id', 'sudo -s', 'sftp', 'scp -t /tmp',
    'wm-deploy ' + 'a' * 40 + ' ' + 'b' * 64 + ';id',
    'wm-deploy ' + 'a' * 40 + ' ' + 'b' * 64 + '\n',
    'wm-deploy ../../etc/passwd ' + 'b' * 64])
def test_ssh_gate_rejects_arbitrary_commands(command):
    with pytest.raises(ValueError):
        module('ssh_gate').request(command, io.BytesIO(b'anything'))


def test_gate_checks_size_digest_and_preserves_exact_bytes(monkeypatch):
    gate = module('ssh_gate')
    data = b'\x00an artifact\xff'
    digest = hashlib.sha256(data).hexdigest()
    command = f'wm-deploy {"a" * 40} {digest}'
    payload = gate.request(command, io.BytesIO(data))
    header, body = payload.split(b'\n', 1)
    assert json.loads(header) == {'commit': 'a' * 40, 'sha256': digest}
    assert body == data
    with pytest.raises(ValueError, match='checksum'):
        gate.request(command, io.BytesIO(b'changed'))
    monkeypatch.setattr(gate, 'MAX_ARCHIVE', 2)
    with pytest.raises(ValueError, match='size'):
        gate.request(command, io.BytesIO(data))


def test_host_fingerprint_is_required_and_checked():
    client = module('send')
    data = b'test host public key bytes'
    key = 'ssh-ed25519 ' + base64.b64encode(data).decode()
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
    assert client.pinned_host_key(key, fingerprint, 'example.test', '2222').startswith('[example.test]:2222 ')
    assert client.pinned_host_key(key, fingerprint, 'example.test', '22').startswith('example.test ')
    for wrong_key, wrong_fingerprint in ((key, 'SHA256:' + 'a' * 43), (key + '\n' + key, fingerprint),
                                          ('@cert-authority ' + key, fingerprint), (key, '')):
        with pytest.raises(ValueError):
            client.pinned_host_key(wrong_key, wrong_fingerprint, 'example.test', '22')


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux privileged controller')
def test_receiver_revalidates_identity_and_privileged_policy(tmp_path, monkeypatch):
    receiver = module('receive')
    gate = module('ssh_gate')
    data = b'archive bytes'
    payload = gate.request(f'wm-deploy {"a" * 40} {hashlib.sha256(data).hexdigest()}', io.BytesIO(data))
    assert receiver.read_request(io.BytesIO(payload))[1] == data
    with pytest.raises(ValueError):
        receiver.read_request(io.BytesIO(payload + b'corruption'))
    with pytest.raises(ValueError):
        receiver.read_request(io.BytesIO(b'{}\n' + data))
    monkeypatch.setattr(receiver, 'HOME', tmp_path / 'home')
    monkeypatch.setattr(receiver, 'CONFIG', tmp_path / 'etc')
    monkeypatch.setattr(receiver, 'trusted', lambda *a, **k: None)
    receiver.CONFIG.mkdir()
    pinned = receiver.HOME / 'policy/deploy/provision.py'
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(b'reviewed root code')
    policy = {'deploy/provision.py': hashlib.sha256(pinned.read_bytes()).hexdigest()}
    (receiver.CONFIG / 'policy.json').write_text(json.dumps(policy))
    receiver.verify_policy(tmp_path, {'files': policy})
    for files in ({}, {**policy, 'deploy/evil.py': '0' * 64}, {'deploy/provision.py': '0' * 64}):
        with pytest.raises(ValueError, match='policy changed'):
            receiver.verify_policy(tmp_path, {'files': files})
    pinned.write_bytes(b'changed root policy')
    with pytest.raises(ValueError, match='integrity'):
        receiver.verify_policy(tmp_path, {'files': policy})


def test_release_builds_git_objects_and_ignores_dirty_checkout(tmp_path):
    spec = importlib.util.spec_from_file_location('git_release_test', REPO / 'deploy/release.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    checkout = tmp_path / 'repo'
    checkout.mkdir()
    for name in ('deploy', 'wordpress_manager', 'docs'):
        shutil.copytree(REPO / name, checkout / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('pyproject.toml', 'README.md', 'SECURITY.md'):
        shutil.copyfile(REPO / name, checkout / name)
    def git(*args):
        return subprocess.run(['git', *args], cwd=checkout, check=True, capture_output=True).stdout.decode().strip()
    git('init')
    git('add', '.')
    git('-c', 'user.name=Disposable test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
    commit = git('rev-parse', 'HEAD')
    first, second = tmp_path / 'first.tgz', tmp_path / 'second.tgz'
    assert release.build_commit(checkout, first, commit)
    (checkout / 'wordpress_manager/__init__.py').write_text('dirty and untested')
    (checkout / 'deploy/untracked-secret').write_text('never include')
    assert release.build_commit(checkout, second, commit)
    assert first.read_bytes() == second.read_bytes()
    target = tmp_path / 'verified'
    release.unpack(first.read_bytes(), hashlib.sha256(first.read_bytes()).hexdigest(), target)
    assert release.verify_tree(target)['git_commit'] == commit
    assert not (target / 'deploy/untracked-secret').exists()


def test_workflow_test_gate_and_disabled_default():
    ci = (REPO / '.github/workflows/ci.yml').read_text()
    production = (REPO / '.github/workflows/production.yml').read_text()
    # Development uses fast checks, but production keeps all original required jobs.
    assert 'branches: [main]' in ci
    assert 'branches: [main, production]' in ci
    assert 'workflow_call:' in ci
    assert 'name: test (ubuntu-24.04, 3.12)' in ci
    assert 'name: test (${{ matrix.os }}, ${{ matrix.python }})' in ci
    assert "if: github.ref == 'refs/heads/production' || github.base_ref == 'production'" in ci
    assert 'needs: [test, compatibility, browser, deployment]' in ci
    assert 'WM_TEST_BROWSER: chromium' in ci and 'ci_deploy_integration.py --disposable-system' in ci
    assert 'workflow_dispatch:' in production and 'branches: [production]' in production
    assert "github.ref == 'refs/heads/production' && vars.PRODUCTION_DEPLOY_ENABLED == 'true'" in production
    assert 'needs: verify' in production and 'uses: ./.github/workflows/ci.yml' in production
    assert 'cancel-in-progress: false' in production and 'environment: production' in production
    assert 'ssh-keyscan' not in production and 'pull_request_target' not in production
