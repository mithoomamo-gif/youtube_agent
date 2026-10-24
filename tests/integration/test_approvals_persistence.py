"""Acceptance test D (Round 2) — approvals persistence across a restart.

Round 1 gap under test
----------------------
In Round 1 the approvals router (``backend/app/routers/approvals.py``)
tracked decisions in an **in-memory** dict (``_DECISIONS``) and said so
in its own docstring: *"a backend restart forgets them"*.  Round 2's
execution team owns the fix: an ``approvals`` table persisted via
**Alembic migration 0002** (expected under ``backend/migrations/``).

What this test proves
---------------------
1. Migration 0002 exists on disk (``backend/migrations/versions/``).
2. It is *applied* to a freshly booted sidecar's database: after boot
   against a clean data dir, ``<data_dir>/app.db`` contains the
   approvals table the migration creates (and/or the DB version
   bookkeeping — ``alembic_version`` / ``schema_meta`` — has advanced
   past the Round 1 baseline of version 1).
3. An approval decision made through the real HTTP API **survives a
   full sidecar restart** against the SAME data dir:
   * pre-restart: approve a pending gate -> 200, decision ``approved``;
   * restart the process (new process, empty in-memory state);
   * post-restart: the approval queue / DB still shows the decision,
     and a second approve attempt (with a *fresh* Idempotency-Key, so
     the 24h idempotency replay cannot mask it) returns
     **409 ``already_decided``** (or equivalent).  A Round 1 in-memory
     implementation would happily return 200 again here — that is the
     regression this test exists to catch.

Approval-creation path actually used
------------------------------------
Round 2's approvals router (see its module docstring) persists a
decision in exactly two ways:

1. **Pending-row path** — the execution layer's gates bridge
   (``app.execution.gates_bridge.ensure_approval``) has already
   INSERTed a ``pending`` approvals row for the gate; deciding
   UPDATEs that row.
2. **Insert-with-body-fields path** — no row exists, but the decision
   body carries ``project_id`` + ``gate`` ('a'/'b'), so a fully
   decided row is INSERTed.

A bare id with neither (the Round 1 skeleton call shape) is explicitly
kept in-memory-only by design in Round 2, because
``approvals.project_id`` is NOT NULL with an FK to ``projects`` — so
this test does NOT use the bare-id shape for its persistence claim.

The scenario therefore: creates a niche + a ``review_first`` project
through the API, then — because there is no channel-create endpoint
and the projects router only best-effort-persists API projects whose
channel FK cannot resolve — seeds the FK parents (niche, channel,
project) directly into the sidecar's own ``app.db``, mirroring the
execution team's ``backend/tests/test_execution_approvals.py``
``_seed_project()`` pattern.  It starts that project's pipeline via
the API (the real process entry autostarts the ExecutionService,
``YTA_EXECUTION=1``) and polls the approvals queue for a pending gate
row.  If one materialises, the decision uses path 1 (empty body); if
the stub DAG does not reach a gate in time, the decision uses path 2
(body carries the seeded ``project_id`` + ``gate='a'``).  The scenario
records which path was used (``creation_path``) so a failure report
can state it precisely.  The persistence assertions are identical for
both paths.

Boot contract
------------
The sidecar is booted as a real subprocess via the documented launch
environment (``backend/app/config.py``): ``YTA_TOKEN``,
``YTA_PORT``, ``YTA_BOOT_ID``, ``YTA_DATA_DIR``, ``YTA_LOG_LEVEL``,
with ``backend/.venv``'s interpreter running ``python -m app.main``
from the ``backend/`` directory.  Readiness is ``GET /health/live``
(the one token-exempt endpoint).  All API calls carry
``X-Local-Token``; work-creating POSTs carry a fresh
``Idempotency-Key`` per logical operation.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
MIGRATIONS_VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"
SERVER_PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

TOKEN = "acceptance-d-token-0123456789"
# Tables the approvals queue router has historically read from; the
# migration's own CREATE TABLE names are added to this set dynamically.
CANDIDATE_APPROVAL_TABLES = ("approvals", "approval_queue", "gates", "gate_decisions")

BOOT_TIMEOUT_S = 120.0
GATE_POLL_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Migration 0002 discovery
# ---------------------------------------------------------------------------


def _find_migration_0002() -> Optional[Path]:
    """Locate the Alembic migration whose revision is 0002."""
    if not MIGRATIONS_VERSIONS_DIR.is_dir():
        return None
    for path in sorted(MIGRATIONS_VERSIONS_DIR.glob("*.py")):
        if path.name.startswith("0002"):
            return path
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"""revision[^=\n]*=\s*["']0002["']""", text):
            return path
    return None


def _migration_table_names(migration_path: Optional[Path]) -> set[str]:
    """Table names a migration appears to create (best-effort parse)."""
    names: set[str] = set()
    if migration_path is None:
        return names
    try:
        text = migration_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return names
    names.update(
        re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(\w+)", text, re.I)
    )
    names.update(re.findall(r"op\.create_table\(\s*[\"'](\w+)[\"']", text))
    return names


# ---------------------------------------------------------------------------
# Sidecar subprocess harness
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Sidecar:
    """One sidecar process bound to a fixed data dir."""

    def __init__(self, data_dir: Path, label: str) -> None:
        self.data_dir = data_dir
        self.label = label
        self.port = _free_port()
        self.proc: Optional[subprocess.Popen] = None
        self.log_path = data_dir / f"sidecar-{label}.log"
        self._log_fh = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        httpx = pytest.importorskip("httpx")  # noqa: F841 - presence check
        self.data_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "YTA_TOKEN": TOKEN,
                "YTA_PORT": str(self.port),
                "YTA_BOOT_ID": f"acceptance-d-{self.label}-{uuid.uuid4().hex[:8]}",
                "YTA_DATA_DIR": str(self.data_dir),
                "YTA_LOG_LEVEL": "info",
                "PYTHONPATH": str(BACKEND_DIR),
            }
        )
        self._log_fh = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [SERVER_PYTHON, "-m", "app.main"],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
        )
        self._wait_until_live()

    def _log_tail(self, limit: int = 4000) -> str:
        try:
            data = self.log_path.read_bytes()[-limit:]
            return data.decode("utf-8", errors="replace")
        except OSError:
            return "<no log>"

    def _wait_until_live(self) -> None:
        import httpx

        assert self.proc is not None
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        last_poll = "no poll completed"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"sidecar ({self.label}) exited early with code "
                    f"{self.proc.returncode}; log tail:\n{self._log_tail()}"
                )
            try:
                # trust_env=False: CI/dev shells may export HTTP(S)_PROXY /
                # ALL_PROXY; a loopback sidecar must always be reached
                # directly, never via an egress proxy.
                resp = httpx.get(
                    f"{self.base_url}/health/live", timeout=2.0, trust_env=False
                )
                if resp.status_code == 200:
                    return
                last_poll = f"HTTP {resp.status_code}"
            except Exception as exc:
                last_poll = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
        raise RuntimeError(
            f"sidecar ({self.label}) did not become live within "
            f"{BOOT_TIMEOUT_S}s; last poll: {last_poll}; "
            f"log tail:\n{self._log_tail()}"
        )

    def client(self):
        import httpx

        return httpx.Client(
            base_url=self.base_url,
            headers={"X-Local-Token": TOKEN},
            timeout=10.0,
            trust_env=False,  # loopback only — never via an env proxy
        )

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_code(resp) -> Optional[str]:
    try:
        body = resp.json()
    except Exception:
        return None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("code")
        return body.get("code")
    return None


