import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='POSIX provisioning boundary')
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def provision(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('provision_test', REPO / 'deploy/provision.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(m, 'CONFIG', tmp_path / 'web.json')
    m.CONFIG.write_text('{"provisioning":true}')
    # initialize's ownership check is intentionally root-only; CI tests run as a regular user.
    m.STATE.mkdir(mode=0o700)
    with m.db() as connection:
        connection.executescript('''
        CREATE TABLE audit(time REAL,action TEXT,detail TEXT);
        CREATE TABLE sessions(token TEXT PRIMARY KEY,expires REAL);
        CREATE TABLE plans(id TEXT PRIMARY KEY,owner TEXT,expires REAL,body TEXT,used INTEGER DEFAULT 0);
        CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT,detail TEXT);
        ''')
    monkeypatch.setattr(m, 'command', lambda *args: pytest.fail('Unexpected real command'))
    return m


def baseline(m):
    return {'packages': {'php8.5-fpm': '8.5.12'}, 'configs': {'/etc/php/8.5/fpm/pool.d/site.conf': 'hash'},
            'paths': {p: False for p in ('/usr/local/bin/wp', '/usr/bin/wp', '/usr/local/nginx',
            '/usr/local/sbin/nginx', '/usr/local/bin/php', '/usr/local/sbin/php-fpm', '/usr/local/mysql',
            '/opt/lampp', '/usr/share/phpmyadmin', str(m.STACK))}}


def test_php_versions_and_configs_are_preserved(provision, monkeypatch):
    m = provision
    before = baseline(m)
    monkeypatch.setattr(m, 'inventory', lambda: before)
    calls = []
    def apt(names, installed):
        calls.extend(names)
        return {name: 'candidate' for name in names}
    monkeypatch.setattr(m, 'apt_changes', apt)
    plan = m.make_plan('8.3')
    assert plan['preserved_php'] == ['php8.5-fpm']
    assert 'php8.5-fpm' not in calls and 'php8.3-fpm' in calls
    assert before == plan['baseline']
    with pytest.raises(ValueError, match='already have all required extensions'):
        m.make_plan('8.5')
    with pytest.raises(ValueError, match='Unsupported'):
        m.make_plan('8.3; touch /tmp/pwned')


@pytest.mark.parametrize('output', ['Remv nginx [1.2]', 'Inst nginx [1.2] (1.3 Ubuntu:24.04/noble [amd64])'])
def test_apt_cannot_remove_or_upgrade_existing_packages(provision, monkeypatch, output):
    m = provision
    monkeypatch.setattr(m, 'command', lambda *args: 'release v=24.04,o=Ubuntu,a=noble,n=noble' if args[0].endswith('apt-cache') else output)
    with pytest.raises(ValueError):
        m.apt_changes(['certbot'], {'nginx': '1.2'})


def test_third_party_repositories_are_rejected(provision, monkeypatch):
    m = provision
    monkeypatch.setattr(m, 'command', lambda *args: 'release o=LP-PPA-something,a=noble')
    with pytest.raises(ValueError, match='Ubuntu-origin'):
        m.apt_changes(['nginx'], {})


def test_server_drift_prevents_any_modification(provision, monkeypatch):
    m = provision
    from contextlib import nullcontext
    monkeypatch.setattr(m, 'lock', nullcontext)
    monkeypatch.setattr(m, 'inventory', lambda: {'changed': True})
    with pytest.raises(ValueError, match='Server changed'):
        m.execute({'baseline': baseline(m)})


def test_protocol_rejects_arbitrary_commands_and_unauthenticated_jobs(provision):
    m = provision
    for value in ({'op': 'shell', 'command': 'id'}, {'op': 'apply', 'path': '/tmp/evil'},
                  {'op': 'plan', 'php': '8.3'}, {'op': 'status'}):
        with pytest.raises(ValueError):
            m.dispatch(value)


def test_confirmation_is_one_use_session_bound_and_durable(provision, monkeypatch):
    m = provision
    import time
    with m.db() as connection:
        connection.execute('INSERT INTO sessions VALUES(?,?)', (m.digest('owner'), time.time()+100))
        connection.execute('INSERT INTO sessions VALUES(?,?)', (m.digest('other'), time.time()+100))
    monkeypatch.setattr(m, 'make_plan', lambda php: {'php': php})
    plan = m.dispatch({'op': 'plan', 'capability': 'owner', 'php': '8.3'})
    for cap, text in [('other', m.CONFIRM), ('owner', 'yes')]:
        with pytest.raises(ValueError):
            m.dispatch({'op': 'apply', 'capability': cap, 'plan': plan['id'], 'confirmation': text})
    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr(m.threading, 'Thread', Thread)
    result = m.dispatch({'op': 'apply', 'capability': 'owner', 'plan': plan['id'], 'confirmation': m.CONFIRM})
    with pytest.raises(ValueError):
        m.dispatch({'op': 'apply', 'capability': 'owner', 'plan': plan['id'], 'confirmation': m.CONFIRM})
    with m.db() as connection:
        assert connection.execute('SELECT state FROM jobs WHERE id=?', (result['job'],)).fetchone()[0] == 'running'
        log = str([tuple(row) for row in connection.execute('SELECT * FROM audit')])
        assert 'job-approved' in log and 'capability' not in log


def test_login_throttle_and_no_cleartext_secrets(provision):
    m = provision
    import hashlib
    secret = {'salt': 'aa' * 16, 'hash': hashlib.pbkdf2_hmac('sha256', b'correct-password', bytes.fromhex('aa' * 16), 600000).hex()}
    (m.STATE / 'credentials.json').write_text(json.dumps(secret))
    token = m.authenticate('correct-password')['capability']
    assert len(token) > 32
    for _ in range(5):
        with pytest.raises(ValueError, match='Invalid credentials'):
            m.authenticate('secret-never-logged')
    with pytest.raises(ValueError, match='Too many'):
        m.authenticate('correct-password')
    assert b'secret-never-logged' not in (m.STATE / 'ledger.sqlite').read_bytes()
    assert token.encode() not in (m.STATE / 'ledger.sqlite').read_bytes()
