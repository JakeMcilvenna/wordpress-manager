"""Disposable-guest-only native website control protocol.

This module is loaded by the existing authenticated root provisioner. It does
not expose a production website operation: *every* entrypoint checks the native
executor's root, Ubuntu 24.04, Docker and exact disposable-marker guard.

There are no user-supplied commands or paths. Site plans are regenerated at
approval time, authenticated sessions own one-use plans and jobs, and the
guest-only administrator credential is held in the root-only SQLite ledger
until its single successful retrieval. No credential enters audit/status/logs.
"""
import importlib.util
import json
from pathlib import Path
import secrets
import time


def native_executor():
    path = Path(__file__).with_name("ubuntu_site_executor.py")
    spec = importlib.util.spec_from_file_location("_wm_guest_site_executor", path)
    if spec is None or spec.loader is None:
        raise ValueError("Native website executor unavailable")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    native.disposable_only()
    return native


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS site_plans (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL,
            body TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS site_jobs (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, site_id TEXT NOT NULL,
            state TEXT NOT NULL, detail TEXT NOT NULL, result TEXT
        );
    """)
    interrupted = connection.execute(
        "SELECT id FROM site_jobs WHERE state='running'"
    ).fetchall()
    for row in interrupted:
        connection.execute(
            "UPDATE site_jobs SET state='interrupted', detail=? WHERE id=?",
            ("Site state needs operator review; no automatic retry.", row["id"]),
        )
    return [row["id"] for row in interrupted]


def running_site_job(connection):
    return connection.execute(
        "SELECT 1 FROM site_jobs WHERE state='running' LIMIT 1"
    ).fetchone() is not None


def _trusted_plan(native, body):
    spec = native.WebsiteSpec.from_fields(
        body.get("title"), body.get("email"), body.get("domain"), body.get("php")
    )
    plan = native.make_plan(spec)
    if body != plan.as_session():
        raise ValueError("The native site plan changed; review it again")
    return plan


def job_worker(job_id, plan, db, audit):
    try:
        native = native_executor()
        audit("site-job-start", {"job": job_id, "site": plan.site_id})
        result = native.create(plan, native.CONFIRMATION)
    except Exception:
        # The executor leaves partial resources marked needs-review. Never
        # automatically erase resources after a failed privileged operation.
        with db() as connection:
            connection.execute(
                "UPDATE site_jobs SET state='failed',detail=? WHERE id=?",
                ("Native installation failed; inspect ownership record and journal.", job_id),
            )
        audit("site-job-failed", {"job": job_id, "site": plan.site_id})
        return
    with db() as connection:
        connection.execute(
            "UPDATE site_jobs SET state='succeeded',detail=?,result=? WHERE id=?",
            ("Native website created in disposable guest.", json.dumps(result), job_id),
        )
    audit("site-job-succeeded", {"job": job_id, "site": plan.site_id})


def dispatch(op, message, owner, db, audit, canonical, digest, threading):
    # Runs on every operation, including status and credential retrieval.
    # In particular, a live Hetzner service cannot mint plans or execute jobs.
    native = native_executor()
    if op == "site-plan":
        spec = native.WebsiteSpec.from_fields(
            message.get("title"), message.get("email"),
            message.get("domain"), message.get("php"),
        )
        plan = native.make_plan(spec)
        native._preflight(plan)
        identifier = secrets.token_hex(16)
        with db() as connection:
            connection.execute(
                "INSERT INTO site_plans(id,owner,expires,body) VALUES(?,?,?,?)",
                (identifier, owner, time.time() + 600, canonical(plan.as_session())),
            )
        audit("site-plan-created", {"id": identifier, "site": plan.site_id,
                                    "digest": digest(plan.as_session())})
        return {"id": identifier, "digest": digest(plan.as_session()), **plan.as_session()}

    if op == "site-status":
        with db() as connection:
            rows = connection.execute(
                "SELECT id,site_id,state,detail FROM site_jobs "
                "WHERE owner=? ORDER BY rowid DESC LIMIT 20", (owner,),
            )
            return {"jobs": [dict(row) for row in rows]}

    if op == "site-result":
        job = message.get("job")
        if not isinstance(job, str) or len(job) != 32 or any(c not in "0123456789abcdef" for c in job):
            raise ValueError("Invalid native site job")
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT result,state FROM site_jobs WHERE id=? AND owner=?",
                (job, owner),
            ).fetchone()
            if row is None or row["state"] != "succeeded" or not row["result"]:
                raise ValueError("No available completed site result")
            result = json.loads(row["result"])
            connection.execute("UPDATE site_jobs SET result=NULL WHERE id=?", (job,))
        audit("site-result-collected", {"job": job})
        return result

    if op == "site-apply":
        if message.get("confirmation") != native.CONFIRMATION:
            raise ValueError("Explicit native website confirmation is required")
        plan_id = message.get("plan")
        if not isinstance(plan_id, str) or len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("Invalid site plan")
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body FROM site_plans "
                "WHERE id=? AND owner=? AND used=0 AND expires>?",
                (plan_id, owner, time.time()),
            ).fetchone()
            if row is None:
                raise ValueError("Site plan expired, used or belongs to another session")
            if running_site_job(connection) or connection.execute(
                "SELECT 1 FROM jobs WHERE state='running'"
            ).fetchone():
                raise ValueError("A server or site job is already running")
            plan = _trusted_plan(native, json.loads(row["body"]))
            native._preflight(plan)
            job_id = secrets.token_hex(16)
            connection.execute("UPDATE site_plans SET used=1 WHERE id=?", (plan_id,))
            connection.execute(
                "INSERT INTO site_jobs(id,owner,site_id,state,detail) "
                "VALUES(?, ?, ?, 'running', ?)",
                (job_id, owner, plan.site_id, "Approved native guest site plan"),
            )
        audit("site-job-approved", {"job": job_id, "site": plan.site_id, "plan": plan_id})
        threading.Thread(target=job_worker, args=(job_id, plan, db, audit), daemon=True).start()
        return {"job": job_id}

    raise ValueError("Unsupported native website operation")