def _queue_items(client) -> list[dict[str, Any]]:
    resp = client.get("/api/v1/approvals")
    assert resp.status_code == 200, f"GET /approvals -> {resp.status_code}: {resp.text}"
    body = resp.json()
    items = body.get("items") if isinstance(body, dict) else body
    assert isinstance(items, list), f"approvals queue is not a list: {body!r}"
    return items


def _item_for(items: list[dict], approval_id: str) -> Optional[dict]:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("id") == approval_id or item.get("approval_id") == approval_id:
            return item
    return None


def _item_shows_approved(item: dict) -> bool:
    """Tolerant check: any common field says this gate was approved/decided."""
    decision = str(item.get("decision") or "").lower()
    status = str(item.get("status") or "").lower()
    state = str(item.get("state") or "").lower()
    return (
        decision == "approved"
        or status in ("approved", "decided")
        or state in ("approved", "decided")
        or bool(item.get("decided_at"))
    )


def _db_snapshot(db_path: Path) -> dict[str, Any]:
    """Read table names, version bookkeeping and approval rows from app.db."""
    snapshot: dict[str, Any] = {
        "exists": db_path.exists(),
        "tables": set(),
        "alembic_version": None,
        "schema_meta_version": None,
        "approval_rows": {},  # table -> [row dicts]
    }
    if not db_path.exists():
        return snapshot
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        snapshot["tables"] = tables
        if "alembic_version" in tables:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            snapshot["alembic_version"] = row[0] if row else None
        if "schema_meta" in tables:
            row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
            snapshot["schema_meta_version"] = int(row[0]) if row else None
        migration_tables = _migration_table_names(_find_migration_0002())
        for table in sorted(set(CANDIDATE_APPROVAL_TABLES) | migration_tables):
            if table in tables:
                try:
                    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                    snapshot["approval_rows"][table] = [dict(r) for r in rows]
                except sqlite3.Error:
                    pass
    finally:
        conn.close()
    return snapshot


