from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import subprocess

import pytest

from wordpress_manager.collector import Collector, Result, cpu_ticks, parse_memory, parse_packages, run_probe, service_state


def test_installed_versions_and_dynamic_fpm(observer):
    snap = observer.snapshot()
    software = {item["id"]: item for item in snap["software"]}
    assert software["php8.5-fpm"]["service"] == "php8.5-fpm.service"
    assert software["wpcli"]["version"] == "2.12.0"
    assert software["mariadb"]["version"] == "1:10.11.13"
    assert software["phpmyadmin"]["state"] == "not-detected"
    assert snap["healthy_services"] == snap["service_count"] == 3
    assert snap["os"] == "Ubuntu 24.04.3 LTS"
    assert snap["updates"]["count"] == 7
    assert snap["reboot_required"] is False


def test_package_parser_excludes_removed_and_malformed():
    assert parse_packages("a\t1\tinstalled\nb\t2\tconfig-files\nbad\nlib:amd64\t3\tinstalled") == {"a": "1", "lib": "3"}


def test_memory_uses_available_not_free():
    memory = parse_memory("MemTotal: 100 kB\nMemFree: 5 kB\nMemAvailable: 60 kB")
    assert memory == {"percent": 40.0, "used": 40960, "total": 102400}
    assert parse_memory("MemTotal: 0 kB")["percent"] is None
    assert parse_memory("no access")["total"] is None


def test_cpu_avoids_double_counting_guest():
    assert cpu_ticks("cpu 100 20 30 500 10 5 5 0 99 10") == (670, 510)
    assert cpu_ticks("cpu nope") is None


def test_cpu_sampling_and_cache():
    now = [0]
    ticks = ["cpu 100 0 0 900 0 0 0 0"]
    calls = []
    observer = Collector(system="Linux", runner=lambda *a: calls.append(a) or Result(code=127),
                         reader=lambda path: ticks[0] if path == "/proc/stat" else None,
                         clock=lambda: now[0])
    first = observer.snapshot()
    assert first["resources"]["cpu_percent"] is None
    assert observer.snapshot() is first
    count = len(calls)
    ticks[0] = "cpu 125 0 0 975 0 0 0 0"
    now[0] = 16
    assert observer.snapshot()["resources"]["cpu_percent"] == 25
    assert len(calls) > count


def test_concurrent_requests_share_snapshot(observer):
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: observer.snapshot(), range(20)))
    assert all(item is results[0] for item in results)


@pytest.mark.parametrize("state,expected", [("active", "healthy"), ("failed", "failed"), ("inactive", "stopped"), ("activating", "starting")])
def test_service_states(state, expected):
    assert service_state(Result(f"LoadState=loaded\nActiveState={state}")) == expected


def test_missing_and_unavailable_are_different():
    assert service_state(Result("LoadState=not-found")) == "missing"
    assert service_state(Result(code=1)) == "unknown"
    assert service_state(Result("irrelevant")) == "unknown"


def test_non_linux_never_runs_linux_commands():
    def forbidden(*args):
        pytest.fail("No Linux command may run on Windows")
    snapshot = Collector(system="Windows", runner=forbidden).snapshot()
    assert snapshot["supported"] is False
    assert snapshot["resources"]["memory"]["percent"] is None
    assert all(item["state"] == "unknown" for item in snapshot["software"])


def test_probes_fail_gracefully():
    for error in (FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("probe", 4)):
        with patch("wordpress_manager.collector.subprocess.run", side_effect=error):
            assert run_probe("packages").error


def test_probe_command_is_fixed_and_has_deadline():
    with patch("wordpress_manager.collector.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok", "private")) as run:
        assert run_probe("service", ("php8.5-fpm.service",)).output == "ok"
        args, kwargs = run.call_args
        assert args[0] == ("/usr/bin/systemctl", "show", "--property=LoadState,ActiveState,SubState", "--", "php8.5-fpm.service")
        assert kwargs["timeout"] == 4
        assert kwargs.get("shell", False) is False
        assert kwargs["env"]["WP_CLI_CONFIG_PATH"] == "/dev/null"
        assert "private" not in run_probe("service", ("nginx.service",)).output


@pytest.mark.parametrize("args", [("nginx; rm -rf /",), ("ssh.service",), ("--help",), ("../nginx.service",), ()])
def test_service_allowlist_blocks_injection(args):
    with pytest.raises(ValueError):
        run_probe("service", args)


def test_package_probe_rejects_caller_arguments():
    with pytest.raises(ValueError):
        run_probe("packages", ("--anything",))


def test_inaccessible_proc_and_disk_are_unknown():
    def inaccessible(_):
        raise PermissionError()
    snapshot = Collector(system="Linux", runner=lambda *a: Result(code=127), reader=lambda _: None, disk=inaccessible).snapshot()
    assert snapshot["resources"]["disk"]["percent"] is None
    assert snapshot["resources"]["cpu_percent"] is None
    assert snapshot["updates"]["count"] is None
