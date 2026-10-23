"""Style Lab CLOSURE — independent adversarial tests (test-only).

Probes the two closure fixes from ``docs/STYLELAB_BUGS.md`` strictly
through the public API / service surface (black-box where possible):

SL-001 — profile creation on the API path
    (``POST /channels/{id}/style-profiles`` ->
    ``app.style_lab.service.create_profile``) must enforce BOTH the
    strict ``style_profile.v1`` schema
    (``app.integration.style_profile.StyleProfile``, ``extra="forbid"``)
    AND the no-copy gate (``assert_no_copy``: verbatim title /
    description text and every >= 4-word shingle of them).  The
    synthesis path already does both; the bug is that the API path
    persisted arbitrary JSON with neither check.

    1. shingle: a payload embedding a >= 4-word run copied from a
       competitor's STORED description (a partial chunk, not the full
       description) is rejected 422 and nothing persists.
    2. verbatim: a payload whose free-text fields contain a
       competitor's exact TITLE is rejected and nothing persists.
    3. schema: a payload with correct top-level keys but a nested
       field of the wrong type is rejected; an unknown extra key is
       rejected (``extra="forbid"``).  Nothing persists either way.
    4. accept path: a patterns-only payload modelled on
       ``outputs/stylelab/proof/style_profile.json`` IS accepted
       (guards against an over-blocking fix).
    5. activation: activating a legitimately created clean profile
       works, and activation does not change the persisted profile
       content (read-only router inspection, recorded in the run
       report, says activation only flips status via the store).

SL-002 — queued-analysis claimer
    Queueing (``POST .../competitor-videos/{video_id}/analysis``)
    flips a row to ``queued``; per ``docs/STYLE_LAB.md`` section 7
    something must CLAIM queued rows, run the engine, and land the
    row on ``analysed`` / ``analysis_failed``.  At bug-log time no
    claimer existed at all.  These tests therefore DISCOVER the
    claimer entry point (dedicated module under ``app.style_lab`` /
    ``app.style_analysis`` / ``app.execution``, a service-level function,
    or the ExecutionService scheduler tick — the way the execution
    service would drive it) and invoke it; if nothing discoverable
    exists, the tests fail with that as the recorded evidence.

    6. a queued video whose file is missing lands on
       ``analysis_failed`` WITH a recorded reason, and a second
       claimer pass does not retry it (status + ``updated_at``
       unchanged — one attempt only).
    7. STOP ALL: with the app's kill switch engaged
       (``app.execution.flags.set_flag(..., "stop_all", True)`` —
       the persisted settings-table flag the JobRunner re-reads
       every poll), a queued row must remain ``queued``.

These tests were written against the SPEC while the fixes were
being implemented in parallel; a failure whose message says the
boundary / claimer is absent is evidence the fix had not landed,
not a defect in the test.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"slx-a-{_SUFFIX}"
YT_A = f"UCslX{_SUFFIX}xxxx"

# Competitor texts stored through the API.  The profile payloads
# below copy from THESE strings, so the no-copy gate has something
# real to catch — and the clean payload shares no >= 4-word run
# with either of them.
COMPETITOR_TITLE = "Secrets of the Deep Blue Ocean Revealed"
COMPETITOR_DESCRIPTION = (
    "Join us as we explore the deepest trenches and discover glowing "
    "creatures that light up the darkest corners of the ocean floor today"
)
# A >= 4-word run from the description's middle (partial chunk, NOT
# the whole description): "discover glowing creatures that light"
# are 5 consecutive description words.
DESCRIPTION_SHINGLE = "discover glowing creatures that light"


# ---------------------------------------------------------------------------
# Seeding / helpers
# ---------------------------------------------------------------------------


def _seed_channels() -> None:
    from app import db as db_module

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
            " title, language, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
            (CHAN_A, YT_A, "Closure Owner Channel", now, now),
        )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _channels(app):
    # Depends on the conftest `app` fixture so the DB schema exists.
    _seed_channels()
    yield


def _unique_handle(prefix: str) -> str:
    return f"@{prefix}{uuid.uuid4().hex[:8]}"


def _must_competitor(client: TestClient, handle: str) -> dict:
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors", json={"input": handle}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _must_video_with_texts(
    client: TestClient, competitor_id: str, *, title: str, description: str
) -> dict:
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitors/{competitor_id}/videos",
        json={
            "source": "pasted_metadata",
            "title": title,
            "description": description,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _proof_profile(channel_id: str) -> dict:
    """A patterns-only profile modelled on the proof artifact.

    Loads ``outputs/stylelab/proof/style_profile.json`` when present
    (the clean shape the generation proof shipped), overriding only
    ``channel_id`` to the test's owner channel; falls back to an
    embedded copy of the same shape when the artifact is absent.
    """
    proof_path = (
        Path(__file__).resolve().parents[2]
        / "outputs" / "stylelab" / "proof" / "style_profile.json"
    )
    payload: dict | None = None
    try:
        payload = json.loads(proof_path.read_text(encoding="utf-8"))
    except Exception:
        payload = None
    if not isinstance(payload, dict) or payload.get("schema_version") != "style_profile.v1":
        payload = {
            "schema_version": "style_profile.v1",
            "channel_id": channel_id,
            "video_count": 1,
            "duration": {"median_s": 185.73, "p25_s": 185.73,
                         "p75_s": 185.73, "target_s": 185.73},
            "speech": {"wpm_median": 118.23, "language": "en"},
            "hook": {"window_s": 37.15, "types": ["cold_open"]},
            "chapters": {"target_count": 5, "avg_chapter_s": 37.15},
            "visuals": {"stock_ratio": 1.0, "ai_ratio": 0.0,
                        "avg_shot_len_s": 8.0, "cuts_per_min": 4.85},
            "captions": {"present_rate": 1.0, "burned_in_rate": 0.0,
                         "burned_in": False},
            "music": {"present_rate": 1.0, "expected": True},
            "thumbnail": {"layout_class": None, "text_words_min": None,
                          "text_words_max": None, "face_rate": 0.0,
                          "face_expected": False,
                          "palette_class": "dark_neutral"},
            "title_formula": {"len_min": 60, "len_max": 60,
                              "number_rate": 0.0, "bracket_rate": 1.0,
                              "caps_rate": 0.0, "keyword_position": "start"},
            "description_structure": {"sections": ["intro", "cta"],
                                      "hashtag_min": 0, "hashtag_max": 0,
                                      "link_expected": False},
            "provenance": {"source_asset_ids": ["own-asset-1"],
                           "source_sha256": ["0" * 64]},
            "no_copy_manifest": {"never_reuse_asset_ids": ["own-asset-1"],
                                 "never_reuse_sha256": ["0" * 64],
                                 "contains_competitor_assets": False},
        }
    payload = copy.deepcopy(payload)
    payload["channel_id"] = channel_id
    return payload


def _list_profiles(client: TestClient) -> list[dict]:
    resp = client.get(f"/api/v1/channels/{CHAN_A}/style-profiles")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _create_profile(client: TestClient, name: str, competitor_ids, profile):
    return client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={
            "name": name,
            "competitor_ids": list(competitor_ids),
            "profile": profile,
        },
    )


def _assert_nothing_persisted(client: TestClient, before_ids: set, name: str):
    after = _list_profiles(client)
    after_ids = {p["id"] for p in after}
    assert after_ids == before_ids, (
        f"rejected profile creation still persisted a row: "
        f"before={sorted(before_ids)} after={sorted(after_ids)}"
    )
    assert all(p["name"] != name for p in after), (
        f"a profile named {name!r} persisted despite rejection"
    )


# ---------------------------------------------------------------------------
# SL-001.1 — >= 4-word shingle from a stored description is rejected
# ---------------------------------------------------------------------------


def test_sl001_shingle_from_description_rejected_422_nothing_persists(
    client: TestClient,
):
    comp = _must_competitor(client, _unique_handle("ShingleRival"))
    _must_video_with_texts(
        client, comp["id"],
        title=COMPETITOR_TITLE, description=COMPETITOR_DESCRIPTION,
    )
    before_ids = {p["id"] for p in _list_profiles(client)}

    profile = _proof_profile(CHAN_A)
    # Smuggle the description shingle into a schema-valid free-text
    # field (hook pattern labels are arbitrary strings in the
    # schema), padded with unrelated words — a partial, re-framed
    # chunk, NOT a verbatim copy of the whole description.  Only a
    # >= 4-word-shingle gate (assert_no_copy) can catch this; a
    # whole-string comparison cannot.
    profile["hook"]["types"] = [
        "cold_open",
        f"inspired {DESCRIPTION_SHINGLE} style",
    ]
    name = f"adv-shingle-{_SUFFIX}"
    resp = _create_profile(client, name, [comp["id"]], profile)

    assert resp.status_code == 422, (
        f"SL-001: profile embedding a >=4-word shingle "
        f"({DESCRIPTION_SHINGLE!r}) from the competitor's stored "
        f"description was NOT rejected with 422 — got "
        f"{resp.status_code}: {resp.text[:400]}. "
        f"(If 201, the no-copy gate is still bypassed on the API path.)"
    )
    _assert_nothing_persisted(client, before_ids, name)


# ---------------------------------------------------------------------------
# SL-001.2 — verbatim competitor TITLE in a free-text field is rejected
# ---------------------------------------------------------------------------


def test_sl001_verbatim_title_in_free_text_rejected_nothing_persists(
    client: TestClient,
):
    comp = _must_competitor(client, _unique_handle("TitleRival"))
    _must_video_with_texts(
        client, comp["id"],
        title=COMPETITOR_TITLE, description=COMPETITOR_DESCRIPTION,
    )
    before_ids = {p["id"] for p in _list_profiles(client)}

    profile = _proof_profile(CHAN_A)
    # The competitor's exact title, verbatim, in a schema-valid
    # free-text field of the profile payload.
    profile["thumbnail"]["layout_class"] = COMPETITOR_TITLE
    name = f"adv-title-{_SUFFIX}"
    resp = _create_profile(client, name, [comp["id"]], profile)

    assert resp.status_code in (400, 422), (
        f"SL-001: profile carrying the competitor's verbatim title "
        f"{COMPETITOR_TITLE!r} was NOT rejected — got "
        f"{resp.status_code}: {resp.text[:400]}. "
        f"(If 201, the no-copy gate is still bypassed on the API path.)"
    )
    _assert_nothing_persisted(client, before_ids, name)


# ---------------------------------------------------------------------------
# SL-001.3 — subtle schema violations are rejected
# ---------------------------------------------------------------------------


def test_sl001_nested_wrong_type_rejected_nothing_persists(
    client: TestClient,
):
    comp = _must_competitor(client, _unique_handle("SchemaRival"))
    before_ids = {p["id"] for p in _list_profiles(client)}

    profile = _proof_profile(CHAN_A)
    # Correct top-level keys, but a NESTED field of the wrong type:
    # duration.median_s must be a float, not a string.
    profile["duration"]["median_s"] = "one hundred eighty five"
    name = f"adv-schema-type-{_SUFFIX}"
    resp = _create_profile(client, name, [comp["id"]], profile)

    assert resp.status_code in (400, 422), (
        f"SL-001: profile with duration.median_s as a string was NOT "
        f"rejected — got {resp.status_code}: {resp.text[:400]}. "
        f"(If 201, style_profile.v1 validation is still bypassed on "
        f"the API path.)"
    )
    _assert_nothing_persisted(client, before_ids, name)


def test_sl001_unknown_extra_key_rejected_nothing_persists(
    client: TestClient,
):
    comp = _must_competitor(client, _unique_handle("ExtraRival"))
    before_ids = {p["id"] for p in _list_profiles(client)}

    profile = _proof_profile(CHAN_A)
    # StyleProfile is strict (extra="forbid"): an unknown key must
    # fail validation even though every known field is well-formed.
    profile["unexpected_extra_key"] = "surprise"
    name = f"adv-schema-extra-{_SUFFIX}"
    resp = _create_profile(client, name, [comp["id"]], profile)

    assert resp.status_code in (400, 422), (
        f"SL-001: profile with unknown extra key "
        f"'unexpected_extra_key' was NOT rejected — got "
        f"{resp.status_code}: {resp.text[:400]}. "
        f"(If 201, the strict schema is still bypassed on the API path.)"
    )
    _assert_nothing_persisted(client, before_ids, name)


# ---------------------------------------------------------------------------
# SL-001.4 — patterns-only proof-shaped payload IS accepted
# ---------------------------------------------------------------------------


def test_sl001_patterns_only_proof_profile_accepted(client: TestClient):
    comp = _must_competitor(client, _unique_handle("CleanRival"))
    # Even WITH competitor texts stored on the channel, a clean
    # patterns-only profile shares no >= 4-word run with them and
    # must not be over-blocked.
    _must_video_with_texts(
        client, comp["id"],
        title=COMPETITOR_TITLE, description=COMPETITOR_DESCRIPTION,
    )
    profile = _proof_profile(CHAN_A)
    name = f"adv-clean-{_SUFFIX}"
    resp = _create_profile(client, name, [comp["id"]], profile)

    assert resp.status_code == 201, (
        f"clean patterns-only profile (proof shape) was rejected — "
        f"got {resp.status_code}: {resp.text[:400]}"
    )
    body = resp.json()
    assert body["name"] == name
    assert body["channel_id"] == CHAN_A
    stored = {p["id"]: p for p in _list_profiles(client)}
    assert body["id"] in stored
    assert stored[body["id"]]["profile"] is not None, (
        "accepted profile persisted without its profile payload"
    )


# ---------------------------------------------------------------------------
# SL-001.5 — activation of a clean profile works; content untouched
# ---------------------------------------------------------------------------


def test_sl001_activation_clean_profile_works_content_unchanged(
    client: TestClient,
):
    comp = _must_competitor(client, _unique_handle("ActivateRival"))
    profile = _proof_profile(CHAN_A)
    name = f"adv-activate-{_SUFFIX}"
    created = _create_profile(client, name, [comp["id"]], profile)
    assert created.status_code == 201, created.text
    pid = created.json()["id"]

    def _snapshot():
        items = {p["id"]: p for p in _list_profiles(client)}
        assert pid in items
        return items[pid]

    before = _snapshot()
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles/{pid}/activate"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    after = _snapshot()
    assert after["status"] == "active"
    # Activation must not re-persist profile content: the persisted
    # payload must be byte-identical before and after.  (Read-only
    # inspection of the landed fix — app/routers/style_lab.py +
    # app/style_lab/service.py::activate_profile +
    # store.activate_profile — confirms: activation only flips
    # style_profiles.status via the store and never re-writes
    # profile_json; as defence-in-depth it RE-CHECKS any stored
    # content (schema + assert_no_copy against the profile's own
    # competitors) before activating and refuses a violating legacy
    # row with 422, leaving it non-active.)
    assert after.get("profile_json") == before.get("profile_json"), (
        "activation changed profile_json — activation re-persists "
        "profile content and must re-run the no-copy/schema gate"
    )
    assert after.get("profile") == before.get("profile")


# ---------------------------------------------------------------------------
# SL-002 — claimer discovery
# ---------------------------------------------------------------------------


def _conn_factory():
    from app import db as db_module

    return db_module.get_connection


def _data_dir() -> Path:
    from app.config import get_settings

    return Path(get_settings().data_dir)


def _flexible_args(fn, *, factory, store, data_dir: Path) -> list[tuple]:
    """Plausible (args, kwargs) attempts for an unknown entry point,
    most specific first, derived from its signature when possible."""
    attempts: list[tuple] = []
    try:
        params = [
            p for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.name != "self"
        ]
    except (TypeError, ValueError):
        params = []
    if params:
        built_args: list = []
        built_kwargs: dict = {}
        ok = True
        for p in params:
            lname = p.name.lower()
            if "conn" in lname or lname in ("factory", "db", "db_path"):
                value = factory
            elif "store" in lname:
                value = store
            elif "data_dir" in lname or "base_dir" in lname or "out_dir" in lname:
                value = data_dir
            elif "settings" in lname:
                from app.config import get_settings

                value = get_settings()
            elif p.default is not inspect.Parameter.empty:
                continue
            else:
                ok = False
                break
            if p.kind == p.POSITIONAL_OR_KEYWORD:
                built_kwargs[p.name] = value
            else:
                built_args.append(value)
        if ok:
            attempts.append((tuple(built_args), built_kwargs))
    attempts += [
        ((factory,), {}),
        ((store,), {}),
        ((factory, data_dir), {}),
        ((), {}),
    ]
    # De-duplicate while preserving order.
    seen: set = set()
    unique: list[tuple] = []
    for item in attempts:
        key = repr(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _call_flexible(fn, *, factory, store, data_dir: Path):
    """Call ``fn`` trying plausible signatures; await coroutines.

    Only TypeError (signature mismatch) advances to the next attempt;
    the last error is re-raised when every attempt mismatches."""
    last_exc: Exception | None = None
    for args, kwargs in _flexible_args(
        fn, factory=factory, store=store, data_dir=data_dir
    ):
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            return result
        except TypeError as exc:
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def _construct_flexible(cls, *, factory, store, data_dir: Path):
    return _call_flexible(
        cls, factory=factory, store=store, data_dir=data_dir
    )


_CLAIMER_METHOD_NAMES = (
    "poll_once", "run_once", "tick", "claim_once", "process_once",
    "claim_queued", "claim_queued_analyses", "process_queued",
    "run", "poll", "claim_next", "__call__",
)


def _claimer_callable_from_object(obj, *, factory, store, data_dir: Path):
    """Return a zero-arg driver for a claimer class/module member."""
    if inspect.isclass(obj):
        instance = _construct_flexible(
            obj, factory=factory, store=store, data_dir=data_dir
        )
        for method_name in _CLAIMER_METHOD_NAMES:
            method = getattr(instance, method_name, None)
            if callable(method):
                return lambda m=method: _call_flexible(
                    m, factory=factory, store=store, data_dir=data_dir
                )
        return None
    if callable(obj):
        return lambda f=obj: _call_flexible(
            f, factory=factory, store=store, data_dir=data_dir
        )
    return None


def _is_claimer_name(name: str) -> bool:
    low = name.lower()
    if "test" in low:
        return False
    if "claim" in low and any(
        tok in low for tok in ("analysis", "analyse", "style", "queued", "competitor")
    ):
        return True
    if ("poller" in low or "claimer" in low) and any(
        tok in low for tok in ("analysis", "analyse", "style")
    ):
        return True
    return low in {
        "poll_style_lab_once", "run_style_lab_once",
        "process_style_lab_queue", "drive_analysis_queue",
    }


def _find_dedicated_claimer():
    """Discover a dedicated Style Lab claimer entry point.

    Returns ``(driver_callable, description)`` or ``(None, reason)``.
    Searches explicit candidate modules first, then every module in
    the ``app.style_lab`` / ``app.style_analysis`` / ``app.execution``
    packages for claim/poll-shaped members."""
    import importlib
    import pkgutil

    factory = _conn_factory()
    from app.style_lab.store import StyleLabStore

    store = StyleLabStore(factory)
    data_dir = _data_dir()

    modules: list = []
    for modname in (
        "app.style_lab.claimer", "app.style_analysis.claimer",
        "app.style_lab.analysis_claimer", "app.style_analysis.analysis_claimer",
        "app.style_lab.poller", "app.style_analysis.poller",
        "app.style_lab.analysis", "app.style_lab.service",
        "app.execution.style_lab_claimer",
    ):
        try:
            modules.append((modname, importlib.import_module(modname)))
        except Exception:
            continue
    for package_name in ("app.style_lab", "app.style_analysis", "app.execution"):
        try:
            package = importlib.import_module(package_name)
        except Exception:
            continue
        for info in pkgutil.iter_modules(package.__path__):
            modname = f"{package_name}.{info.name}"
            if any(m == modname for m, _ in modules):
                continue
            try:
                modules.append((modname, importlib.import_module(modname)))
            except Exception:
                continue

    for modname, module in modules:
        for name in dir(module):
            if not _is_claimer_name(name):
                continue
            obj = getattr(module, name, None)
            if obj is None:
                continue
            # Only accept members actually defined in that module
            # (avoid re-exported execution JobQueue.claim lookalikes).
            if getattr(obj, "__module__", modname) not in (modname, None) \
                    and not inspect.isclass(obj):
                continue
            try:
                driver = _claimer_callable_from_object(
                    obj, factory=factory, store=store, data_dir=data_dir
                )
            except Exception:
                continue
            if driver is not None:
                return driver, f"{modname}.{name}"
    return None, "no dedicated claimer module/function/class found"


def _drive_via_execution_service() -> str:
    """Drive one ExecutionService scheduler tick (the integration
    point STYLE_LAB.md section 7 anticipates for the claimer), plus
    any style/analysis-shaped extra methods the service exposes."""
    factory = _conn_factory()
    from app.execution.service import ExecutionService

    service = ExecutionService(factory)
    driven: list[str] = []
    for name in dir(service):
        low = name.lower()
        if ("style" in low or "analysis" in low or "competitor" in low) \
                and any(tok in low for tok in ("tick", "claim", "poll", "once", "run")):
            method = getattr(service, name, None)
            if callable(method):
                try:
                    result = method()
                    if inspect.isawaitable(result):
                        asyncio.run(result)
                    driven.append(name)
                except Exception:
                    pass
    tick = getattr(service, "_tick_once", None)
    if callable(tick):
        tick()
        driven.append("_tick_once")
    return ", ".join(driven) or "nothing"


_CLAIMER_STATE: dict = {"dedicated": "unknown", "desc": None}


def _drive_claimer_once() -> str:
    """Drive ONE claimer pass the way the execution service would.

    Prefers a dedicated claimer entry point; falls back to the
    ExecutionService scheduler tick.  Returns a description of what
    was driven.  Raises AssertionError when nothing discoverable
    exists — that absence IS the SL-002 evidence."""
    if _CLAIMER_STATE["dedicated"] == "unknown":
        driver, desc = _find_dedicated_claimer()
        _CLAIMER_STATE["dedicated"] = driver
        _CLAIMER_STATE["desc"] = desc
    driver = _CLAIMER_STATE["dedicated"]
    if driver is not None:
        driver()
        return f"dedicated claimer {_CLAIMER_STATE['desc']}"
    # Fallback: the execution service's own tick.
    try:
        driven = _drive_via_execution_service()
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"SL-002: no claimer entry point discoverable "
            f"({_CLAIMER_STATE['desc']}) and the ExecutionService "
            f"fallback drive failed: {exc!r}"
        ) from exc
    return f"execution-service tick ({driven})"


def _raw_video_row(video_id: str) -> dict | None:
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute(
            "SELECT * FROM competitor_videos WHERE id = ?", (video_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _queue_analysis(client: TestClient, video_id: str) -> dict:
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/competitor-videos/{video_id}/analysis"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analysis_status"] == "queued", body
    return body


def _set_stop_all(value: bool) -> None:
    from app.execution.flags import set_flag

    set_flag(_conn_factory(), "stop_all", value)


def _failure_reason_evidence(video_id: str) -> str | None:
    """A recorded failure reason for ``video_id``, wherever the
    claimer chose to record it: a reason/error-ish column on the
    competitor_videos row (the current DDL has none, so a fix that
    records the reason in the DB must add or repurpose one), or a
    failure mention in the file-based analysis artifacts."""
    row = _raw_video_row(video_id)
    if row is not None:
        for key, value in row.items():
            if key == "analysis_status":
                continue
            if any(tok in key.lower()
                   for tok in ("reason", "error", "fail", "detail", "note")) \
                    and value not in (None, ""):
                return f"row column {key}={value!r}"
    data_dir = _data_dir()
    slug = re.sub(r"[^a-z0-9._-]+", "-", video_id.lower()).strip("-.") or video_id
    for base in (data_dir / "stylelab", data_dir / "style_lab"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if video_id not in str(path) and slug not in str(path) \
                    and path.name != "index.json":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if video_id in text or slug in text:
                lowered = text.lower()
                if any(tok in lowered for tok in
                       ("analysis_failed", "fail", "error", "missing",
                        "not found", "corrupt", "no such file")):
                    return f"artifact {path} records a failure"
    return None


@pytest.fixture()
def _stop_all_cleanup():
    _set_stop_all(False)
    yield
    _set_stop_all(False)


# ---------------------------------------------------------------------------
# SL-002.0 — a claimer entry point exists at all
# ---------------------------------------------------------------------------


def test_sl002_claimer_entry_point_discoverable():
    driver, desc = _find_dedicated_claimer()
    if driver is not None:
        _CLAIMER_STATE["dedicated"] = driver
        _CLAIMER_STATE["desc"] = desc
        return  # discovered — the behavioural tests below drive it
    # No dedicated module: the claimer may live inside the
    # ExecutionService.  That counts only if its module actually
    # references the Style Lab queue (otherwise the tick is just the
    # pre-existing schedule tick and SL-002 is unimplemented).
    import app.execution.service as service_module

    source = Path(service_module.__file__).read_text(
        encoding="utf-8", errors="replace"
    )
    assert ("style_lab" in source or "stylelab" in source) and (
        "queued" in source or "claim" in source.lower()
    ), (
        f"SL-002: no queued-analysis claimer discoverable — {desc}; "
        f"app.execution.service does not reference the Style Lab "
        f"queue either. Queueing still dead-ends at 'queued' "
        f"(docs/STYLE_LAB.md section 7 as-built status)."
    )


# ---------------------------------------------------------------------------
# SL-002.6 — missing/corrupt file -> analysis_failed, reason, no retry
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_stop_all_cleanup")
def test_sl002_missing_file_fails_once_with_reason_no_retry(
    client: TestClient,
):
    comp = _must_competitor(client, _unique_handle("BrokenRival"))
    video = _must_video_with_texts(
        client, comp["id"],
        title="Broken File Video", description="file will be missing",
    )
    video_id = video["id"]
    # Turn the row into an attached-file video pointing at a file
    # that does not exist on disk (the missing/corrupt case).
    from app import db as db_module

    missing_rel = f"style_lab/competitors/{comp['id']}/missing-{_SUFFIX}.mp4"
    conn = db_module.get_connection()
    try:
        conn.execute(
            "UPDATE competitor_videos SET source = 'attached_file',"
            " storage_path = ? WHERE id = ?",
            (missing_rel, video_id),
        )
    finally:
        conn.close()
    assert not (_data_dir() / missing_rel).exists()

    _queue_analysis(client, video_id)

    driven = _drive_claimer_once()
    row = _raw_video_row(video_id)
    assert row is not None
    assert row["analysis_status"] == "analysis_failed", (
        f"SL-002: after driving the claimer ({driven}) a queued "
        f"video with a missing file is "
        f"{row['analysis_status']!r}, expected 'analysis_failed'"
    )
    reason = _failure_reason_evidence(video_id)
    assert reason is not None, (
        "SL-002: video landed on 'analysis_failed' but no failure "
        f"reason is recorded anywhere (row columns: "
        f"{sorted(row.keys())}; no failure artifact found)"
    )

    # Second pass must NOT retry the failed row: one attempt only.
    after_first = _raw_video_row(video_id)
    _drive_claimer_once()
    after_second = _raw_video_row(video_id)
    assert after_second is not None
    assert after_second["analysis_status"] == "analysis_failed", (
        f"SL-002: second claimer pass moved a failed video to "
        f"{after_second['analysis_status']!r} — failed rows must "
        f"not be retried automatically"
    )
    assert after_second["updated_at"] == after_first["updated_at"], (
        "SL-002: second claimer pass touched the failed row "
        f"(updated_at {after_first['updated_at']} -> "
        f"{after_second['updated_at']}) — a retry attempt was made; "
        f"exactly one attempt is allowed"
    )


# ---------------------------------------------------------------------------
# SL-002.7 — STOP ALL leaves a queued row unclaimed
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_stop_all_cleanup")
def test_sl002_stop_all_leaves_queued_row_unclaimed(client: TestClient):
    comp = _must_competitor(client, _unique_handle("HaltedRival"))
    video = _must_video_with_texts(
        client, comp["id"],
        title="Halted Video", description="must stay queued under stop all",
    )
    video_id = video["id"]
    _queue_analysis(client, video_id)

    # Engage the app's STOP ALL kill switch the way the runner's
    # owner does: the persisted settings-table flag
    # (app.execution.flags — JobRunner re-reads it every poll).
    from app.execution.flags import read_flags

    _set_stop_all(True)
    assert read_flags(_conn_factory()).stop_all is True
    try:
        driven = _drive_claimer_once()
    finally:
        _set_stop_all(False)

    row = _raw_video_row(video_id)
    assert row is not None
    assert row["analysis_status"] == "queued", (
        f"SL-002: with STOP ALL engaged the claimer ({driven}) "
        f"moved a queued row to {row['analysis_status']!r} — it "
        f"must not claim anything while stop_all is set (sibling "
        f"loops check app.execution.flags.read_flags every poll)"
    )