def _seed_db_project(db_path: Path) -> str:
    """Insert niche + channel + project rows (FK parents of approvals).

    Mirrors ``_seed_project()`` in the execution team's
    ``backend/tests/test_execution_approvals.py``: there is no
    channel-create REST endpoint, and the projects router only
    best-effort-persists API projects (their channel FK cannot
    resolve), so a DB-resident project — required for an approvals
    row, whose ``project_id`` is NOT NULL REFERENCES projects(id) —
    is seeded directly into the sidecar's database file.
    Returns the seeded project id.
    """
    suffix = uuid.uuid4().hex[:12]
    niche_id = f"niche-accd-{suffix}"
    channel_id = f"chan-accd-{suffix}"
    project_id = f"proj-accd-{suffix}"
    now_ms = int(time.time() * 1000)
    deadline = time.monotonic() + 20.0
    last_exc: Optional[Exception] = None
    while time.monotonic() < deadline:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO niche_profiles (id, key, label, source, "
                "user_idea, created_at, updated_at) VALUES (?, ?, "
                "'Acceptance D Niche', 'other', "
                "'Seeded niche for approvals persistence acceptance', "
                "?, ?)",
                (niche_id, f"accd-{suffix}", now_ms, now_ms),
            )
            conn.execute(
                "INSERT INTO channels (id, youtube_channel_id, title, "
                "language, created_at, updated_at) VALUES (?, ?, "
                "'Acceptance D Channel', 'en', ?, ?)",
                (channel_id, f"UCaccd{suffix}", now_ms, now_ms),
            )
            conn.execute(
                "INSERT INTO projects (id, channel_id, niche_profile_id, "
                "topic, topic_source, status, publish_mode, created_at, "
                "updated_at) VALUES (?, ?, ?, "
                "'Why approvals must survive restarts', 'user', "
                "'draft', 'review_first', ?, ?)",
                (project_id, channel_id, niche_id, now_ms, now_ms),
            )
            conn.commit()
            return project_id
        except sqlite3.OperationalError as exc:  # DB busy — retry
            last_exc = exc
            time.sleep(0.5)
        finally:
            conn.close()
    raise RuntimeError(f"could not seed DB project into {db_path}: {last_exc!r}")


