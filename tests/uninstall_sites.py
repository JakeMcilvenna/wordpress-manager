"""Live WordPress preservation assertions for the guarded disposable stack guest.

Called only by stack_integration.py after its explicit disposable-system guard.
No SSH, no host mounts, no public listeners. These resources belong to the test
customer, independently of the panel, and are deliberately left in the guest.
"""
import hashlib
import json
from pathlib import Path
import ssl
import subprocess
import time
import urllib.error
import urllib.request


def run(*args):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, f'{args}: {result.stderr}'
    return result.stdout


def wp(path, *args):
    return run('/usr/local/bin/wp', '--allow-root', '--path=' + str(path), *args)


def prepare():
    assert Path('/run/wm-disposable-test').read_text() == 'disposable Ubuntu deployment test\n'
    run('useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent',
        '--shell', '/usr/sbin/nologin', 'wm-customer')
    root = Path('/var/www/wm-uninstall-customer')
    root.mkdir()
    production = root / 'production'
    wp(production, 'core', 'download', '--version=6.8.3')
    config = Path('/etc/nginx/sites-enabled/wm-uninstall-customer.conf')
    cert = Path('/etc/letsencrypt/live/wm-uninstall.test')
    cert.mkdir(parents=True)
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
        '-keyout', cert / 'privkey.pem', '-out', cert / 'fullchain.pem',
        '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1')
    renewal = Path('/etc/letsencrypt/renewal/wm-uninstall.test.conf')
    renewal.parent.mkdir(exist_ok=True)
    renewal.write_text('# customer renewal configuration sentinel\n')
    pool = Path('/etc/php/8.3/fpm/pool.d/wm-customer.conf')
    pool.write_text('''[wm-customer]
user = wm-customer
group = wm-customer
listen = /run/php/wm-customer.sock
listen.owner = www-data
listen.group = www-data
listen.mode = 0660
pm = ondemand
pm.max_children = 3
''')
    nginx = []
    for name, port in (('production', 8090), ('staging', 8091)):
        path = root / name
        if name == 'staging':
            import shutil
            shutil.copytree(production, path, ignore=shutil.ignore_patterns('wp-config.php'))
        database = 'wm_uninstall_' + name
        run('mariadb', '-e', f"CREATE DATABASE {database}; CREATE USER '{database}'@'localhost' IDENTIFIED BY 'disposable-test-password'; GRANT ALL ON {database}.* TO '{database}'@'localhost';")
        wp(path, 'config', 'create', '--dbname=' + database, '--dbuser=' + database,
           '--dbpass=disposable-test-password', '--dbhost=localhost')
        wp(path, 'config', 'set', 'DISABLE_WP_CRON', 'true', '--raw')
        wp(path, 'core', 'install', '--url=http://127.0.0.1:' + str(port),
           '--title=Customer ' + name, '--admin_user=testadmin',
           '--admin_password=disposable-admin-password', '--admin_email=test@example.test', '--skip-email')
        if name == 'production':
            wp(path, 'config', 'set', 'WP_HOME',
               "(!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off' ? 'https://127.0.0.1:8443' : 'http://127.0.0.1:8090')", '--raw')
            wp(path, 'config', 'set', 'WP_SITEURL', 'WP_HOME', '--raw')
        nginx.append(f'''server {{
listen 127.0.0.1:{port};
{'listen 127.0.0.1:8443 ssl;' if name == 'production' else ''}
ssl_certificate {cert}/fullchain.pem;
ssl_certificate_key {cert}/privkey.pem;
server_name _;
root {path};
index index.php;
location / {{ try_files $uri $uri/ /index.php?$args; }}
location ~ [.]php$ {{ include fastcgi_params; fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name; fastcgi_param HTTPS $https if_not_empty; fastcgi_pass unix:/run/php/wm-customer.sock; }}
}}
''')
    config.write_text('\n'.join(nginx))
    run('chown', '-R', 'wm-customer:wm-customer', root)
    backups = Path('/var/backups/wm-customer')
    backups.mkdir()
    (backups / 'production.sql').write_text(run('mariadb-dump', 'wm_uninstall_production'))
    run('tar', '-czf', backups / 'sites.tar.gz', '-C', root, '.')
    schedule = Path('/etc/cron.d/wm-customer-backup')
    schedule.write_text('17 3 * * * root /usr/bin/mariadb-dump wm_uninstall_production > /var/backups/wm-customer/nightly.sql\n')
    run('nginx', '-t')
    run('systemctl', 'restart', 'php8.3-fpm')
    run('systemctl', 'reload', 'nginx')
    # Reload acknowledgement precedes new worker readiness. Retry only during
    # fixture setup; preservation checks after removal must pass immediately.
    for attempt in range(50):
        try:
            return snapshot()
        except urllib.error.URLError:
            if attempt == 49:
                raise
            time.sleep(.2)


def snapshot():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(
            cafile='/etc/letsencrypt/live/wm-uninstall.test/fullchain.pem')))
    for port, scheme in ((8090, 'http'), (8091, 'http'), (8443, 'https')):
        with opener.open(f'{scheme}://127.0.0.1:{port}/', timeout=10) as response:
            assert response.status == 200 and b'Customer' in response.read()
    # Preserve process identity too: no shared-service restart is acceptable.
    units = ('nginx', 'mariadb', 'php8.3-fpm', 'wordpress-manager-dbadmin', 'wordpress-manager-dbadmin-php')
    services = {}
    for unit in units:
        assert run('systemctl', 'is-active', unit).strip() == 'active'
        services[unit] = run('systemctl', 'show', '--property=MainPID', '--value', unit)
    files = {}
    for directory in ('/var/www/wm-uninstall-customer', '/var/backups/wm-customer', '/etc/letsencrypt'):
        for path in Path(directory).rglob('*'):
            if path.is_file():
                info = path.stat()
                files[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_uid, info.st_gid, info.st_mode)
    for name in ('/etc/nginx/sites-enabled/wm-uninstall-customer.conf',
                 '/etc/php/8.3/fpm/pool.d/wm-customer.conf', '/etc/cron.d/wm-customer-backup'):
        files[name] = hashlib.sha256(Path(name).read_bytes()).hexdigest()
    databases = {}
    for name in ('production', 'staging'):
        path = Path('/var/www/wm-uninstall-customer') / name
        assert wp(path, 'core', 'is-installed') == ''
        # HTTP can update WordPress cron timestamps. Check real customer rows,
        # DB users/grants and login ability rather than a volatile whole DB dump.
        databases[name] = wp(path, 'db', 'query', 'SELECT ID,post_title,post_content FROM wp_posts ORDER BY ID', '--skip-column-names')
        databases[name + '_grants'] = run('mariadb', '-N', '-e', f"SHOW GRANTS FOR 'wm_uninstall_{name}'@'localhost'")
    accounts = run('getent', 'passwd', 'wm-customer')
    packages = run('dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n')
    assert opener.open('http://127.0.0.1:8081/', timeout=10).status == 200
    return json.dumps([files, services, databases, accounts, packages], sort_keys=True)
