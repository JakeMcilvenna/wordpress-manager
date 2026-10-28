"""Bounded, local, read-only Linux observations. No shell, sudo, or network calls."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import time


@dataclass(frozen=True)
class Result:
    output: str = ""
    code: int = 0
    error: str | None = None


# Only these executables and argument shapes are permitted. Never accept browser input.
EXECUTABLES = {
    "packages": ("/usr/bin/dpkg-query", ("-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n")),
    "service": ("/usr/bin/systemctl", None),
    "wp": ("/usr/local/bin/wp", ("--info", "--no-color")),
    "php": ("/usr/bin/php", ("--version",)),
}


def run_probe(probe: str, args: tuple[str, ...] = ()) -> Result:
    executable, fixed_args = EXECUTABLES[probe]
    if probe == "service":
        if len(args) != 1 or not re.fullmatch(r"(?:nginx|mariadb|php\d+\.\d+-fpm)\.service", args[0]):
            raise ValueError("Service is outside the probe allowlist")
        argv = (executable, "show", "--property=LoadState,ActiveState,SubState", "--", args[0])
    else:
        if args:
            raise ValueError("This probe accepts no arguments")
        argv = (executable, *fixed_args)
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=4, check=False,
            cwd="/", stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C",
                 "HOME": "/nonexistent", "WP_CLI_CONFIG_PATH": "/dev/null", "PAGER": "cat",
                 "SYSTEMD_PAGER": "cat", "SYSTEMD_COLORS": "0"},
        )
        # stderr may contain paths or local configuration; never display it.
        truncated = len(completed.stdout) > 2097152
        return Result(completed.stdout[:2097152], completed.returncode if not truncated else 1,
                      None if completed.returncode == 0 and not truncated else "Probe could not complete")
    except FileNotFoundError:
        return Result(code=127, error="Probe is unavailable")
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return Result(code=1, error="Probe unavailable or timed out")


def read_text(path: str | Path, limit: int = 65536) -> str | None:
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return stream.read(limit)
    except (OSError, UnicodeError):
        return None


def parse_packages(output: str) -> dict[str, str]:
    packages = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] == "installed":
            packages[parts[0].split(":")[0]] = parts[1][:120]
    return packages


def parse_memory(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+) kB", line)
        if match:
            values[match[1]] = int(match[2]) * 1024
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if total is None or available is None or total <= 0:
        return {"percent": None, "used": None, "total": None}
    used = max(0, total - available)
    return {"percent": round(min(100, used / total * 100), 1), "used": used, "total": total}


def cpu_ticks(text: str) -> tuple[int, int] | None:
    try:
        # guest and guest_nice are already included in user and nice.
        fields = next(line for line in text.splitlines() if line.startswith("cpu ")).split()[1:9]
        values = [int(value) for value in fields]
        if len(values) < 4:
            return None
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    except (ValueError, StopIteration):
        return None


def service_state(result: Result) -> str:
    values = dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)
    if result.code != 0 or "LoadState" not in values:
        return "unknown"
    if values["LoadState"] == "not-found":
        return "missing"
    active = values.get("ActiveState")
    return {"active": "healthy", "inactive": "stopped", "failed": "failed",
            "activating": "starting", "deactivating": "stopping"}.get(active, "unknown")


class Collector:
    def __init__(self, runner=run_probe, reader=read_text, system=None, disk=shutil.disk_usage,
                 clock=time.monotonic, ttl=15):
        self.runner, self.reader, self.disk = runner, reader, disk
        self.system = system or platform.system()
        self.clock, self.ttl = clock, ttl
        self._lock = threading.Lock()
        self._snapshot = None
        self._sampled_at = -float("inf")
        self._cpu_previous = None

    def snapshot(self) -> dict:
        with self._lock:
            if self._snapshot is not None and self.clock() - self._sampled_at < self.ttl:
                return self._snapshot
            self._snapshot = self._collect()
            self._sampled_at = self.clock()
            return self._snapshot

    def _collect(self) -> dict:
        linux = self.system == "Linux"
        package_result = self.runner("packages") if linux else Result(code=127)
        packages = parse_packages(package_result.output)
        fpm = sorted(name for name in packages if re.fullmatch(r"php\d+\.\d+-fpm", name))
        specifications = [
            ("nginx", "Nginx", "Web server", "nginx", "nginx.service"),
            ("php", "PHP CLI", "Command-line runtime", next((p for p in sorted(packages) if re.fullmatch(r"php\d+\.\d+-cli", p)), "php-cli"), None),
            *[(name, "PHP-FPM " + name[3:-4], "Isolated site processes", name, name + ".service") for name in fpm],
            ("mariadb", "MariaDB", "Database server", "mariadb-server", "mariadb.service"),
            ("phpmyadmin", "phpMyAdmin", "Private database administration", "phpmyadmin", None),
            ("wpcli", "WP-CLI", "WordPress command line", None, None),
            ("certbot", "Certbot", "TLS certificate tooling", "certbot", None),
        ]
        if not fpm:
            specifications.insert(2, ("fpm", "PHP-FPM", "Isolated site processes", "php-fpm", None))
        software = []
        for ident, name, purpose, package, service in specifications:
            version = packages.get(package)
            if ident == "nginx" and version is None:
                version = next((packages[p] for p in ("nginx-core", "nginx-full", "nginx-light") if p in packages), None)
            source = "dpkg package database"
            if linux and ident == "wpcli":
                info = self.runner("wp")
                match = re.search(r"^WP-CLI version:\s*([^\r\n]+)", info.output, re.MULTILINE)
                version = match[1].strip()[:80] if match else None
                source = "/usr/local/bin/wp --info"
            if linux and ident == "php":
                info = self.runner("php")
                match = re.search(r"^PHP ([0-9][\w.\-+]*)", info.output)
                if match:
                    version = match[1]
                    source = "/usr/bin/php --version"
            state = "installed" if version else ("not-detected" if linux and package_result.code == 0 else "unknown")
            if linux and service:
                state = service_state(self.runner("service", (service,)))
            software.append({"id": ident, "name": name, "purpose": purpose, "version": version,
                             "state": state, "source": source, "service": service})
        resources = self._resources(linux)
        os_release = self.reader("/etc/os-release") if linux else None
        os_name = self.system
        if os_release:
            match = re.search(r'^PRETTY_NAME=["\']?([^"\'\n]+)', os_release, re.MULTILINE)
            os_name = match[1] if match else "Linux"
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        notifier = self.reader("/var/lib/update-notifier/updates-available") if linux else None
        update_count = re.search(r"^(\d+) updates? can be applied", notifier or "", re.MULTILINE)
        return {"hostname": platform.node(), "os": os_name, "platform": self.system,
                "supported": linux, "collected_at": timestamp, "software": software,
                "resources": resources, "updates": {"count": int(update_count[1]) if update_count else None,
                "source": "Local update-notifier cache; may be stale. No repositories contacted."},
                "reboot_required": self.reader("/var/run/reboot-required") is not None if linux else None,
                "healthy_services": sum(s["state"] == "healthy" for s in software),
                "service_count": sum(s["service"] is not None for s in software)}

    def _resources(self, linux: bool) -> dict:
        memory = parse_memory(self.reader("/proc/meminfo") or "") if linux else {"percent": None, "used": None, "total": None}
        ticks = cpu_ticks(self.reader("/proc/stat") or "") if linux else None
        cpu = None
        if ticks and self._cpu_previous:
            delta = ticks[0] - self._cpu_previous[0]
            idle = ticks[1] - self._cpu_previous[1]
            if delta > 0 and idle >= 0:
                cpu = round(max(0, min(100, (delta - idle) / delta * 100)), 1)
        self._cpu_previous = ticks
        try:
            usage = self.disk("/" if linux else Path.cwd().anchor)
            disk = {"used": usage.used, "total": usage.total, "percent": round(usage.used / usage.total * 100, 1)}
        except (OSError, ZeroDivisionError):
            disk = {"used": None, "total": None, "percent": None}
        try:
            uptime = float((self.reader("/proc/uptime") or "").split()[0]) if linux else None
        except (IndexError, ValueError):
            uptime = None
        try:
            load = [float(value) for value in (self.reader("/proc/loadavg") or "").split()[:3]] if linux else []
        except ValueError:
            load = []
        return {"cpu_percent": cpu, "memory": memory, "disk": disk, "uptime_seconds": uptime,
                "load": load, "cores": os.cpu_count()}
