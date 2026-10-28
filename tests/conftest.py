from collections import namedtuple

import pytest

from wordpress_manager import create_app
from wordpress_manager.collector import Collector, Result

PACKAGES = "\n".join(f"{name}\t{version}\tinstalled" for name, version in [
    ("nginx", "1.24.0-2ubuntu7.3"), ("php8.5-cli", "8.5.12"),
    ("php8.5-fpm", "8.5.12"), ("mariadb-server", "1:10.11.13"), ("certbot", "2.9.0"),
])


@pytest.fixture
def observer():
    def run(probe, args=()):
        if probe == "packages":
            return Result(PACKAGES)
        if probe == "wp":
            return Result("OS: Linux\nPHP binary: /usr/bin/php\nWP-CLI version: 2.12.0\n")
        if probe == "php":
            return Result("PHP 8.5.12 (cli)")
        return Result("LoadState=loaded\nActiveState=active\nSubState=running\n")
    files = {"/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04.3 LTS"',
             "/proc/meminfo": "MemTotal: 4000000 kB\nMemAvailable: 3000000 kB\n",
             "/proc/stat": "cpu  500 0 300 9000 200 0 0 0 0 0\n",
             "/proc/uptime": "172805.00 999.00", "/proc/loadavg": "0.12 0.20 0.16 1/10 123",
             "/var/lib/update-notifier/updates-available": "7 updates can be applied immediately."}
    disk = namedtuple("disk", "total used free")(100 * 1024**3, 30 * 1024**3, 70 * 1024**3)
    return Collector(runner=run, reader=lambda path: files.get(path), system="Linux", disk=lambda _: disk)


@pytest.fixture
def app(observer):
    return create_app({"TESTING": True}, collector=observer)


@pytest.fixture
def client(app):
    return app.test_client()
