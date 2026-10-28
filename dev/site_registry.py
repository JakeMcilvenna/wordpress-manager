"""Unprivileged persistent *local preview* website registration.

Records only a fixed site identifier; never controls WordPress, filesystem
permissions, Docker, or MariaDB. Never imported by the production application.
"""
from pathlib import Path
import sqlite3

SITE_ID = "local-wordpress"
DEFAULT_DB = "/var/lib/wm-preview/registered-sites.sqlite3"


class SiteRegistry:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)

    def is_registered(self):
        if not self.path.is_file():
            return False
        with sqlite3.connect(self.path, timeout=5) as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM registrations WHERE site_id = ?", (SITE_ID,)
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        return bool(row)

    def register(self):
        # The only supported ID is a source-code constant, not a URL or form field.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS registrations "
                         "(site_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO registrations (site_id, created_at) "
                         "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))", (SITE_ID,))

    def unregister(self):
        if not self.path.is_file():
            return
        with sqlite3.connect(self.path, timeout=5) as conn:
            try:
                conn.execute("DELETE FROM registrations WHERE site_id = ?", (SITE_ID,))
            except sqlite3.OperationalError:
                return