# ---------------------------------------------------------------------------
# Scenario: boot -> drive to a gate -> approve -> restart -> re-query
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scenario(tmp_path_factory) -> dict[str, Any]:
    data_dir = tmp_path_factory.mktemp("yta-approvals-data")
    result: dict[str, Any] = {
        "data_dir": data_dir,
        "db_path": data_dir / "app.db",
        "creation_path": None,
        "errors": [],
    }

    first = Sidecar(data_dir, "boot1")
    second: Optional[Sidecar] = None
    try:
        # ---- boot 1 ---------------------------------------------------
        first.start()
        result["db_after_boot1"] = _db_snapshot(result["db_path"])
        client = first.client()
        try:
            # Sanity: the token-guarded readiness probe works.
            ready = client.get("/health/ready")
            result["ready_status_boot1"] = ready.status_code

            # ---- drive a review-first project toward a gate -----------
            niche_resp = client.post(
                "/api/v1/niches",
                json={
                    "name": "Acceptance D Facts",
                    "idea": "Persistence acceptance facts for gate approvals",
                    "language": "en-US",
                    "tone": "neutral",
                },
            )
            result["niche_status"] = niche_resp.status_code
            niche_id = (
                niche_resp.json().get("id") if niche_resp.status_code == 201 else None
            ) or "facts"

            project_resp = client.post(
                "/api/v1/projects",
                json={
                    "channel_id": "chan-acceptance-d",
                    "niche_profile_id": niche_id,
                    "topic_mode": "manual",
                    "topic": "Why approvals must survive restarts",
                    "target_minutes": 8,
                    "publish_mode": "review_first",
                },
                headers={"Idempotency-Key": f"accd-project-{uuid.uuid4().hex}"},
            )
            result["project_status"] = project_resp.status_code
            assert project_resp.status_code == 201, (
                f"project creation failed: {project_resp.status_code} "
                f"{project_resp.text}"
            )
            project = project_resp.json()
            result["api_project_id"] = project["id"]
            result["project_publish_mode"] = project.get("publish_mode")

            # Seed the DB-resident review-first project (FK parent for
            # approvals rows) — see _seed_db_project.  The gate flow is
            # driven on THIS project; the API project above proves the
            # REST creation surface, the seeded one can own approvals.
            seeded_project_id = _seed_db_project(result["db_path"])
            result["project_id"] = seeded_project_id

            start_resp = client.post(
                f"/api/v1/projects/{seeded_project_id}/pipeline/start",
                json={},
                headers={"Idempotency-Key": f"accd-start-{uuid.uuid4().hex}"},
            )
            result["pipeline_start_status"] = start_resp.status_code

            # ---- find (or fall back to) a pending approval ------------
            # Preferred: the execution layer parks the project at a gate
            # and the gates bridge INSERTs a pending approvals row, which
            # the queue endpoint then lists for this project.
            approval_id: Optional[str] = None
            deadline = time.monotonic() + GATE_POLL_TIMEOUT_S
            while time.monotonic() < deadline:
                items = _queue_items(client)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("project_id") == seeded_project_id and (
                        item.get("status") == "pending"
                        or not _item_shows_approved(item)
                    ):
                        approval_id = item.get("id") or item.get("approval_id")
                        break
                if approval_id:
                    break
                time.sleep(1.0)
            approve_body: dict[str, Any] = {}
            if approval_id:
                result["creation_path"] = (
                    "pending-row path: gates_bridge pending approvals row "
                    "for the seeded review-first project (pipeline/start -> "
                    "execution DAG -> gate), decided with an empty body"
                )
            else:
                # Fallback (documented in the module docstring): the stub
                # DAG did not reach a gate in time, so use the Round 2
                # router's insert-with-body-fields persistence path —
                # the body carries project_id + gate, so the decision is
                # INSERTed as a decided approvals row.
                approval_id = f"gate-a-{seeded_project_id}"
                approve_body = {"project_id": seeded_project_id, "gate": "a"}
                result["creation_path"] = (
                    "insert-with-body-fields path: approve body carried "
                    "project_id + gate='a' for the seeded review-first "
                    "project (no pending gate row materialised in time)"
                )
            result["approval_id"] = approval_id
            result["queue_before_decision"] = _queue_items(client)

            # ---- approve the pending gate ------------------------------
            approve_resp = client.post(
                f"/api/v1/approvals/{approval_id}/approve",
                json=approve_body,
                headers={"Idempotency-Key": f"accd-approve-{uuid.uuid4().hex}"},
            )
            result["approve_status"] = approve_resp.status_code
            try:
                result["approve_body"] = approve_resp.json()
            except Exception:
                result["approve_body"] = {"raw": approve_resp.text}
            result["queue_after_decision_boot1"] = _queue_items(client)
        finally:
            client.close()
        result["db_after_decision"] = _db_snapshot(result["db_path"])

        # ---- restart against the SAME data dir -------------------------
        first.stop()

        second = Sidecar(data_dir, "boot2")
        second.start()
        result["booted_second_time"] = True
        client2 = second.client()
        try:
            result["queue_after_restart"] = _queue_items(client2)
            # Decisive probe: a NEW Idempotency-Key (so the persisted
            # idempotency store cannot replay the original 200) on the
            # same approval in a FRESH process.  Persisted decisions ->
            # 409 already_decided.  In-memory decisions -> 200 again.
            second_approve = client2.post(
                f"/api/v1/approvals/{result['approval_id']}/approve",
                json={},
                headers={"Idempotency-Key": f"accd-reapprove-{uuid.uuid4().hex}"},
            )
            result["second_approve_status"] = second_approve.status_code
            result["second_approve_code"] = _error_code(second_approve)
            try:
                result["second_approve_body"] = second_approve.json()
            except Exception:
                result["second_approve_body"] = {"raw": second_approve.text}
            proj_resp = client2.get(f"/api/v1/projects/{result['project_id']}")
            result["project_status_after_restart"] = proj_resp.status_code
            if proj_resp.status_code == 200:
                result["project_after_restart"] = proj_resp.json()
        finally:
            client2.close()
        result["db_after_restart"] = _db_snapshot(result["db_path"])
        return result
    finally:
        first.stop()
        if second is not None:
            second.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migration_0002_exists() -> None:
    """Migration 0002 (approvals table) must exist under backend/migrations."""
    path = _find_migration_0002()
    assert path is not None, (
        "BLOCKER: no Alembic migration 0002 found in "
        f"{MIGRATIONS_VERSIONS_DIR} (files present: "
        f"{sorted(p.name for p in MIGRATIONS_VERSIONS_DIR.glob('*.py')) if MIGRATIONS_VERSIONS_DIR.is_dir() else '<dir missing>'}"
        "). Round 2's approvals-persistence work (approvals table) has "
        "not landed — approvals decisions cannot be persisted."
    )
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert "0001" in text, (
        f"migration {path.name} does not chain from revision 0001 "
        "(no down_revision reference to 0001 found)"
    )


