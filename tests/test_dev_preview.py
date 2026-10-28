"""Check that the opt-in Docker preview cannot mutate or expose the live host."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_preview_is_ubuntu_and_reuses_reviewed_python_wheels():
    dockerfile = (ROOT / "dev/Dockerfile.preview").read_text()
    assert "FROM ubuntu:24.04" in dockerfile
    assert "COPY deploy/requirements.lock" in dockerfile
    assert "--require-hashes" in dockerfile and "--no-deps" in dockerfile
    assert "USER wm-preview" in dockerfile
    assert "socat TCP-LISTEN:8090" in dockerfile
    assert "TCP:127.0.0.1:8080" in dockerfile


def test_preview_never_mounts_system_secrets_or_listens_on_public_host():
    compose = (ROOT / "compose.preview.yml").read_text()
    assert '"127.0.0.1:8090:8090"' in compose
    assert "./:/workspace:ro" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "privileged: true" not in compose
    assert "/var/run/docker.sock" not in compose
    assert "/etc/wordpress-manager" not in compose
    assert "/var/lib/wordpress-manager-control" not in compose


def test_preview_has_reloading_without_debugger():
    source = (ROOT / "dev/preview.py").read_text()
    assert 'TEMPLATES_AUTO_RELOAD": True' in source
    assert 'host="127.0.0.1"' in source
    assert 'debug=False' in source
    assert 'use_reloader=True' in source
