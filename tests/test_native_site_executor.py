"""Native Ubuntu website executor regression tests; no guest privileges required."""
from pathlib import Path
import json
import re

import pytest

from deploy import ubuntu_site_executor as native
# The native executor loads the same stdlib-only contract without Flask.
WebsiteSpec = native.WebsiteSpec
WebsiteService = native.WebsiteService
WebsiteOperationUnavailable = native.WebsiteOperationUnavailable


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


@pytest.mark.parametrize("state", ["provisioning", "needs-review"])
def test_incomplete_native_ledger_blocks_other_site_plans(monkeypatch, tmp_path, state):
    """An interrupted site is still owned; never silently hide its record."""
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    site_id = native.site_id_for("partial.test")
    (tmp_path / (site_id + ".json")).write_text(json.dumps({
        "site_id": site_id, "domain": "partial.test",
        "title": "Interrupted site", "state": state,
    }))
    backend = native.DisposableUbuntuWebsiteBackend()
    with pytest.raises(ValueError, match="Incomplete native site ledger"):
        backend.inspect()
    with pytest.raises(ValueError, match="Incomplete native site ledger"):
        WebsiteService(backend).plan(
            "Unrelated", "admin@other.test", "other.test", "8.3"
        )


def test_unknown_or_symlinked_native_ledger_blocks_planning(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    (tmp_path / "unexpected.json").write_text("{}")
    backend = native.DisposableUbuntuWebsiteBackend()
    with pytest.raises(ValueError, match="unrecognized"):
        backend.list_sites()
    (tmp_path / "unexpected.json").unlink()
    (tmp_path / "wm-000000000000.json").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="Unsafe"):
        backend.list_sites()


def test_complete_native_ledger_matches_deterministic_owned_resources(monkeypatch, tmp_path):
    """Only a complete, server-derived ownership journal is healthy."""
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    plan = native.make_plan(spec())
    record = {
        "site_id": plan.site_id, "domain": plan.spec.domain,
        "php": plan.spec.php, "title": plan.spec.title,
        "resources": list(plan.resources), "state": "created",
    }
    path = tmp_path / (plan.site_id + ".json")
    path.write_text(json.dumps(record))
    backend = native.DisposableUbuntuWebsiteBackend()
    assert backend.list_sites()[0]["domain"] == "one.test"
    assert backend.inspect()["sites"] == 1


@pytest.mark.parametrize("mutation", [
    lambda record: record.pop("resources"),
    lambda record: record.update(resources=[]),
    lambda record: record.update(resources=["/etc/passwd"]),
    lambda record: record.update(domain="other.test"),
    lambda record: record.update(domain="ONE.TEST"),
    lambda record: record.update(php="8.4"),
    lambda record: record.update(php="9.9"),
    lambda record: record.update(title=""),
    lambda record: record.update(site_id=native.site_id_for("other.test")),
    lambda record: record.update(unexpected="unreviewed"),
])
def test_tampered_created_ledger_cannot_free_or_adopt_resources(
    monkeypatch, tmp_path, mutation
):
    """A forged 'created' record must not permit unrelated site planning."""
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    plan = native.make_plan(spec())
    record = {
        "site_id": plan.site_id, "domain": plan.spec.domain,
        "php": plan.spec.php, "title": plan.spec.title,
        "resources": list(plan.resources), "state": "created",
    }
    mutation(record)
    (tmp_path / (plan.site_id + ".json")).write_text(json.dumps(record))
    backend = native.DisposableUbuntuWebsiteBackend()
    with pytest.raises(ValueError, match="ownership|identity"):
        backend.list_sites()
    with pytest.raises(ValueError, match="ownership|identity"):
        WebsiteService(backend).plan(
            "Unrelated", "admin@other.test", "other.test", "8.3"
        )


@pytest.mark.parametrize(("config", "claimed"), [
    ("server { server_name other.test one.test; }", True),
    ("server_name other.test\n one.test;", True),
    ("server_name *.test;", True),
    ("server_name .one.test;", True),
    ("server_name one.test;", True),
    ("server_name someone.test;", False),
    ("server_name other.test; # server_name one.test;", False),
])
def test_nginx_aliases_and_wildcards_cannot_hide_domain_ownership(config, claimed):
    assert native._nginx_claims_domain(config, "one.test") is claimed


