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
from wordpress_manager.site_operations import WebsiteService  # noqa: E402

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

    listed = service.list_sites()
    assert len(listed) == 1 and listed[0]["domain"] == domain
    for call in (lambda: service.create(plan, native.CONFIRMATION),
                 lambda: service.plan("Duplicate", "admin@native-one.test",
                                      domain, "8.3")):
        try:
            call()
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate native install was not refused")
    assert service.list_sites() == listed
    assert native.read_record(plan["site_id"])["state"] == "created"
    check_site(domain)
    print("PASS: disposable Ubuntu guest installed isolated native WordPress, "
          "preserved ownership and rejected forged/duplicate plans", flush=True)


if __name__ == "__main__":
    main()
