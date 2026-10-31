"""Run real native Nginx, PHP-FPM, MariaDB and WordPress provisioning in CI guest.

NEVER invoke on the live VPS, Windows host or an existing Ubuntu server.
It is called only after the guarded stack integration installs real packages
inside the disposable Ubuntu 24.04 systemd container.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deploy import ubuntu_site_executor as native  # noqa: E402
WebsiteService = native.WebsiteService  # shared stdlib-only contract, no Flask dependency

def check_site(domain):
    connection = http.client.HTTPConnection("127.0.0.1", native.PORT, timeout=10)
    try:
        connection.request("GET", "/", headers={"Host": domain + ":" + str(native.PORT)})
        response = connection.getresponse()
        body = response.read(512 * 1024)
        assert response.status == 200, f"Unexpected native guest HTTP status {response.status}"
        assert b"WordPress" in body or b"Native test" in body
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-system", action="store_true", required=True)
    parser.parse_args()
    native.disposable_only()
    backend = native.DisposableUbuntuWebsiteBackend()
    service = WebsiteService(backend)
    assert service.list_sites() == []
    # The root provisioner uses UMask=0077, but site users must be able to
    # execute the verified WP-CLI Phar without being able to modify it.
    wpcli = Path("/usr/local/bin/wp")
    assert wpcli.stat().st_uid == 0
    assert wpcli.stat().st_mode & 0o777 == 0o755
    name, domain = "Native test WordPress", "native-one.test"
    plan = service.plan(name, "admin@native-one.test", domain, "8.3")
    assert plan["confirmation"] == native.CONFIRMATION
    assert "127.0.0.1:8092" not in str(plan)
    assert plan["backend"] == "ubuntu-disposable"

    try:
        service.create(plan, "YES")
    except ValueError:
        pass
    else:
        raise AssertionError("Wrong native confirmation accepted")
    assert not native.STATE.exists(), "Confirmation failure mutated host resources"

    forged = {**plan, "resources": ["/etc/passwd"]}
    try:
        service.create(forged, native.CONFIRMATION)
    except ValueError:
        pass
    else:
        raise AssertionError("Forged resource manifest accepted")
    assert not native.STATE.exists(), "Tampered plan mutated host resources"

    result = service.create(plan, native.CONFIRMATION)
    assert result["installed"] is True and result["domain"] == domain
    assert result["url"] == "http://native-one.test:18080/"
    assert result["password"] and len(result["password"]) >= 30
    # Do not print, persist or log the generated administrator password.

    record = native.read_record(plan["site_id"])
    assert record["state"] == "created" and record["domain"] == domain
    assert all("password" not in key for key in record)
    account = "wms" + plan["site_id"][3:13]
    db = "wm_" + plan["site_id"][3:13]
    def command(*args):
        return subprocess.check_output(list(args), text=True, timeout=60).strip()
    assert command("/usr/sbin/runuser", "-u", account, "--",
                   "/usr/local/bin/wp", "core", "is-installed",
                   "--path=" + str(native.ROOT / plan["site_id"])) == ""
    assert command("/usr/bin/mariadb", "-N", "-B", "-e",
                   "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
                   "WHERE SCHEMA_NAME='" + db + "';") == db
    assert (native.ROOT / plan["site_id"] / "wp-config.php").stat().st_mode & 0o077 == 0o040
    assert (native.ROOT / plan["site_id"] / "index.php").is_file()
    assert (native.PHP_ROOT / "8.3/fpm/pool.d" / (plan["site_id"] + ".conf")).is_file()
    check_site(domain)

    # Real multi-site integration: the same guarded Ubuntu executor must
    # provision two independent WordPress sites, not a second Docker backend.
    first_record = native.read_record(plan["site_id"])
    second_domain = "native-two.test"
    second_plan = service.plan(
        "Second native WordPress", "admin@native-two.test", second_domain, "8.3"
    )
    assert second_plan["site_id"] != plan["site_id"]
    assert set(second_plan["resources"]).isdisjoint(plan["resources"])
    second_result = service.create(second_plan, native.CONFIRMATION)
    assert second_result["installed"] is True
    assert second_result["domain"] == second_domain
    assert second_result["password"] and len(second_result["password"]) >= 30
    assert native.read_record(plan["site_id"]) == first_record
    assert native.read_record(second_plan["site_id"])["state"] == "created"
    check_site(domain)
    check_site(second_domain)

    for current_plan in (plan, second_plan):
        current_id = current_plan["site_id"]
        account = "wms" + current_id[3:13]
        database = "wm_" + current_id[3:13]
        assert command("/usr/sbin/runuser", "-u", account, "--",
                       "/usr/local/bin/wp", "core", "is-installed",
                       "--path=" + str(native.ROOT / current_id)) == ""
        assert command("/usr/bin/mariadb", "-N", "-B", "-e",
                       "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
                       "WHERE SCHEMA_NAME='" + database + "';") == database
        assert (native.PHP_ROOT / "8.3/fpm/pool.d" / (current_id + ".conf")).is_file()

    # Isolation is more than distinct names: prove that the real site Linux
    # accounts cannot read each other's database credentials, write to the
    # other WordPress tree, or query the other MariaDB schema. WP-CLI uses
    # each site's own wp-config.php credentials; no secrets enter argv/logs.
    for own, foreign in ((plan, second_plan), (second_plan, plan)):
        own_id, foreign_id = own["site_id"], foreign["site_id"]
        own_user = "wms" + own_id[3:13]
        own_db = "wm_" + own_id[3:13]
        foreign_db = "wm_" + foreign_id[3:13]
        own_path = str(native.ROOT / own_id)
        foreign_path = str(native.ROOT / foreign_id)
        assert command(
            "/usr/sbin/runuser", "-u", own_user, "--",
            "/usr/local/bin/wp", "db", "query", "SELECT DATABASE();",
            "--skip-column-names", "--path=" + own_path,
        ) == own_db
        for args in (
            ("/usr/bin/test", "-r", foreign_path + "/wp-config.php"),
            ("/usr/bin/test", "-w", foreign_path),
            ("/usr/local/bin/wp", "db", "query",
             "SELECT COUNT(*) FROM `" + foreign_db + "`.wp_options;",
             "--skip-column-names", "--path=" + own_path),
        ):
            denied = subprocess.run(
                ["/usr/sbin/runuser", "-u", own_user, "--", *args],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60, check=False,
            )
            assert denied.returncode != 0, (
                "Cross-site filesystem or database access was allowed"
            )

    # A service restart must not lose either site or its independent routing.
    subprocess.check_call(["/usr/bin/systemctl", "restart", "nginx"], timeout=60)
    subprocess.check_call(["/usr/bin/systemctl", "restart", "php8.3-fpm"], timeout=60)
    restarted = WebsiteService(native.DisposableUbuntuWebsiteBackend())
    listed = restarted.list_sites()
    assert {site["domain"] for site in listed} == {domain, second_domain}
    assert restarted.inspect()["sites"] == 2
    for current_plan in (plan, second_plan):
        try:
            restarted.create(current_plan, native.CONFIRMATION)
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate native install was not refused")
    for existing_domain in (domain, second_domain):
        try:
            restarted.plan("Duplicate", "admin@" + existing_domain,
                           existing_domain.upper(), "8.3")
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate native domain was not refused")
    assert restarted.list_sites() == listed
    assert native.read_record(plan["site_id"]) == first_record
    check_site(domain)
    check_site(second_domain)
    # An unmanaged Nginx alias must not be adopted even when the intended
    # domain is not the first name in an existing server_name directive.
    alias_plan = restarted.plan(
        "Reserved alias", "admin@reserved.test", "reserved.test", "8.3"
    )
    alias_config = native.NGINX_AVAILABLE / "wm-unmanaged-alias.conf"
    alias_config.write_text(
        "server {\n  listen 127.0.0.1:18080;\n"
        "  server_name other.test reserved.test;\n}\n"
    )
    try:
        try:
            restarted.create(alias_plan, native.CONFIRMATION)
        except ValueError as exc:
            assert "already configured" in str(exc)
        else:
            raise AssertionError("Existing Nginx alias was adopted")
        assert native.read_record(alias_plan["site_id"]) is None
    finally:
        alias_config.unlink()

    # An effective sites-enabled/* vhost can be an extensionless symlink to
    # a config outside sites-available. Its aliases are still owned by Nginx.
    enabled_plan = restarted.plan(
        "Enabled alias", "admin@enabled-alias.test", "enabled-alias.test", "8.3"
    )
    enabled_link = native.NGINX_ENABLED / "wm-unmanaged-external"
    with tempfile.TemporaryDirectory(prefix="wm-unmanaged-nginx-") as temporary:
        external_config = Path(temporary) / "external-vhost"
        external_config.write_text(
            "server { listen 127.0.0.1:18080; "
            "server_name OTHER.TEST ENABLED-ALIAS.TEST; }\n"
        )
        enabled_link.symlink_to(external_config)
        try:
            try:
                restarted.create(enabled_plan, native.CONFIRMATION)
            except ValueError as exc:
                assert "already configured" in str(exc)
            else:
                raise AssertionError("Enabled Nginx symlink alias was adopted")
            assert native.read_record(enabled_plan["site_id"]) is None
        finally:
            enabled_link.unlink()
    check_site(domain)
    check_site(second_domain)

    # Simulate a third site's interrupted ownership journal on the real guest.
    # Both service planning and a direct root executor call must fail closed,
    # without touching either of the two healthy WordPress installations.
    third_plan = restarted.plan(
        "Third site", "admin@third.test", "third.test", "8.3"
    )
    interrupted_id = native.site_id_for("interrupted.test")
    interrupted_path = native.RECORDS / (interrupted_id + ".json")
    interrupted_path.write_text(json.dumps({
        "site_id": interrupted_id, "domain": "interrupted.test",
        "title": "Interrupted site", "state": "needs-review",
    }))
    try:
        try:
            restarted.plan("Fourth site", "admin@fourth.test", "fourth.test", "8.3")
        except ValueError:
            pass
        else:
            raise AssertionError("Interrupted foreign site was ignored by planner")
        try:
            native.create(native.make_plan(native.WebsiteSpec.from_fields(
                third_plan["title"], third_plan["email"],
                third_plan["domain"], third_plan["php"],
            )), native.CONFIRMATION)
        except ValueError:
            pass
        else:
            raise AssertionError("Direct executor ignored interrupted foreign site")
        assert native.read_record(third_plan["site_id"]) is None
        check_site(domain)
        check_site(second_domain)
    finally:
        interrupted_path.unlink()

    # Inject a real provisioning failure after the third site's Linux account
    # and database have been created, but before WordPress download. The
    # executor must retain its ownership journal rather than cleaning up
    # partially owned resources or allowing an unrelated fourth install.
    failed_plan = restarted.plan(
        "Failure recovery", "admin@failure.test", "failure.test", "8.3"
    )
    original_run = native._run
    def fail_wordpress_download(*args, **kwargs):
        if (len(args) >= 7 and args[0] == "/usr/sbin/runuser"
                and args[4:7] == ("/usr/local/bin/wp", "core", "download")):
            raise RuntimeError("Injected disposable WordPress download failure")
        return original_run(*args, **kwargs)

    native._run = fail_wordpress_download
    try:
        try:
            restarted.create(failed_plan, native.CONFIRMATION)
        except RuntimeError as exc:
            assert "Injected disposable" in str(exc)
        else:
            raise AssertionError("Injected third-site provisioning failure was ignored")
    finally:
        native._run = original_run

    failed_record = native.read_record(failed_plan["site_id"])
    assert failed_record["state"] == "needs-review"
    assert failed_record["resources"] == failed_plan["resources"]
    assert command("/usr/bin/id", "-u", "wms" + failed_plan["site_id"][3:13])
    failed_database = "wm_" + failed_plan["site_id"][3:13]
    assert command(
        "/usr/bin/mariadb", "-N", "-B", "-e",
        "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
        "WHERE SCHEMA_NAME='" + failed_database + "';"
    ) == failed_database
    for action in (
        lambda: WebsiteService(native.DisposableUbuntuWebsiteBackend()).plan(
            "After failure", "admin@after-failure.test", "after-failure.test", "8.3"
        ),
        lambda: native.create(native.make_plan(native.WebsiteSpec.from_fields(
            "Direct after failure", "admin@direct-after.test",
            "direct-after.test", "8.3"
        )), native.CONFIRMATION),
    ):
        try:
            action()
        except ValueError as exc:
            assert "Incomplete native site ledger" in str(exc)
        else:
            raise AssertionError("Incomplete third-site ownership did not block creation")
    assert native.read_record(plan["site_id"]) == first_record
    assert native.read_record(second_plan["site_id"])["state"] == "created"
    check_site(domain)
    check_site(second_domain)

    print("PASS: disposable Ubuntu guest installed two isolated native WordPress "
          "sites, preserved them across restarts and failed provisioning, and rejected duplicate "
          "or forged plans", flush=True)


if __name__ == "__main__":
    main()