def test_migration_0002_applied_in_booted_db(scenario: dict[str, Any]) -> None:
    """After boot, app.db must show migration 0002's effect.

    Accepted evidence (any one, checked in the post-restart snapshot so
    it reflects the final state of the shared data dir):
    * an approvals table (one the migration creates, or one of the
      queue router's candidate tables) exists in app.db; or
    * ``alembic_version`` records a 0002* revision; or
    * ``schema_meta`` version has advanced beyond the Round 1 baseline
      (1) together with an approvals table being present.
    """
    migration = _find_migration_0002()
    assert migration is not None, (
        "BLOCKER: migration 0002 missing — cannot be applied. "
        "See test_migration_0002_exists."
    )
    snap = scenario["db_after_restart"]
    assert snap["exists"], f"no app.db was created at {scenario['db_path']}"

    migration_tables = _migration_table_names(migration)
    expected_tables = set(CANDIDATE_APPROVAL_TABLES) | migration_tables
    found_tables = expected_tables & snap["tables"]
    alembic_v = str(snap["alembic_version"] or "")
    schema_v = snap["schema_meta_version"]

    applied = bool(found_tables) or alembic_v.startswith("0002") or (
        schema_v is not None and schema_v >= 2 and bool(found_tables)
    )
    assert applied, (
        f"migration 0002 ({migration.name}) exists but shows no effect in "
        f"the booted DB at {scenario['db_path']}: tables present="
        f"{sorted(snap['tables'])}, expected one of {sorted(expected_tables)}, "
        f"alembic_version={snap['alembic_version']!r}, "
        f"schema_meta_version={schema_v!r}. The sidecar boot path "
        "(app.main -> app.db.init_db / schema DDL) does not apply it, "
        "so the approvals table is never created."
    )
    scenario["applied_evidence"] = {
        "migration": str(migration),
        "tables_found": sorted(found_tables),
        "alembic_version": snap["alembic_version"],
        "schema_meta_version": schema_v,
    }


