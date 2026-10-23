#!/usr/bin/env python3
"""Round 6 bounded SOAK / STABILITY harness.

Boots the real sidecar (``python -m app.main``) as a subprocess in dev
mode — its own ephemeral port, throwaway data dir under
``outputs/round6/soak/state/``, a fresh per-launch token — and drives it
through the real client surface (REST + WebSocket, launch-token auth)
for a fixed window:

* **Produce/QC cycles** — ``POST /api/v1/projects`` (manual topic,
  ``target_minutes=1``, ``publish_mode='auto'``) and, on alternating
  cycles, ``POST /api/v1/schedules/{id}/run-now`` (a real synchronous
  ``SchedulerTick`` that materialises a project).  Each cycle is awaited
  until its 10-job DAG reaches terminal job states (read back from the
  app DB, the same ground truth the Round-4 proof used).

  Content path is the cheapest *honest* one: the subprocess environment
  carries NO provider keys (``GEMINI_API_KEY`` / ``PIXABAY_API_KEY`` /
  YouTube credentials are stripped, presence recorded only), so the
  content layer takes its documented no-key deterministic path
  (``llm_path="deterministic-stub"``, $0, never a silent substitution —
  R4-003 fail-loud semantics are untouched), stock falls back to local
  plates, and publish stays on the honestly-labelled fake provider.
  No real LLM call is ever made by this harness.

* **WebSocket churn** — repeated ticket exchange
  (``POST /api/v1/ws-ticket``), connect, ``client.hello`` /
  ``system.hello_ack``, ``client.ping`` / ``client.pong``,
  subscribe/unsubscribe, clean disconnect — plus an occasional
  bad-ticket connect that must be rejected with close code 4401.

* **Sampling** — every ~5 s: sidecar RSS + full process-tree RSS
  (via ``/proc``, no psutil dependency), descendant count, ffmpeg
  descendant count, and system-wide orphaned ffmpeg processes
  (parent dead / reparented to PID 1).

At the end: a drain wait, ``PRAGMA integrity_check`` (+ foreign-key
check) on the app DB, sidecar-log error scan, then a clean shutdown
(SIGTERM, SIGKILL only as fallback) and verification that the process
is really gone and no orphan ffmpeg remains.

Outputs (``outputs/round6/soak/``):

* ``soak_result.json`` — machine-readable: window, cycle records,
  memory series + start/peak/end/growth, slope per cycle, orphans,
  integrity verdict, error counts + samples.
* ``soak_summary.md`` — short human summary of the same.

Usage (from the repo root)::

    backend/.venv/bin/python tools/round6_soak.py [--minutes 25]
        [--cycle-timeout 600] [--ws-per-cycle 3] [--keep-state]

Exit code: 0 when the run completed and integrity passed (individual
cycle failures are *data*, reported in the result, not a harness
crash); 2 when the sidecar could not boot at all; 1 on harness error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
VENV_PY = BACKEND_DIR / ".venv" / "bin" / "python"

OUTPUT_ROOT = REPO_ROOT / "outputs" / "round6" / "soak"
STATE_DIR = OUTPUT_ROOT / "state"
DATA_DIR = STATE_DIR / "data"
PROJECTS_DIR = STATE_DIR / "projects"
DB_PATH = DATA_DIR / "app.db"
SIDECAR_LOG = STATE_DIR / "sidecar.log"
RESULT_PATH = OUTPUT_ROOT / "soak_result.json"
SUMMARY_PATH = OUTPUT_ROOT / "soak_summary.md"

TERMINAL_JOB_STATUSES = {"done", "succeeded", "failed", "dead", "cancelled"}
TOPICS = [
    "Samundar ki Gehrai ke Raaz",
    "Chand ke Doosre Rukh ke Raaz",
    "Barish Kaise Hoti Hai",
    "Purane Zamane ke Hairat Angez Waqiat",
]
PROVIDER_ENV_KEYS = (
    "GEMINI_API_KEY", "PIXABAY_API_KEY", "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET", "AI33_API_KEY",
)


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# /proc process-tree sampling (no psutil on this VM)
# ---------------------------------------------------------------------------


def _proc_table() -> dict[int, dict[str, Any]]:
    table: dict[int, dict[str, Any]] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text().split()
            # comm may contain spaces/parens: fields after final ')'
            raw = (entry / "stat").read_text()
            lparen, rparen = raw.find("("), raw.rfind(")")
            comm = raw[lparen + 1:rparen]
            fields = raw[rparen + 2:].split()
            ppid = int(fields[1])
            rss_kb = 0
            status = (entry / "status").read_text(errors="replace")
            m = re.search(r"^VmRSS:\s+(\d+)\s*kB", status, re.M)
            if m:
                rss_kb = int(m.group(1))
            cmdline = ""
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(
                    b"\x00", b" "
                ).decode(errors="replace").strip()
            except Exception:
                pass
            table[pid] = {
                "pid": pid, "ppid": ppid, "comm": comm,
                "rss_kb": rss_kb, "cmdline": cmdline,
            }
        except Exception:
            continue
    return table


def tree_sample(root_pid: int) -> dict[str, Any]:
    table = _proc_table()
    descendants: set[int] = set()
    frontier = [root_pid]
    while frontier:
        cur = frontier.pop()
        for pid, info in table.items():
            if info["ppid"] == cur and pid not in descendants:
                descendants.add(pid)
                frontier.append(pid)
    root = table.get(root_pid)
    ffmpeg_desc = [
        p for p in descendants
        if "ffmpeg" in table.get(p, {}).get("comm", "")
        or "ffmpeg" in table.get(p, {}).get("cmdline", "")
    ]
    # Orphaned media processes system-wide: ffmpeg whose parent is
    # gone (reparented to PID 1) or whose parent is not in the table.
    orphans = []
    for pid, info in table.items():
        is_ffmpeg = "ffmpeg" in info["comm"] or (
            "ffmpeg" in info["cmdline"] and "grep" not in info["cmdline"]
        )
        if is_ffmpeg and (info["ppid"] == 1 or info["ppid"] not in table):
            orphans.append({"pid": pid, "ppid": info["ppid"],
                            "cmdline": info["cmdline"][:160]})
    return {
        "sidecar_rss_kb": root["rss_kb"] if root else None,
        "tree_rss_kb": (root["rss_kb"] if root else 0)
        + sum(table[p]["rss_kb"] for p in descendants if p in table),
        "descendants": len(descendants),
        "ffmpeg_descendants": len(ffmpeg_desc),
        "ffmpeg_orphans_systemwide": orphans,
    }


# ---------------------------------------------------------------------------
# DB helpers (read-mostly; fixtures seeded like the Round-4 proof)
# ---------------------------------------------------------------------------


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    cols = [c for c in values if c in _cols(conn, table) and values[c] is not None]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(values[c] for c in cols),
    )


def seed_fixtures(db_path: Path) -> dict[str, str]:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.niches.builtins import get_profile

    profile = get_profile("facts")
    ts = int(time.time() * 1000)
    niche_id = str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _insert(conn, "niche_profiles", {
            "id": niche_id, "key": profile.profile_id, "label": profile.name,
            "source": "builtin", "user_idea": None,
            "language_mix": profile.language,
            "tone": ", ".join(profile.tone),
            "words_per_minute": profile.target_wpm,
            "structure_json": json.dumps(profile.structure_template),
            "hook_style": ", ".join(profile.hook_families),
            "voice_style": profile.voice_style,
            "visual_style": profile.visual_style,
            "cta_style": profile.cta_style,
            "risk_class": "normal",
            "taboo_json": json.dumps(profile.taboo),
            "is_active": 1, "created_at": ts, "updated_at": ts,
        })
        _insert(conn, "channels", {
            "id": channel_id, "youtube_channel_id": "UC-round6-soak",
            "title": "Round 6 Soak — Facts",
            "default_niche_profile_id": niche_id, "language": "ur",
            "status": "active", "created_at": ts, "updated_at": ts,
        })
        conn.commit()
    finally:
        conn.close()
    return {"channel_id": channel_id, "niche_profile_id": niche_id}


def project_jobs(db_path: Path, project_id: str) -> list[dict]:
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT kind, status, attempts, error_code FROM jobs "
                "WHERE project_id = ? ORDER BY created_at", (project_id,))]
        finally:
            conn.close()
    except Exception:
        return []


def integrity_check(db_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"path": str(db_path)}
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            out["integrity_check"] = rows
            out["ok"] = rows == ["ok"]
            fk = [tuple(r) for r in conn.execute("PRAGMA foreign_key_check")]
            out["foreign_key_violations"] = len(fk)
            out["db_bytes"] = db_path.stat().st_size
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Soak driver
# ---------------------------------------------------------------------------


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.token = secrets.token_urlsafe(32)
        self.boot_id = uuid.uuid4().hex
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.base: Optional[str] = None
        self.result: dict[str, Any] = {
            "harness": str(Path(__file__).resolve()),
            "started_at": now_iso(),
            "target_minutes": args.minutes,
            "provider_keys_present_in_parent_env": {
                k: bool(os.environ.get(k)) for k in PROVIDER_ENV_KEYS
            },
            "content_path": (
                "no-key deterministic (provider keys stripped from the "
                "sidecar env; llm_path='deterministic-stub', $0, fake "
                "publish provider — all honestly labelled by the app)"
            ),
            "cycles": [],
            "ws_sessions": {"ok": 0, "failed": 0, "bad_ticket_rejected": 0,
                            "bad_ticket_accepted": 0, "errors": []},
            "http": {"requests": 0, "errors_5xx": 0, "errors_4xx": 0,
                     "samples_5xx": []},
            "samples": [],
            "boot_attempts": [],
        }
        self._stop_sampler = threading.Event()
        self._lock = threading.Lock()

    # -- sidecar lifecycle ---------------------------------------------
    def boot(self) -> bool:
        import httpx

        env = dict(os.environ)
        for key in PROVIDER_ENV_KEYS:
            env.pop(key, None)
        env.pop("GEMINI_API_KEY", None)
        env.update({
            "YTA_TOKEN": self.token,
            "YTA_BOOT_ID": self.boot_id,
            "YTA_PORT": "0",
            "YTA_DATA_DIR": str(DATA_DIR),
            "YTA_PROJECTS_DIR": str(PROJECTS_DIR),
            "YTA_SECRET_BACKEND": "env",
            "YTA_LOG_LEVEL": "info",
            "TMPDIR": str(REPO_ROOT / ".tmp-yta"),
            # Sanitised NO_PROXY (the sandbox's bracketed-IPv6 NO_PROXY
            # breaks httpx — see backend/app/providers/http_client.py).
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })
        for attempt in range(1, 4):
            if SIDECAR_LOG.exists():
                SIDECAR_LOG.unlink()
            logf = open(SIDECAR_LOG, "wb")
            self.proc = subprocess.Popen(
                [str(VENV_PY), "-m", "app.main"],
                cwd=str(BACKEND_DIR), env=env,
                stdout=logf, stderr=subprocess.STDOUT,
            )
            port = None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                try:
                    for line in SIDECAR_LOG.read_text(
                        errors="replace"
                    ).splitlines():
                        if '"listening"' in line:
                            port = int(json.loads(line)["port"])
                            break
                except Exception:
                    pass
                if port:
                    break
                time.sleep(0.4)
            attempt_rec: dict[str, Any] = {"attempt": attempt, "port": port}
            if port is None:
                tail = ""
                try:
                    tail = SIDECAR_LOG.read_text(errors="replace")[-1200:]
                except Exception:
                    pass
                attempt_rec["died"] = True
                attempt_rec["exit_code"] = self.proc.poll()
                attempt_rec["log_tail"] = tail
                self.result["boot_attempts"].append(attempt_rec)
                try:
                    self.proc.kill()
                except Exception:
                    pass
                time.sleep(15)
                continue
            self.port = port
            self.base = f"http://127.0.0.1:{port}"
            # Health gate (real client path).
            ok = False
            with httpx.Client(base_url=self.base, timeout=10,
                              trust_env=False) as client:
                for _ in range(60):
                    try:
                        r = client.get("/health/live")
                        if r.status_code == 200:
                            ok = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.5)
            attempt_rec["health_live_ok"] = ok
            self.result["boot_attempts"].append(attempt_rec)
            if ok:
                self.result["sidecar_pid"] = self.proc.pid
                self.result["port"] = port
                return True
            try:
                self.proc.kill()
            except Exception:
                pass
            time.sleep(10)
        return False

    def shutdown(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"method": None, "exit_code": None}
        if self.proc is None:
            rec["method"] = "no_process"
            return rec
        pid = self.proc.pid
        rec["method"] = "SIGTERM"
        t_down = time.monotonic()
        try:
            self.proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            rec["method"] = "SIGTERM_then_SIGKILL"
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=15)
            except Exception:
                pass
        rec["exit_code"] = self.proc.poll()
        rec["seconds_to_exit"] = round(time.monotonic() - t_down, 1)
        # Verify: pid gone + port closed.
        time.sleep(1)
        rec["pid_alive_after"] = Path(f"/proc/{pid}").exists()
        import socket as _socket

        s = _socket.socket()
        s.settimeout(2)
        try:
            rec["port_connectable_after"] = s.connect_ex(
                ("127.0.0.1", self.port or 0)) == 0
        finally:
            s.close()
        orphans = tree_sample(pid)["ffmpeg_orphans_systemwide"]
        rec["ffmpeg_orphans_after_shutdown"] = orphans
        return rec

    # -- HTTP helpers ----------------------------------------------------
    def client(self):
        import httpx

        return httpx.Client(
            base_url=self.base,
            headers={"X-Local-Token": self.token},
            timeout=60, trust_env=False,
        )

    def call(self, client, method: str, path: str, **kw):
        with self._lock:
            self.result["http"]["requests"] += 1
        try:
            resp = client.request(method, path, **kw)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.result["http"]["errors_5xx"] += 1
                if len(self.result["http"]["samples_5xx"]) < 10:
                    self.result["http"]["samples_5xx"].append(
                        f"{method} {path}: {type(exc).__name__}: {exc}")
            raise
        with self._lock:
            if resp.status_code >= 500:
                self.result["http"]["errors_5xx"] += 1
                if len(self.result["http"]["samples_5xx"]) < 10:
                    self.result["http"]["samples_5xx"].append(
                        f"{method} {path} -> {resp.status_code}: "
                        f"{resp.text[:200]}")
            elif resp.status_code >= 400:
                self.result["http"]["errors_4xx"] += 1
        return resp

    # -- WS churn ---------------------------------------------------------
    def ws_session(self, client, bad_ticket: bool = False) -> None:
        from websockets.sync.client import connect as ws_connect

        ws_rec = self.result["ws_sessions"]
        url = f"ws://127.0.0.1:{self.port}/api/v1/ws"
        try:
            if bad_ticket:
                ticket = "bogus-" + uuid.uuid4().hex
            else:
                r = self.call(client, "POST", "/api/v1/ws-ticket")
                ticket = r.json()["ticket"]
            with ws_connect(url, open_timeout=10, close_timeout=5) as ws:
                ws.send(json.dumps({
                    "type": "client.hello", "ticket": ticket,
                    "ws_schema_versions": [1], "subscriptions": ["all"],
                }))
                msg = json.loads(ws.recv(timeout=10))
                if bad_ticket:
                    ws_rec["bad_ticket_accepted"] += 1
                    return
                if msg.get("type") != "system.hello_ack":
                    raise RuntimeError(f"unexpected first frame {msg.get('type')}")
                ws.send(json.dumps({"type": "client.ping"}))
                msg2 = json.loads(ws.recv(timeout=10))
                if msg2.get("type") != "client.pong":
                    raise RuntimeError(f"ping -> {msg2.get('type')}")
                ws.send(json.dumps({
                    "type": "client.subscribe", "topics": ["jobs", "projects"],
                }))
                ws.send(json.dumps({
                    "type": "client.unsubscribe", "topics": ["jobs"],
                }))
            ws_rec["ok"] += 1
        except Exception as exc:  # noqa: BLE001
            if bad_ticket:
                # Rejection surfaces as a failed handshake/recv/close 4401.
                ws_rec["bad_ticket_rejected"] += 1
            else:
                ws_rec["failed"] += 1
                if len(ws_rec["errors"]) < 10:
                    ws_rec["errors"].append(
                        f"{type(exc).__name__}: {exc}"[:300])

    # -- sampler ------------------------------------------------------------
    def sampler(self, t0: float) -> None:
        while not self._stop_sampler.is_set():
            if self.proc is not None and self.proc.poll() is None:
                s = tree_sample(self.proc.pid)
                s["t_s"] = round(time.monotonic() - t0, 1)
                self.result["samples"].append(s)
            self._stop_sampler.wait(5.0)

    # -- one produce cycle ---------------------------------------------------
    def wait_cycle_terminal(self, project_id: str, timeout_s: float,
                            client) -> dict[str, Any]:
        started = time.monotonic()
        last_status = None
        while time.monotonic() - started < timeout_s:
            jobs = project_jobs(DB_PATH, project_id)
            if jobs and all(
                j["status"] in TERMINAL_JOB_STATUSES for j in jobs
            ):
                proj_status = None
                try:
                    r = self.call(client, "GET", f"/api/v1/projects/{project_id}")
                    proj_status = r.json().get("status")
                except Exception:
                    pass
                failed = [j["kind"] for j in jobs
                          if j["status"] not in ("done", "succeeded")]
                return {
                    "outcome": "terminal",
                    "duration_s": round(time.monotonic() - started, 1),
                    "project_status": proj_status,
                    "jobs_terminal": len(jobs),
                    "jobs_failed": failed,
                }
            time.sleep(3.0)
        return {"outcome": "timeout", "duration_s": round(timeout_s, 1),
                "last_project_status": last_status}

    def run(self) -> int:
        if not self.boot():
            self.result["verdict"] = "boot_failed"
            self.finish()
            return 2
        fixtures = seed_fixtures(DB_PATH)
        self.result["fixtures"] = fixtures
        t0 = time.monotonic()
        deadline = t0 + self.args.minutes * 60
        sampler_thread = threading.Thread(
            target=self.sampler, args=(t0,), daemon=True)
        sampler_thread.start()

        client = self.client()
        schedule_id: Optional[str] = None
        try:
            # Schedule fixture via the real API (one schedule; run-now
            # exercises the SchedulerTick path on alternating cycles).
            try:
                r = self.call(client, "POST", "/api/v1/schedules",
                          headers={"Idempotency-Key": str(uuid.uuid4())},
                          json={
                              "channel_id": fixtures["channel_id"],
                              "niche_profile_id": fixtures["niche_profile_id"],
                              "name": "Round 6 soak schedule",
                              "cadence": {"type": "interval",
                                          "interval_hours": 24,
                                          "topics": TOPICS},
                              "topic_mode": "fixed_list",
                              "target_minutes": 1,
                              "publish_mode": "auto",
                          })
                self.result["schedule_create_status"] = r.status_code
                if r.status_code in (200, 201):
                    schedule_id = r.json().get("id")
                else:
                    self.result["schedule_create_body"] = r.text[:300]
            except Exception as exc:  # noqa: BLE001 — schedule path is
                # one driver; project cycles continue without it.
                self.result["schedule_create_error"] = (
                    f"{type(exc).__name__}: {exc}"[:300])
            self.result["schedule_id"] = schedule_id

            cycle_no = 0
            while time.monotonic() < deadline:
                cycle_no += 1
                cyc: dict[str, Any] = {
                    "cycle": cycle_no,
                    "started_at": now_iso(),
                    "t_offset_s": round(time.monotonic() - t0, 1),
                }
                use_schedule = (
                    schedule_id is not None and cycle_no % 2 == 0)
                project_id: Optional[str] = None
                try:
                    if use_schedule:
                        cyc["driver"] = "schedule_run_now"
                        r = self.call(
                            client, "POST",
                            f"/api/v1/schedules/{schedule_id}/run-now",
                            headers={"Idempotency-Key": str(uuid.uuid4())})
                        cyc["run_now_status"] = r.status_code
                        if r.status_code == 202:
                            project_id = r.json().get("project_id")
                            cyc["tick"] = r.json().get("tick")
                    else:
                        cyc["driver"] = "project_create"
                        r = self.call(
                            client, "POST", "/api/v1/projects",
                            headers={"Idempotency-Key": str(uuid.uuid4())},
                            json={
                                "channel_id": fixtures["channel_id"],
                                "niche_profile_id": fixtures["niche_profile_id"],
                                "topic_mode": "manual",
                                "topic": TOPICS[(cycle_no - 1) % len(TOPICS)],
                                "target_minutes": 1,
                                "publish_mode": "auto",
                            })
                        cyc["create_status"] = r.status_code
                        if r.status_code == 201:
                            project_id = r.json().get("id")
                    cyc["project_id"] = project_id
                    if project_id:
                        remaining = deadline - time.monotonic()
                        budget = min(self.args.cycle_timeout,
                                     max(60.0, remaining))
                        cyc.update(self.wait_cycle_terminal(
                            project_id, budget, client))
                    else:
                        cyc["outcome"] = "no_project_materialised"
                except Exception as exc:  # noqa: BLE001
                    cyc["outcome"] = "driver_exception"
                    cyc["error"] = f"{type(exc).__name__}: {exc}"[:400]
                # REST read churn + WS churn every cycle.
                try:
                    for path in ("/health/ready", "/api/v1/projects",
                                 "/api/v1/channels", "/api/v1/schedules",
                                 "/api/v1/system/info",
                                 "/api/v1/queue/stats"):
                        self.call(client, "GET", path)
                except Exception:
                    pass
                for i in range(self.args.ws_per_cycle):
                    self.ws_session(client, bad_ticket=(i == 0 and cycle_no > 1))
                cyc["rss_at_cycle_end_kb"] = (
                    self.result["samples"][-1]["sidecar_rss_kb"]
                    if self.result["samples"] else None)
                self.result["cycles"].append(cyc)
                print(f"cycle {cycle_no}: {cyc.get('outcome')} "
                      f"driver={cyc.get('driver')} "
                      f"dur={cyc.get('duration_s')}s "
                      f"rss={cyc.get('rss_at_cycle_end_kb')}kB", flush=True)
                # Flush a partial result after every cycle: a host/VM
                # death must not lose the whole run (run-2 lesson).
                try:
                    self.result["wall_clock_s"] = round(
                        time.monotonic() - t0, 1)
                    self.finish()
                except Exception:
                    pass
                if self.proc is None or self.proc.poll() is not None:
                    self.result["sidecar_died_mid_run"] = {
                        "after_cycle": cycle_no,
                        "exit_code": self.proc.poll() if self.proc else None,
                    }
                    break
            self.result["wall_clock_s"] = round(time.monotonic() - t0, 1)
        finally:
            client.close()
            self._stop_sampler.set()
            sampler_thread.join(timeout=10)

        # Log error scan.
        log_text = ""
        try:
            log_text = SIDECAR_LOG.read_text(errors="replace")
        except Exception:
            pass
        self.result["sidecar_log"] = {
            "bytes": len(log_text),
            "error_lines": sum(1 for ln in log_text.splitlines()
                               if "ERROR" in ln),
            "tracebacks": log_text.count("Traceback (most recent call last)"),
            "error_samples": [ln[:240] for ln in log_text.splitlines()
                              if "ERROR" in ln][:10],
        }
        self.result["integrity"] = integrity_check(DB_PATH)
        self.result["shutdown"] = self.shutdown()
        self.analyse_memory()
        self.finish()
        return 0

    # -- analysis + outputs -------------------------------------------------
    def analyse_memory(self) -> None:
        samples = [s for s in self.result["samples"]
                   if s.get("sidecar_rss_kb")]
        mem: dict[str, Any] = {}
        if samples:
            rss = [s["sidecar_rss_kb"] for s in samples]
            mem["start_kb"] = rss[0]
            mem["peak_kb"] = max(rss)
            mem["end_kb"] = rss[-1]
            mem["growth_kb"] = rss[-1] - rss[0]
            mem["growth_pct"] = round(
                100.0 * (rss[-1] - rss[0]) / max(1, rss[0]), 2)
            tree = [s["tree_rss_kb"] for s in samples if s.get("tree_rss_kb")]
            if tree:
                mem["tree_peak_kb"] = max(tree)
                mem["tree_end_kb"] = tree[-1]
            mem["max_descendants"] = max(
                s["descendants"] for s in samples)
            mem["max_ffmpeg_descendants"] = max(
                s["ffmpeg_descendants"] for s in samples)
        cycle_rss = [(c["cycle"], c["rss_at_cycle_end_kb"])
                     for c in self.result["cycles"]
                     if c.get("rss_at_cycle_end_kb")]
        if len(cycle_rss) >= 3:
            xs = [float(x) for x, _ in cycle_rss]
            ys = [float(y) for _, y in cycle_rss]
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            denom = sum((x - mx) ** 2 for x in xs)
            slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                     / denom) if denom else 0.0
            mem["rss_slope_kb_per_cycle"] = round(slope, 1)
            mem["cycle_end_rss"] = cycle_rss
        orphans_seen = []
        for s in self.result["samples"]:
            orphans_seen.extend(s.get("ffmpeg_orphans_systemwide") or [])
        mem["ffmpeg_orphans_seen_during_run"] = orphans_seen
        self.result["memory"] = mem

    def auto_findings(self) -> list[str]:
        r = self.result
        out: list[str] = []
        for a in r.get("boot_attempts", []):
            if a.get("died"):
                out.append(
                    f"Boot attempt {a['attempt']} died (exit "
                    f"{a.get('exit_code')}): "
                    f"{(a.get('log_tail') or '')[-300:]}")
        if r.get("sidecar_died_mid_run"):
            out.append(f"Sidecar DIED mid-run: {r['sidecar_died_mid_run']}")
        mem = r.get("memory", {})
        slope = mem.get("rss_slope_kb_per_cycle")
        if slope is not None and slope > 1024:
            out.append(
                f"Possible memory growth: sidecar RSS slope "
                f"{slope} kB/cycle across "
                f"{len(mem.get('cycle_end_rss') or [])} cycles "
                f"(total growth {mem.get('growth_kb')} kB).")
        elif slope is not None:
            out.append(
                f"No leak signal: sidecar RSS slope {slope} kB/cycle, "
                f"total growth {mem.get('growth_kb')} kB "
                f"({mem.get('growth_pct')}%) over the run.")
        orphans = mem.get("ffmpeg_orphans_seen_during_run") or []
        if orphans:
            out.append(
                f"Orphaned ffmpeg processes observed during run: "
                f"{orphans[:5]}")
        shut = r.get("shutdown", {})
        if shut.get("ffmpeg_orphans_after_shutdown"):
            out.append(
                "Orphaned ffmpeg AFTER shutdown: "
                f"{shut['ffmpeg_orphans_after_shutdown']}")
        if shut.get("method") == "SIGTERM_then_SIGKILL":
            out.append(
                "Sidecar did not exit within 25 s of SIGTERM "
                "(SIGKILL required) — graceful-shutdown latency finding.")
        if shut.get("pid_alive_after"):
            out.append("Sidecar pid STILL ALIVE after shutdown sequence!")
        integ = r.get("integrity", {})
        if integ and not integ.get("ok"):
            out.append(f"DB integrity_check FAILED: {integ}")
        elif integ:
            out.append("DB integrity_check: ok (foreign_key_check clean)."
                       if integ.get("foreign_key_violations") == 0 else
                       f"DB integrity ok but {integ.get('foreign_key_violations')} FK violations.")
        http = r.get("http", {})
        if http.get("errors_5xx"):
            out.append(
                f"HTTP 5xx/timeout errors: {http['errors_5xx']} — samples: "
                f"{http.get('samples_5xx', [])[:3]}")
        failed_cycles = [c for c in r.get("cycles", [])
                         if c.get("outcome") != "terminal"]
        if failed_cycles:
            out.append(
                f"Cycles not reaching terminal: "
                f"{[(c.get('cycle'), c.get('outcome')) for c in failed_cycles]}")
        job_failed = [(c.get("cycle"), c.get("jobs_failed"))
                      for c in r.get("cycles", []) if c.get("jobs_failed")]
        if job_failed:
            out.append(f"Cycles with failed jobs: {job_failed}")
        ws = r.get("ws_sessions", {})
        if ws.get("failed"):
            out.append(f"WS sessions failed: {ws['failed']} — "
                       f"{ws.get('errors', [])[:3]}")
        if ws.get("bad_ticket_accepted"):
            out.append("SECURITY: a bad WS ticket was ACCEPTED.")
        log = r.get("sidecar_log", {})
        if log.get("tracebacks"):
            out.append(
                f"Sidecar log contains {log['tracebacks']} traceback(s), "
                f"{log.get('error_lines')} ERROR lines — see state/sidecar.log.")
        if r.get("harness_error"):
            out.append(f"Harness error (run cut short): {r['harness_error']}")
        return out

    def finish(self) -> None:
        self.result["finished_at"] = now_iso()
        self.result["findings"] = self.auto_findings()
        cycles = self.result["cycles"]
        completed = sum(1 for c in cycles if c.get("outcome") == "terminal")
        http = self.result["http"]
        total_req = max(1, http["requests"])
        self.result["headline"] = {
            "cycles_attempted": len(cycles),
            "cycles_terminal": completed,
            "http_requests": http["requests"],
            "http_5xx": http["errors_5xx"],
            "http_error_rate_pct": round(
                100.0 * http["errors_5xx"] / total_req, 2),
            "ws_ok": self.result["ws_sessions"]["ok"],
            "ws_failed": self.result["ws_sessions"]["failed"],
            "integrity_ok": self.result.get("integrity", {}).get("ok"),
        }
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(self.result, indent=2, ensure_ascii=False,
                       default=str), encoding="utf-8")
        SUMMARY_PATH.write_text(self.summary_md(), encoding="utf-8")
        print(f"result: {RESULT_PATH}")
        print(f"summary: {SUMMARY_PATH}")
        print(json.dumps(self.result["headline"], indent=2))

    def summary_md(self) -> str:
        r = self.result
        h = r.get("headline", {})
        mem = r.get("memory", {})
        integ = r.get("integrity", {})
        shut = r.get("shutdown", {})
        mins = (r.get("wall_clock_s") or 0) / 60.0
        lines = [
            "# Round 6 — Soak / Stability Summary",
            "",
            f"- Run window: **{mins:.1f} min** driving "
            f"(target {r.get('target_minutes')} min), "
            f"started {r.get('started_at')}, finished {r.get('finished_at')}",
            f"- Content path: {r.get('content_path')}",
            f"- Cycles: **{h.get('cycles_terminal')}/{h.get('cycles_attempted')}** "
            "reached terminal job states",
            f"- HTTP: {h.get('http_requests')} requests, "
            f"{h.get('http_5xx')} 5xx "
            f"(error rate {h.get('http_error_rate_pct')}%), "
            f"{r['http']['errors_4xx']} 4xx",
            f"- WebSocket: {h.get('ws_ok')} clean sessions, "
            f"{h.get('ws_failed')} failed, bad-ticket rejected "
            f"{r['ws_sessions']['bad_ticket_rejected']}x / accepted "
            f"{r['ws_sessions']['bad_ticket_accepted']}x",
            f"- Memory (sidecar RSS): start "
            f"{mem.get('start_kb')} kB → peak {mem.get('peak_kb')} kB → "
            f"end {mem.get('end_kb')} kB "
            f"(growth {mem.get('growth_kb')} kB, "
            f"{mem.get('growth_pct')}%, slope "
            f"{mem.get('rss_slope_kb_per_cycle')} kB/cycle)",
            f"- Processes: max descendants {mem.get('max_descendants')}, "
            f"max ffmpeg descendants {mem.get('max_ffmpeg_descendants')}, "
            f"orphan ffmpeg during run: "
            f"{len(mem.get('ffmpeg_orphans_seen_during_run') or [])}, "
            f"after shutdown: "
            f"{len(shut.get('ffmpeg_orphans_after_shutdown') or [])}",
            f"- DB integrity: **{integ.get('integrity_check')}** "
            f"(fk violations {integ.get('foreign_key_violations')}, "
            f"db {integ.get('db_bytes')} bytes)",
            f"- Sidecar log: {r.get('sidecar_log', {}).get('error_lines')} "
            f"ERROR lines, "
            f"{r.get('sidecar_log', {}).get('tracebacks')} tracebacks",
            f"- Shutdown: {shut.get('method')}, exit "
            f"{shut.get('exit_code')}, pid alive after: "
            f"{shut.get('pid_alive_after')}, port connectable after: "
            f"{shut.get('port_connectable_after')}",
            "",
            "## Cycles",
            "",
            "| # | driver | outcome | project status | duration s | failed jobs |",
            "|---|--------|---------|----------------|-----------|-------------|",
        ]
        for c in r["cycles"]:
            lines.append(
                f"| {c.get('cycle')} | {c.get('driver')} | "
                f"{c.get('outcome')} | {c.get('project_status')} | "
                f"{c.get('duration_s')} | "
                f"{', '.join(c.get('jobs_failed') or []) or '—'} |")
        lines += ["", "## Findings", ""]
        findings = r.get("findings") or []
        lines += [f"- {f}" for f in findings] or ["- (see soak_result.json)"]
        lines.append("")
        return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=25.0)
    parser.add_argument("--cycle-timeout", type=float, default=600.0)
    parser.add_argument("--ws-per-cycle", type=int, default=3)
    parser.add_argument("--keep-state", action="store_true",
                        help="Do not wipe outputs/round6/soak/state first")
    args = parser.parse_args(argv)

    if not args.keep_state and STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / ".tmp-yta").mkdir(parents=True, exist_ok=True)

    soak = Soak(args)
    try:
        return soak.run()
    except KeyboardInterrupt:
        soak.result["verdict"] = "interrupted"
        try:
            soak.result["shutdown"] = soak.shutdown()
        except Exception:
            pass
        soak.finish()
        return 1
    except Exception as exc:  # noqa: BLE001 — a harness crash must still
        # shut the sidecar down and write the result (run-1 lesson).
        import traceback

        soak.result["verdict"] = "harness_error"
        soak.result["harness_error"] = (
            f"{type(exc).__name__}: {exc}\n"
            + "".join(traceback.format_exc(limit=8)[-1500:]))
        try:
            soak._stop_sampler.set()
        except Exception:
            pass
        try:
            soak.result["shutdown"] = soak.shutdown()
        except Exception:
            pass
        try:
            soak.result["integrity"] = integrity_check(DB_PATH)
        except Exception:
            pass
        soak.finish()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
