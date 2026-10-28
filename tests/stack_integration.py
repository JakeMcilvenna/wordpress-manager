"""Real provisioning/security exercise, ONLY in a disposable Ubuntu 24.04 guest.

Run after deploy_integration.py. This intentionally leaves installed stack packages
and private admin resources in the guest so their survival after uninstall can be checked.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import uninstall_sites

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/opt/wordpress-manager')
CONTROL = Path('/var/lib/wordpress-manager-control')


def run(*args, success=True):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=1200)
    if (result.returncode == 0) != success:
        raise AssertionError(f'{args}: {result.stdout}\n{result.stderr}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-system', action='store_true', required=True)
    parser.parse_args()
    assert os.geteuid() == 0 and Path('/run/wm-disposable-test').read_text() == 'disposable Ubuntu deployment test\n'
    assert not BASE.exists()
    # Docker images include a generic policy-rc.d. Remove it only in this guarded test guest;
    # production deliberately refuses to replace an existing policy.
    policy = Path('/usr/sbin/policy-rc.d')
    saved_policy = policy.read_bytes() if policy.exists() else None
    if saved_policy is not None:
        assert saved_policy.strip() == b'#!/bin/sh\nexit 101'
        policy.unlink()
    sentinel = Path('/etc/php/8.5/fpm/pool.d/customer.conf')
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b'; existing externally managed PHP version\n')
    original = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    run('/bin/bash', REPO / 'deploy/install.sh', '--non-interactive', '--trusted-checkout', '--enable-provisioning')
    assert run('stat', '-c', '%a', CONTROL / 'ledger.sqlite').strip() == '600'
    assert 'wm-dashboard' in run('stat', '-c', '%G', '/run/wordpress-manager/control.sock')
    run('runuser', '-u', 'nobody', '--', 'python3', '-c', "import socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/wordpress-manager/control.sock')", success=False)
    rejected = run('runuser', '-u', 'wm-dashboard', '--', 'python3', '-c',
        "import socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/wordpress-manager/control.sock');s.sendall(b'{\"op\":\"shell\",\"command\":\"id\"}\\n');print(s.recv(4096).decode())")
    assert json.loads(rejected)['ok'] is False
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
    def get(path):
        return opener.open('http://127.0.0.1:8080' + path, timeout=30).read().decode()
    def token(page):
        return re.search('name="csrf" value="([a-f0-9]+)"', page).group(1)
    def post(path, values):
        return opener.open('http://127.0.0.1:8080' + path, urllib.parse.urlencode(values).encode(), timeout=30).read().decode()
    page = get('/login')
    password = (CONTROL / 'initial-password').read_text().strip()
    post('/login', {'csrf': token(page), 'username': 'admin', 'password': password})
    page = get('/setup/control')
    page = post('/setup/control', {'csrf': token(page), 'action': 'plan', 'php': '8.3'})
    assert 'Review the installation plan' in page, page
    plan = re.search('name="plan" value="([a-f0-9]+)"', page).group(1)
    assert 'mariadb-server' in page and 'php8.3-fpm' in page
    post('/setup/control', {'csrf': token(page), 'action': 'apply', 'plan': plan, 'confirmation': 'INSTALL MISSING COMPONENTS'})
    with sqlite3.connect(CONTROL / 'ledger.sqlite') as ledger:
        for _ in range(180):
            state, detail = ledger.execute('SELECT state,detail FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()
            if state != 'running':
                break
            time.sleep(2)
        assert state == 'succeeded', detail
        assert ledger.execute("SELECT count(*) FROM audit WHERE action='job-approved'").fetchone()[0] == 1
    print('PASS: authenticated dashboard plan/confirm/job workflow', flush=True)
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == original
    assert '127.0.0.1:8081' in run('ss', '-H', '-ltn', '( sport = :8081 )')
    assert '0.0.0.0:8081' not in run('ss', '-H', '-ltn', '( sport = :8081 )')
    assert 'User=www-data' in Path('/etc/systemd/system/wordpress-manager-dbadmin.service').read_text()
    assert opener.open('http://127.0.0.1:8081/', timeout=5).status == 200
    # No automatic public phpMyAdmin routes were added.
    assert not any('phpmyadmin' in path.read_text().lower() for path in Path('/etc/nginx').rglob('*') if path.is_file())
    tool = BASE / 'current/deploy/provision.py'
    plan = json.loads(run('python3', '-I', tool, 'plan'))
    assert not plan['packages'] and not plan['artifacts']
    run('python3', '-I', tool, 'apply', '--plan', plan['id'], '--non-interactive', '--approve-server-changes')
    with sqlite3.connect(CONTROL / 'ledger.sqlite') as ledger:
        ledger.execute("INSERT INTO jobs VALUES('interrupted-test', 'running', 'test interruption')")
    run('systemctl', 'restart', 'wordpress-manager-provisioner')
    with sqlite3.connect(CONTROL / 'ledger.sqlite') as ledger:
        for _ in range(20):
            status = ledger.execute("SELECT state FROM jobs WHERE id='interrupted-test'").fetchone()[0]
            if status == 'interrupted':
                break
            time.sleep(.2)
        assert status == 'interrupted'
    print('PASS: localhost phpMyAdmin, preserved PHP config, repeat no-op provisioning', flush=True)
    before = uninstall_sites.prepare()
    run('/bin/bash', REPO / 'deploy/uninstall.sh', '--dry-run')
    assert uninstall_sites.snapshot() == before
    run('/bin/bash', REPO / 'deploy/uninstall.sh', '--yes')
    assert (BASE / 'uninstalled.json').exists() and (CONTROL / 'ledger.sqlite').exists()
    assert not Path('/etc/systemd/system/wordpress-manager-provisioner.service').exists()
    assert not Path('/etc/systemd/system/wordpress-manager.service').exists()
    assert not Path('/run/wordpress-manager/control.sock').exists()
    assert not run('ss', '-H', '-ltn', '( sport = :8080 )').strip()
    assert uninstall_sites.snapshot() == before
    run('/bin/bash', REPO / 'deploy/uninstall.sh', '--yes', '--purge-recovery')
    run('/bin/bash', REPO / 'deploy/uninstall.sh', '--yes', '--purge-recovery')
    assert not BASE.exists() and not CONTROL.exists() and not Path('/etc/wordpress-manager').exists()
    assert uninstall_sites.snapshot() == before
    assert sentinel.exists() and Path('/usr/local/bin/wp').is_file()
    assert Path('/var/lib/wordpress-manager-stack/owned.json').exists()
    assert opener.open('http://127.0.0.1:8081/', timeout=5).status == 200
    if saved_policy is not None:
        policy.write_bytes(saved_policy)
        policy.chmod(0o755)
    print('PASS: dry run, retained-data uninstall and purge preserve live production/staging WordPress, MariaDB users/data, site account/pool, TLS, backups/schedule, unchanged service PIDs/packages and phpMyAdmin', flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        subprocess.run(['journalctl', '-u', 'wordpress-manager-provisioner', '-u', 'wordpress-manager-dbadmin', '-u', 'wordpress-manager-dbadmin-php', '-n', '100', '--no-pager'])
        raise
