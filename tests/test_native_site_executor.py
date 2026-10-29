"""Native Ubuntu website executor regression tests; no guest privileges required."""
from pathlib import Path
import re

import pytest

from deploy import ubuntu_site_executor as native
from wordpress_manager.site_operations import WebsiteSpec, WebsiteService, WebsiteOperationUnavailable


def spec(domain="one.test", php="8.3"):
    return WebsiteSpec.from_fields("Native test site", "admin@one.test", domain, php)


def test_deterministic_opaque_identity_and_fixed_resource_ownership():
    first = native.make_plan(spec())
    second = native.make_plan(spec())
    assert first == second
    assert re.fullmatch(r"wm-[0-9a-f]{12}", first.site_id)
    assert first.spec.domain == "one.test"
    assert first.backend == "ubuntu-disposable"
    assert first.confirmation == "CREATE NATIVE TEST SITE"
    assert first.preview_url == "http://one.test:18080/"
    assert any("/etc/nginx/sites-available/" in entry for entry in first.resources)
    assert any("/etc/php/8.3/fpm/pool.d/" in entry for entry in first.resources)
    assert any("mariadb-database:wm_" in entry for entry in first.resources)
    assert any("linux-user:wms" in entry for entry in first.resources)
    assert not any("password" in entry for entry in first.resources)
    assert native.make_plan(spec("two.test")).site_id != first.site_id


def test_only_disposable_test_domains_and_known_php_versions():
    for domain in ("example.dev", "real-site.org", "local.invalid"):
        with pytest.raises(ValueError, match="end in .test"):
            native.make_plan(spec(domain))
    for php in ("5.6", "8.6", "7.4"):
        with pytest.raises(ValueError, match="unsupported"):
            native.make_plan(spec(php=php))
    with pytest.raises(ValueError):
        native.owned_paths("../elsewhere", "8.3")


def test_generated_native_configs_use_dedicated_site_identity_and_socket():
    plan = native.make_plan(spec())
    nginx = native.nginx_config(plan)
    fpm = native.fpm_config(plan)
    assert "listen 127.0.0.1:18080;" in nginx
    assert "server_name one.test;" in nginx
    assert "root " + str(native.ROOT / plan.site_id) in nginx
    assert "location = /wp-config.php" in nginx
    assert "fastcgi_pass unix:/run/php/" + plan.site_id + ".sock" in nginx
    assert "pm.max_children = 2" in fpm
    assert "listen.owner = www-data" in fpm
    assert "user = wms" in fpm
    assert "group = wms" in fpm
    assert "0.0.0.0" not in nginx


def test_wordpress_credentials_are_only_embedded_in_private_config():
    value = native._wordpress_config("wm_1234567890", "some-secret-value")
    assert "define('DB_PASSWORD', 'some-secret-value')" in value
    assert "AUTH_KEY" in value and "NONCE_SALT" in value
    assert "wp-settings.php" in value
    assert "shell_exec" not in value


def test_executor_refuses_every_local_and_production_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    monkeypatch.setattr(native, "DISPOSABLE_MARKER", tmp_path / "missing-marker")
    with pytest.raises(PermissionError, match="outside the disposable"):
        native.disposable_only()
    plan = native.make_plan(spec())
    with pytest.raises(PermissionError):
        native.create(plan, native.CONFIRMATION)
    with pytest.raises(PermissionError):
        native.read_record(plan.site_id)
    marker = tmp_path / "missing-marker"
    marker.write_text("something else\n")
    with pytest.raises(PermissionError, match="marker"):
        native.disposable_only()
    marker.write_text(native.DISPOSABLE_TEXT)
    # Windows and Linux local host /etc/os-release must still be Ubuntu 24.04.
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    with pytest.raises(ValueError, match="confirmation"):
        native.create(plan, "INSTALL EVERYTHING")


def test_native_backend_fails_closed_and_has_shared_contract(monkeypatch, tmp_path):
    backend = native.DisposableUbuntuWebsiteBackend()
    monkeypatch.setattr(native.os, "geteuid", lambda: 0)
    monkeypatch.setattr(native, "DISPOSABLE_MARKER", tmp_path / "missing-marker")
    with pytest.raises(PermissionError):
        backend.inspect()
    with pytest.raises(PermissionError):
        backend.plan(spec())
    for action in (lambda: backend.suspend("wm-000000000000"),
                   lambda: backend.delete("wm-000000000000", "DELETE")):
        with pytest.raises(WebsiteOperationUnavailable):
            action()
    with pytest.raises(PermissionError):
        WebsiteService(backend).plan("Example", "admin@example.test",
                                      "example.test", "8.3")


def test_ownership_journal_has_no_automatic_destructive_cleanup():
    code = (Path(__file__).resolve().parents[1] /
            "deploy/ubuntu_site_executor.py").read_text()
    assert 'record["state"] = "needs-review"' in code
    assert "def _atomic_json" in code
    assert "os.O_NOFOLLOW" in code
    assert "os.symlink(paths[\"nginx\"], paths[\"enabled\"])" in code
    assert "rm -rf" not in code and "shutil.rmtree" not in code
    assert "/run/wm-disposable-test" in code
    assert "subprocess.run" in code and "shell=True" not in code
    assert "NotImplementedError" in code
    # The production adapter remains explicitly disabled.
    prod = (Path(__file__).resolve().parents[1] /
            "wordpress_manager/ubuntu_sites.py").read_text()
    assert "WebsiteOperationUnavailable" in prod
    assert "def _disabled" in prod