def test_approval_decision_persists_across_restart(scenario: dict[str, Any]) -> None:
    """The approve decision must survive a full sidecar restart."""
    # Diagnostic (visible with -s / in CI logs): which of the Round 2
    # persistence paths produced the approval that was decided.
    print(
        f"[acceptance-d] creation_path={scenario.get('creation_path')!r} "
        f"approval_id={scenario.get('approval_id')!r} "
        f"pipeline_start_status={scenario.get('pipeline_start_status')!r}"
    )
    # Pre-restart decision succeeded.
    assert scenario["approve_status"] == 200, (
        f"initial approve failed: status={scenario.get('approve_status')} "
        f"body={scenario.get('approve_body')!r} "
        f"(creation_path={scenario.get('creation_path')!r})"
    )
    body = scenario["approve_body"] or {}
    assert str(body.get("decision") or "").lower() in ("approved", ""), body
    if body.get("decision"):
        assert body["decision"] == "approved"

    assert scenario.get("booted_second_time"), "sidecar did not reboot"

    approval_id = scenario["approval_id"]

    # (a) Queue state after restart still records the decision, when the
    # queue surfaces this approval at all.
    queue_after = scenario["queue_after_restart"]
    item = _item_for(queue_after, approval_id)
    if item is not None:
        assert _item_shows_approved(item), (
            f"approval {approval_id} is back in the queue after restart "
            f"but does not show as approved/decided: {item!r}"
        )

    # (b) Direct DB evidence: the approvals table (migration 0002) must
    # hold a row for this approval carrying the approved decision.  Both
    # Round 2 persistence paths (pending-row UPDATE, insert-with-body)
    # write exactly such a row, so when the table is readable this is a
    # hard assertion, not a soft probe.
    snap = scenario["db_after_restart"]
    db_row_proves_it = False
    for _table, rows in (snap["approval_rows"] or {}).items():
        for row in rows:
            if row.get("id") == approval_id or row.get("approval_id") == approval_id:
                blob = json.dumps(row, default=str).lower()
                if "approved" in blob or "decided" in blob:
                    db_row_proves_it = True
    scenario["db_row_proves_decision"] = db_row_proves_it
    if "approvals" in snap["tables"]:
        assert db_row_proves_it, (
            f"approvals table exists in {scenario['db_path']} but holds "
            f"no decided/approved row for approval {approval_id}: "
            f"rows={snap['approval_rows']!r}"
        )

    # (c) DECISIVE: second approve in the fresh process must be a 409
    # already_decided (new Idempotency-Key, so no replay masking).  An
    # in-memory decision store returns 200 here — the Round 1 bug.
    assert scenario["second_approve_status"] == 409, (
        "approval decision did NOT survive restart: second approve in a "
        f"fresh process returned {scenario['second_approve_status']} "
        f"(body={scenario.get('second_approve_body')!r}) instead of 409 "
        "already_decided. The decision was held in memory (Round 1 "
        "behaviour) or the persisted record is not consulted on boot. "
        f"creation_path={scenario.get('creation_path')!r}; "
        f"db tables={sorted(snap['tables'])}; "
        f"db approval rows={snap['approval_rows']!r}"
    )
    code = (scenario.get("second_approve_code") or "").lower()
    body2 = json.dumps(scenario.get("second_approve_body") or {}, default=str).lower()
    assert "already_decided" in code or "already_decided" in body2 or "already" in body2, (
        f"second approve returned 409 but not an already-decided error: "
        f"code={scenario.get('second_approve_code')!r} "
        f"body={scenario.get('second_approve_body')!r}"
    )
