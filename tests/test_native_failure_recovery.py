"""No-root failure recovery and secret-redaction regressions for native guest code.

These tests never call disposable provisioning or external services.
"""
import subprocess

import pytest

from deploy import ubuntu_site_executor as native


def test_failed_native_commands_never_expose_credentials(monkeypatch):
    secret = "credential-must-never-appear"
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1, args[0], output=secret, stderr="failed with " + secret
        )
    monkeypatch.setattr(native.subprocess, "run", fail)
    with pytest.raises(RuntimeError) as error:
        native._run("/usr/bin/mariadb", "--batch",
                    input_text="CREATE USER identified by " + secret)
    assert secret not in str(error.value)
    assert "mariadb" in str(error.value)
    assert "CREATE USER" not in str(error.value)


def test_download_diagnostics_are_opt_in_bounded_and_single_line(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1, args[0], stderr="x" * 1000 + "\nlast line\rnext"
        )
    monkeypatch.setattr(native.subprocess, "run", fail)
    command = ("/usr/sbin/runuser", "-u", "wmsaaaaaaaaaa", "--",
               "/usr/local/bin/wp", "core", "download")
    with pytest.raises(RuntimeError) as ordinary:
        native._run(*command)
    assert "last line" not in str(ordinary.value)
    with pytest.raises(RuntimeError) as diagnostic:
        native._run(*command, safe_download_diagnostic=True)
    message = str(diagnostic.value)
    assert "last line next" in message
    assert "\n" not in message and "\r" not in message
    assert len(message) < 900


def test_exclusive_native_write_refuses_existing_resource(tmp_path):
    owned = tmp_path / "owned.conf"
    owned.write_text("unmanaged content")
    with pytest.raises(FileExistsError):
        native._write_exclusive(owned, "replacement")
    assert owned.read_text() == "unmanaged content"


def test_record_reader_refuses_symlink_and_identity_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "disposable_only", lambda: None)
    monkeypatch.setattr(native, "RECORDS", tmp_path)
    site_id = "wm-aaaaaaaaaaaa"
    target = tmp_path / "external.json"
    target.write_text('{"site_id":"wm-bbbbbbbbbbbb"}')
    link = tmp_path / (site_id + ".json")
    link.symlink_to(target)
    assert native.read_record(site_id) is None
    link.unlink()
    link.write_text(target.read_text())
    with pytest.raises(ValueError, match="identity mismatch"):
        native.read_record(site_id)
    assert target.read_text() == '{"site_id":"wm-bbbbbbbbbbbb"}'
