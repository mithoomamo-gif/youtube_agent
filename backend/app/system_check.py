"""First-run system checks (Round 6, onboarding wizard).

Every check here performs the REAL probe it claims to perform — the
wizard must prove the machine works, not collect settings and show a
fake green tick:

* ``webview2`` — Windows only. Looks the Edge WebView2 Runtime up in
  the registry (EdgeUpdate client key) and, failing that, on disk
  under ``Microsoft\\EdgeWebView\\Application``. On any other OS the
  honest result is ``unverified`` ("cannot verify on this OS") — never
  pass, never fail.
* ``ffmpeg`` — an ``ffmpeg`` binary must resolve through
  :mod:`app.ffmpeg_resolution` (env override → bundled ``runtime/``
  → PATH) *and* answer a real loudnorm probe: a 1-second generated
  sine is run through
  ``loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json`` (the production
  target, −14 LUFS) and the printed JSON must parse with a finite
  integrated-loudness value. (Pure silence is deliberately NOT used:
  it measures −inf LUFS, which the audio pipeline itself rejects.)
* ``disk`` — ``shutil.disk_usage`` on the data volume; reports the
  actual free bytes. Warns below 10 GiB free, fails below 1 GiB.
* ``sidecar`` — the backend's own health endpoints are called for real
  through the ASGI app (``GET /health/live``, plus ``/health/ready``
  when the caller's launch token is available to forward).

Status vocabulary: ``pass`` / ``fail`` / ``warn`` / ``unverified``.
Every result carries a human ``detail`` and a ``measured`` dict with
the raw numbers/strings the verdict rests on, so the UI never has to
invent anything.

Test seams: ``which`` / ``runner`` / ``disk_usage`` / ``platform`` /
``environ`` / ``registry_version`` / ``sidecar_probe`` are injectable;
when left as ``None`` the real stdlib functions are resolved AT CALL
TIME (so tests can also monkeypatch ``shutil.which`` etc.).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.ffmpeg_resolution import probe_loudnorm, resolve_ffmpeg, resolve_ffprobe

GIB = 1024**3
#: Free space below this on the data volume -> "warn".
DISK_WARN_FREE_BYTES = 10 * GIB
#: Free space below this -> "fail" (production cannot run safely).
DISK_FAIL_FREE_BYTES = 1 * GIB

WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
LOUDNORM_FILTER = "loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json"


def _result(
    check_id: str,
    label: str,
    status: str,
    detail: str,
    measured: Optional[dict] = None,
) -> dict:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "measured": measured or {},
    }


# ---------------------------------------------------------------------------
# WebView2 (Windows only)
# ---------------------------------------------------------------------------
def _webview2_version_from_registry() -> Optional[str]:
    """Version string from the EdgeUpdate registry keys, or None."""
    try:
        import winreg  # type: ignore[import-not-found]  # Windows only
    except Exception:
        return None
    clients = "Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_CLIENT_GUID
    locations = [
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + clients),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + clients),
        (winreg.HKEY_CURRENT_USER, "Software\\" + clients),
    ]
    for hive, subkey in locations:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
                if value and str(value).strip() not in ("", "0.0.0.0"):
                    return str(value).strip()
        except OSError:
            continue
        except Exception:
            continue
    return None


def _webview2_version_from_disk(environ: Mapping[str, str]) -> Optional[str]:
    """Version from ``<PF>\\Microsoft\\EdgeWebView\\Application\\<ver>``."""
    bases = [
        environ.get("ProgramFiles(x86)"),
        environ.get("ProgramFiles"),
        environ.get("LOCALAPPDATA"),
    ]
    for base in bases:
        if not base:
            continue
        app_dir = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        try:
            if not app_dir.is_dir():
                continue
            for child in sorted(app_dir.iterdir(), reverse=True):
                if child.is_dir() and (child / "msedgewebview2.exe").is_file():
                    return child.name
        except OSError:
            continue
    return None


def check_webview2(
    *,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    registry_version: Optional[Callable[[], Optional[str]]] = None,
) -> dict:
    plat = platform or sys.platform
    if not plat.startswith("win"):
        return _result(
            "webview2",
            "WebView2 runtime",
            "unverified",
            "Cannot verify on this OS — the WebView2 runtime only "
            f"exists on Windows (this machine reports platform '{plat}'). "
            "On the Windows machine this check performs a real registry "
            "and on-disk lookup.",
            {"platform": plat},
        )
    env = os.environ if environ is None else environ
    version = None
    source = None
    reader = registry_version or _webview2_version_from_registry
    try:
        version = reader()
    except Exception:
        version = None
    if version:
        source = "registry"
    else:
        version = _webview2_version_from_disk(env)
        if version:
            source = "filesystem"
    if version:
        return _result(
            "webview2",
            "WebView2 runtime",
            "pass",
            f"WebView2 runtime found (version {version}, via {source}).",
            {"platform": plat, "version": version, "source": source},
        )
    return _result(
        "webview2",
        "WebView2 runtime",
        "fail",
        "WebView2 runtime was NOT found in the registry or under "
        "Microsoft\\EdgeWebView\\Application. The app window cannot "
        "open without it — install the Microsoft Edge WebView2 Runtime.",
        {"platform": plat, "version": None},
    )


# ---------------------------------------------------------------------------
# FFmpeg + loudnorm probe
# ---------------------------------------------------------------------------
def check_ffmpeg(
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    runner: Optional[Callable[..., Any]] = None,
    ffmpeg_bin: Optional[str] = None,
    timeout_s: float = 30.0,
) -> dict:
    # Binary resolution is owned by app.ffmpeg_resolution (env override
    # -> bundled runtime/ -> PATH). The ``which`` seam is forwarded to
    # the resolver and is looked up on ``shutil`` AT CALL TIME when not
    # injected, so monkeypatching ``shutil.which`` (PATH stripped of
    # ffmpeg) still yields the honest failure. An explicit
    # ``ffmpeg_bin`` keeps its legacy meaning: a name/path resolved
    # through ``which``.
    if which is not None:
        which_fn = which
    else:
        def which_fn(name: str) -> Optional[str]:
            return shutil.which(name)

    run_fn = runner or subprocess.run
    # Anchor the bundled-binary search at the interpreter's own
    # directory WITHOUT resolving symlinks: a dev venv's python is a
    # symlink to the system interpreter, and resolving it would make
    # the system ffmpeg look "bundled" beside /usr/bin/python — the
    # resolver's contract is that under a venv nothing is bundled and
    # resolution falls through to PATH (which is exactly what the
    # PATH-stripped failure case measures).
    anchor = Path(sys.executable).parent
    if ffmpeg_bin is not None:
        path = which_fn(ffmpeg_bin)
    else:
        resolved = resolve_ffmpeg(which=which_fn, anchor=anchor)
        path = str(resolved) if resolved is not None else None
    if not path:
        return _result(
            "ffmpeg",
            "FFmpeg + loudnorm",
            "fail",
            "ffmpeg was not found on PATH. Video rendering and the "
            "loudnorm mastering stage cannot run without it.",
            {"path": None},
        )
    # Version (real invocation).
    version_line = ""
    try:
        proc = run_fn(
            [path, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        first = (proc.stdout or "").splitlines()
        version_line = first[0].strip() if first else ""
        if proc.returncode != 0:
            return _result(
                "ffmpeg", "FFmpeg + loudnorm", "fail",
                f"ffmpeg exists at {path} but `ffmpeg -version` failed "
                f"(exit {proc.returncode}).",
                {"path": path},
            )
    except Exception as exc:
        return _result(
            "ffmpeg", "FFmpeg + loudnorm", "fail",
            f"ffmpeg exists at {path} but could not be executed: {exc}",
            {"path": path},
        )
    # Capability probe (app.ffmpeg_resolution.probe_loudnorm): confirms
    # the resolved build actually ships the loudnorm filter. It gates
    # only when it parsed a real filter list (filters_seen > 0) — an
    # injected test runner that does not model ``-filters`` leaves the
    # measured sine probe below as the authority.
    capability = probe_loudnorm(path, runner=run_fn, timeout=timeout_s)
    if capability.filters_seen > 0 and not capability.loudnorm_present:
        return _result(
            "ffmpeg", "FFmpeg + loudnorm", "fail",
            "ffmpeg was found but its build does not ship the loudnorm "
            "filter — the render pipeline cannot loudness-normalise "
            "with this build.",
            {"path": path, "version": version_line},
        )
    # Loudnorm measurement probe: generated 1s sine -> loudnorm JSON
    # must parse and carry a finite integrated loudness (the production
    # parse path). probe_loudnorm reports filter presence only, not
    # the measured LUFS this check has always reported, so the
    # measurement itself stays here — against the resolved binary.
    try:
        proc = run_fn(
            [
                path, "-hide_banner", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-af", LOUDNORM_FILTER,
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            return _result(
                "ffmpeg", "FFmpeg + loudnorm", "fail",
                "ffmpeg ran but the loudnorm probe failed "
                f"(exit {proc.returncode})"
                + (f": {tail[-1]}" if tail else "."),
                {"path": path, "version": version_line},
            )
        from app.pipeline.audio._ffmpeg import loudnorm_value, parse_loudnorm_json

        raw = parse_loudnorm_json(proc.stderr or "")
        input_i = loudnorm_value(raw, "input_i")
        input_tp = loudnorm_value(raw, "input_tp")
    except Exception as exc:
        return _result(
            "ffmpeg", "FFmpeg + loudnorm", "fail",
            "ffmpeg ran but its loudnorm filter did not return a usable "
            f"measurement for the probe tone: {exc}",
            {"path": path, "version": version_line},
        )
    resolved_ffprobe = resolve_ffprobe(which=which_fn, anchor=anchor)
    ffprobe_path = str(resolved_ffprobe) if resolved_ffprobe is not None else None
    return _result(
        "ffmpeg",
        "FFmpeg + loudnorm",
        "pass",
        f"ffmpeg found and the loudnorm filter answered the probe "
        f"(probe tone measured {input_i:.2f} LUFS integrated, "
        f"{input_tp:.2f} dBTP true peak; production target is −14 LUFS).",
        {
            "path": path,
            "version": version_line,
            "loudnorm_input_i_lufs": input_i,
            "loudnorm_input_tp_dbtp": input_tp,
            "loudnorm_target_lufs": -14.0,
            "loudnorm_filter_present": True,
            "ffprobe_path": ffprobe_path,
        },
    )


# ---------------------------------------------------------------------------
# Disk space
# ---------------------------------------------------------------------------
def check_disk(
    *,
    data_dir: str | Path,
    disk_usage: Optional[Callable[[str], Any]] = None,
) -> dict:
    usage_fn = disk_usage or shutil.disk_usage
    target = Path(data_dir)
    probe_path = target if target.exists() else target.parent
    try:
        usage = usage_fn(str(probe_path))
        free = int(usage.free)
        total = int(usage.total)
    except Exception as exc:
        return _result(
            "disk",
            "Disk space",
            "fail",
            f"Could not measure free space on the data volume ({probe_path}): {exc}",
            {"path": str(probe_path)},
        )
    free_gb = round(free / GIB, 2)
    measured = {
        "path": str(probe_path),
        "free_bytes": free,
        "total_bytes": total,
        "free_gb": free_gb,
        "warn_below_bytes": DISK_WARN_FREE_BYTES,
        "fail_below_bytes": DISK_FAIL_FREE_BYTES,
    }
    if free < DISK_FAIL_FREE_BYTES:
        return _result(
            "disk", "Disk space", "fail",
            f"Only {free_gb} GB free on the data volume — below the "
            "1 GB minimum. Free up space before producing videos.",
            measured,
        )
    if free < DISK_WARN_FREE_BYTES:
        return _result(
            "disk", "Disk space", "warn",
            f"{free_gb} GB free on the data volume — below the "
            "recommended 10 GB. Stock footage and renders are large; "
            "production may run out of space.",
            measured,
        )
    return _result(
        "disk", "Disk space", "pass",
        f"{free_gb} GB free on the data volume.",
        measured,
    )


# ---------------------------------------------------------------------------
# Sidecar health (real endpoint calls through the ASGI app)
# ---------------------------------------------------------------------------
def _threaded_health_probe(app: Any, headers: Optional[Mapping[str, str]]):
    """Call /health/live (+ /health/ready with token) through the app.

    Uses a real HTTP-level client (Starlette TestClient) so the request
    passes through routing + middleware exactly like the shell's own
    health polling. It runs in a worker thread (see check_sidecar):
    invoking the ASGI app re-entrantly from inside a request handler's
    event loop can deadlock against the server's portal.
    """
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        live_resp = client.get("/health/live")
        live_ok = False
        if live_resp.status_code == 200:
            try:
                live_ok = live_resp.json().get("status") == "ok"
            except Exception:
                live_ok = False
        ready: Optional[dict] = None
        token = (headers or {}).get("x-local-token") or (headers or {}).get(
            "X-Local-Token"
        )
        if token:
            ready_resp = client.get(
                "/health/ready", headers={"X-Local-Token": token}
            )
            try:
                ready = {
                    "http_status": ready_resp.status_code,
                    "body": ready_resp.json(),
                }
            except Exception:
                ready = {"http_status": ready_resp.status_code, "body": None}
        return live_ok, ready


async def _asgi_health_probe(app: Any, headers: Optional[Mapping[str, str]]):
    import asyncio

    return await asyncio.to_thread(_threaded_health_probe, app, headers)


async def check_sidecar(
    *,
    app: Any = None,
    headers: Optional[Mapping[str, str]] = None,
    probe: Optional[Callable[..., Any]] = None,
) -> dict:
    if probe is None and app is None:
        return _result(
            "sidecar", "Sidecar health", "fail",
            "No backend app handle was available to probe — the "
            "sidecar health endpoint could not be called.",
            {},
        )
    try:
        if probe is not None:
            outcome = probe(app, headers)
            if hasattr(outcome, "__await__"):
                outcome = await outcome
            live_ok, ready = outcome
        else:
            live_ok, ready = await _asgi_health_probe(app, headers)
    except Exception as exc:
        return _result(
            "sidecar", "Sidecar health", "fail",
            f"The sidecar health endpoint did not respond: {exc}",
            {},
        )
    if not live_ok:
        return _result(
            "sidecar", "Sidecar health", "fail",
            "GET /health/live did not return {\"status\": \"ok\"} — the "
            "sidecar is not healthy.",
            {"live_ok": False, "ready": ready},
        )
    measured: dict[str, Any] = {"live_ok": True, "ready": ready}
    if ready is not None:
        body = ready.get("body") or {}
        if ready.get("http_status") != 200 or body.get("db") != "ok":
            return _result(
                "sidecar", "Sidecar health", "fail",
                "The sidecar answered /health/live but /health/ready "
                "reports the database is not ready.",
                measured,
            )
        return _result(
            "sidecar", "Sidecar health", "pass",
            "Sidecar is live: /health/live answered ok and "
            "/health/ready reports the database is ok.",
            measured,
        )
    return _result(
        "sidecar", "Sidecar health", "pass",
        "Sidecar is live: /health/live answered {\"status\": \"ok\"} "
        "(readiness probe skipped — no launch token to forward).",
        measured,
    )


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------
async def run_all_checks(
    *,
    data_dir: str | Path,
    app: Any = None,
    headers: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    checks = [
        check_webview2(platform=platform, environ=environ),
        check_ffmpeg(),
        check_disk(data_dir=data_dir),
        await check_sidecar(app=app, headers=headers),
    ]
    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    elif "unverified" in statuses:
        overall = "pass_with_unverified"
    else:
        overall = "pass"
    return {
        "checks": checks,
        "overall": overall,
        "platform": platform or sys.platform,
    }