@pytest.mark.parametrize("entry", [
    ".orphaned-record", "wm-000000000000.json.bak",
    "unexpected.txt", "subdirectory",
])
def test_native_inventory_rejects_unrecognized_entries(monkeypatch, tmp_path, entry):
    """A partial or renamed journal must not silently disappear from inventory."""
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    path = tmp_path / entry
    if entry == "subdirectory":
        path.mkdir()
    else:
        path.write_text("unreviewed ownership")
    with pytest.raises(ValueError, match="unrecognized"):
        native.DisposableUbuntuWebsiteBackend().list_sites()


def test_non_object_native_ledger_is_not_trusted(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    site_id = native.site_id_for("one.test")
    (tmp_path / (site_id + ".json")).write_text("[]")
    with pytest.raises(ValueError, match="identity"):
        native.DisposableUbuntuWebsiteBackend().list_sites()


@pytest.mark.parametrize(("state", "expected"), [
    ("provisioning", "Incomplete native site ledger"),
    ("needs-review", "Incomplete native site ledger"),
    ("created", "Incomplete native site ownership record"),
])
def test_direct_native_create_refuses_unresolved_foreign_site_before_writes(
    monkeypatch, tmp_path, state, expected
):
    """The root executor cannot bypass shared-service inventory checks.

    A separate failed installation may own unknown resources even when the
    new site's deterministic paths and domain do not overlap.
    """
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    foreign_id = native.site_id_for("foreign.test")
    (tmp_path / (foreign_id + ".json")).write_text(json.dumps({
        "site_id": foreign_id, "domain": "foreign.test",
        "title": "Unresolved site", "state": state,
    }))
    def unexpected_write(*args, **kwargs):
        raise AssertionError("Native create must not start filesystem writes")
    monkeypatch.setattr(native, "_directory", unexpected_write)
    new_plan = native.make_plan(spec("new.test"))
    with pytest.raises(ValueError, match=expected):
        native.create(new_plan, native.CONFIRMATION)
    assert not (tmp_path / (new_plan.site_id + ".json")).exists()

@pytest.mark.parametrize(("directory", "filename", "config"), [
    ("available", "unmanaged.conf", "server { server_name OTHER.TEST One.Test; }"),
    ("enabled", "extensionless-vhost", "server { server_name OTHER.TEST ONE.TEST; }"),
    ("conf_d", "unmanaged.conf", "server { server_name *.test; }"),
])
def test_native_nginx_preflight_checks_all_config_sources(
    monkeypatch, tmp_path, directory, filename, config
):
    """Enabled vhosts can have no .conf suffix and names are case-insensitive."""
    roots = {name: tmp_path / name for name in ("available", "enabled", "conf_d")}
    for root in roots.values():
        root.mkdir()
    monkeypatch.setattr(native, "NGINX_AVAILABLE", roots["available"])
    monkeypatch.setattr(native, "NGINX_ENABLED", roots["enabled"])
    monkeypatch.setattr(native, "NGINX_CONF_D", roots["conf_d"])
    external = tmp_path / "external-vhost"
    external.write_text(config)
    (roots[directory] / filename).symlink_to(external)
    with pytest.raises(ValueError, match="already configured"):
        native._check_nginx_domain_available("one.test")


def test_native_nginx_preflight_rejects_broken_active_link(monkeypatch, tmp_path):
    available, enabled, conf_d = (tmp_path / name for name in ("available", "enabled", "conf_d"))
    for root in (available, enabled, conf_d):
        root.mkdir()
    monkeypatch.setattr(native, "NGINX_AVAILABLE", available)
    monkeypatch.setattr(native, "NGINX_ENABLED", enabled)
    monkeypatch.setattr(native, "NGINX_CONF_D", conf_d)
    (enabled / "orphaned-vhost").symlink_to(tmp_path / "missing-vhost")
    with pytest.raises(ValueError, match="Unresolvable Nginx configuration link"):
        native._check_nginx_domain_available("new.test")


def test_native_nginx_preflight_accepts_unrelated_enabled_vhost(monkeypatch, tmp_path):
    available, enabled, conf_d = (tmp_path / name for name in ("available", "enabled", "conf_d"))
    for root in (available, enabled, conf_d):
        root.mkdir()
    monkeypatch.setattr(native, "NGINX_AVAILABLE", available)
    monkeypatch.setattr(native, "NGINX_ENABLED", enabled)
    monkeypatch.setattr(native, "NGINX_CONF_D", conf_d)
    (enabled / "unrelated").write_text("server { server_name unrelated.test; }")
    native._check_nginx_domain_available("one.test")

