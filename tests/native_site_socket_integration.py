"""Exercise the real authenticated Unix socket as the unprivileged dashboard user.

This script is invoked by stack_integration.py only inside an isolated Ubuntu
24.04 systemd Docker guest. The root test runner passes the disposable
administrator login password via stdin, never a process argument or log.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import time

SOCKET = "/run/wordpress-manager/control.sock"
DOMAIN = "native-two.test"
FIRST_DOMAIN = "native-one.test"
PORT = 18080


def call(message, *, allowed=True):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(35)
        connection.connect(SOCKET)
        connection.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())
        response = b""
        while not response.endswith(b"\n") and len(response) < 65536:
            chunk = connection.recv(65536 - len(response))
            if not chunk:
                break
            response += chunk
    payload = json.loads(response)
    if allowed:
        assert payload["ok"], f"{message.get('op')}: {payload.get('error')}"
        return payload["result"]
    assert payload["ok"] is False, "Unexpectedly approved: " + str(message.get("op"))
    return payload["error"]


def website_ready(domain):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        connection.request("GET", "/", headers={"Host": f"{domain}:{PORT}"})
        response = connection.getresponse()
        body = response.read(512 * 1024)
        assert response.status == 200 and (b"WordPress" in body or b"Native test" in body)
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-system", action="store_true", required=True)
    parser.parse_args()
    assert os.geteuid() != 0, "Must use unprivileged dashboard identity"
    assert Path("/run/wm-disposable-test").read_text() == "disposable Ubuntu deployment test\n"
    assert Path("/.dockerenv").is_file()
    password = sys.stdin.readline().rstrip("\n")
    assert password, "Missing guest administrator test credential"
    cap = call({"op": "login", "password": password})["capability"]
    other = call({"op": "login", "password": password})["capability"]
    del password

    call({"op": "site-status"}, allowed=False)
    call({"op": "shell", "capability": cap, "command": "id"}, allowed=False)
    call({"op": "site-plan", "capability": cap, "title": "Unexpected",
          "email": "admin@native-two.test", "domain": DOMAIN, "php": "8.3",
          "command": "id"}, allowed=False)
    assert "already exist" in call({
        "op": "site-plan", "capability": cap, "title": "Duplicate",
        "email": "admin@native-one.test", "domain": FIRST_DOMAIN, "php": "8.3",
    }, allowed=False).lower()

    plan = call({
        "op": "site-plan", "capability": cap, "title": "Second native website",
        "email": "admin@native-two.test", "domain": DOMAIN, "php": "8.3",
    })
    assert plan["backend"] == "ubuntu-disposable"
    assert plan["domain"] == DOMAIN and plan["confirmation"] == "CREATE NATIVE TEST SITE"
    assert len(plan["resources"]) == 8 and plan["site_id"].startswith("wm-")

    apply = {"op": "site-apply", "plan": plan["id"],
             "confirmation": "CREATE NATIVE TEST SITE"}
    call({**apply, "capability": other}, allowed=False)
    call({**apply, "capability": cap, "confirmation": "YES"}, allowed=False)
    job = call({**apply, "capability": cap})["job"]
    call({**apply, "capability": cap}, allowed=False)

    deadline = time.monotonic() + 780
    while time.monotonic() < deadline:
        state = call({"op": "site-status", "capability": cap})["jobs"]
        assert "password" not in json.dumps(state).lower()
        ours = [row for row in state if row["id"] == job]
        assert len(ours) == 1
        if ours[0]["state"] != "running":
            break
        time.sleep(2)
    else:
        raise AssertionError("Timed out waiting for native site job")
    assert ours[0]["state"] == "succeeded", ours[0]
    assert call({"op": "site-status", "capability": other})["jobs"] == []
    call({"op": "site-result", "capability": other, "job": job}, allowed=False)
    result = call({"op": "site-result", "capability": cap, "job": job})
    assert result["site_id"] == plan["site_id"]
    assert len(result["password"]) >= 30 and result["domain"] == DOMAIN
    del result
    call({"op": "site-result", "capability": cap, "job": job}, allowed=False)
    website_ready(DOMAIN)
    website_ready(FIRST_DOMAIN)
    print("PASS: authenticated guest socket created second independent native WordPress; "
          "session isolation, one-use plans, progress and secret redaction verified", flush=True)


if __name__ == "__main__":
    main()
