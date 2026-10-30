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
    print("PASS: disposable Ubuntu guest installed two isolated native WordPress "
          "sites, preserved them across service restarts and rejected duplicate "
          "or forged plans", flush=True)


if __name__ == "__main__":
    main()
