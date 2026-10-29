"""Guest-only website socket protocol tests (no root or real site changes)."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="POSIX only")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def control(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wm_site_control_test", ROOT / "deploy/site_control.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    state = tmp_path / "ledger.sqlite"

    def db():
        conn = sqlite3.connect(state)
        conn.row_factory = sqlite3.Row
        return conn

    with db() as connection:
        connection.execute("CREATE TABLE jobs(id TEXT, state TEXT, detail TEXT)")
        m.initialize(connection)

    class FakeSpec:
        @staticmethod
        def from_fields(title, email, domain, php):
            if domain != "sample.test" or php != "8.3":
                raise ValueError("Invalid domain or php")
            return SimpleNamespace(title=title, email=email, domain=domain, php=php)

    class FakePlan:
        site_id = "wm-aaaaaaaaaaaa"
        confirmation = "CREATE NATIVE TEST SITE"

        def __init__(self, spec):
            self.spec = spec

        def as_session(self):
            return dict(title=self.spec.title, email=self.spec.email,
                        domain=self.spec.domain, php=self.spec.php,
                        site_id=self.site_id, backend="ubuntu-disposable",
                        resources=["site:owned"], confirmation=self.confirmation)

    events = []
    native = SimpleNamespace(
        WebsiteSpec=FakeSpec, CONFIRMATION="CREATE NATIVE TEST SITE",
        make_plan=lambda spec: FakePlan(spec), _preflight=lambda plan: None,
        create=lambda plan, confirmation: {
            "site_id": plan.site_id, "installed": True, "password": "test-secret",
        },
    )
    monkeypatch.setattr(m, "native_executor", lambda: native)
    def audit(action, details):
        events.append((action, details))
    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    def call(op, owner="owner", **fields):
        return m.dispatch(op, fields, owner, db, audit,
                          lambda v: json.dumps(v, sort_keys=True, separators=(",", ":")),
                          lambda v: "digest", SimpleNamespace(Thread=ImmediateThread))
    return m, db, call, events, monkeypatch


def test_guest_guard_is_mandatory_before_any_operation(control):
    m, db, call, events, monkeypatch = control
    def reject():
        raise PermissionError("Not a disposable Ubuntu guest")
    monkeypatch.setattr(m, "native_executor", reject)
    for op in ("site-plan", "site-apply", "site-status", "site-result"):
        with pytest.raises(PermissionError, match="disposable"):
            call(op)
    with db() as connection:
        assert connection.execute("SELECT count(*) FROM site_jobs").fetchone()[0] == 0


def test_confirmation_ownership_one_use_and_secret_redaction(control):
    m, db, call, events, monkeypatch = control
    fields = dict(title="Sample", email="admin@sample.test", domain="sample.test", php="8.3")
    plan = call("site-plan", **fields)
    with pytest.raises(ValueError, match="another session"):
        call("site-apply", owner="stranger", plan=plan["id"], confirmation="CREATE NATIVE TEST SITE")
    with pytest.raises(ValueError, match="confirmation"):
        call("site-apply", plan=plan["id"], confirmation="YES")
    job = call("site-apply", plan=plan["id"], confirmation="CREATE NATIVE TEST SITE")["job"]
    with pytest.raises(ValueError, match="used"):
        call("site-apply", plan=plan["id"], confirmation="CREATE NATIVE TEST SITE")
    status = call("site-status")
    assert status["jobs"][0]["state"] == "succeeded"
    assert "password" not in json.dumps(status)
    assert "test-secret" not in json.dumps(events)
    with pytest.raises(ValueError, match="No available"):
        call("site-result", owner="stranger", job=job)
    assert call("site-result", job=job)["password"] == "test-secret"
    with pytest.raises(ValueError, match="No available"):
        call("site-result", job=job)
    with db() as connection:
        assert connection.execute("SELECT result FROM site_jobs WHERE id=?", (job,)).fetchone()[0] is None


def test_tampered_stored_plan_is_rejected(control):
    m, db, call, events, monkeypatch = control
    plan = call("site-plan", title="Sample", email="admin@sample.test",
                domain="sample.test", php="8.3")
    with db() as connection:
        body = json.loads(connection.execute(
            "SELECT body FROM site_plans WHERE id=?", (plan["id"],)).fetchone()[0])
        body["resources"] = ["/etc/shadow"]
        connection.execute("UPDATE site_plans SET body=? WHERE id=?",
                           (json.dumps(body), plan["id"]))
    with pytest.raises(ValueError, match="changed"):
        call("site-apply", plan=plan["id"], confirmation="CREATE NATIVE TEST SITE")
    with db() as connection:
        assert connection.execute("SELECT used FROM site_plans WHERE id=?", (plan["id"],)).fetchone()[0] == 0


def test_expired_plan_and_running_stack_job_are_rejected(control):
    m, db, call, events, monkeypatch = control
    plan = call("site-plan", title="Sample", email="admin@sample.test",
                domain="sample.test", php="8.3")
    with db() as connection:
        connection.execute("INSERT INTO jobs VALUES('stack-job','running','busy')")
    with pytest.raises(ValueError, match="already running"):
        call("site-apply", plan=plan["id"], confirmation="CREATE NATIVE TEST SITE")
    with db() as connection:
        connection.execute("DELETE FROM jobs")
        connection.execute("UPDATE site_plans SET expires=? WHERE id=?",
                           (time.time() - 1, plan["id"]))
    with pytest.raises(ValueError, match="expired"):
        call("site-apply", plan=plan["id"], confirmation="CREATE NATIVE TEST SITE")


def test_restart_marks_running_jobs_interrupted_without_retry(control):
    m, db, call, events, monkeypatch = control
    with db() as connection:
        connection.execute(
            "INSERT INTO site_jobs(id,owner,site_id,state,detail) VALUES(?,?,?,?,?)",
            ("a" * 32, "owner", "wm-aaaaaaaaaaaa", "running", "started"),
        )
    with db() as connection:
        interrupted = m.initialize(connection)
    assert interrupted == ["a" * 32]
    assert call("site-status")["jobs"][0]["state"] == "interrupted"
