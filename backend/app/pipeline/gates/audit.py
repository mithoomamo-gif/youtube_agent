"""Append-only JSONL audit log for Gate B conditional-policy events.

Every conditional token, approval, release and block decision MUST be
audit-logged with the QC flags and footage provenance (stock asset IDs)
-- that is the point of this module.

Round 4 introduced a conditional-pass policy for Gate B: a QC ``WARN``
carrying only minor flags can mint a *conditional* token, which is
publishable only after a recorded human approval in review-first mode
and still blocks in full-auto mode.  That policy is only defensible if
every step of it leaves a permanent, tamper-evident trail.  This module
is that trail:

* :func:`record_gate_b_event` appends exactly one JSON object per line
  to a JSONL sink.  It never rewrites, truncates or reorders prior
  lines -- the file is opened in append mode only.
* Each recorded event is enriched with a UTC timestamp (``ts``), the
  conditional-policy version (``policy_version``) and a unique
  ``event_id``, so an auditor can reconstruct *what* was decided,
  *under which policy*, *when*, and *on what evidence* (the caller
  supplies ``qc_flags`` / ``minor_flags`` and the footage provenance in
  ``asset_ids``).
* :func:`read_gate_b_events` parses the log back, in order.
* :func:`asset_ids_from_ledger` extracts the stock asset IDs to log as
  provenance from licence-ledger rows -- without ever fabricating an
  ID for a row that does not carry one.

The log is deliberately a plain file, not a database table: it can be
shipped inside a production pack, diffed, hashed and replayed without
any application state.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Union

__all__ = [
    "EVENT_CONDITIONAL_MINTED",
    "EVENT_APPROVAL_RECORDED",
    "EVENT_PUBLISH_RELEASED",
    "EVENT_BLOCKED",
    "REQUIRED_EVENT_KEYS",
    "FALLBACK_POLICY_VERSION",
    "record_gate_b_event",
    "read_gate_b_events",
    "asset_ids_from_ledger",
]

# ---------------------------------------------------------------------------
# Event-type constants (fixed contract -- Worker 2 / callers use these)
# ---------------------------------------------------------------------------

EVENT_CONDITIONAL_MINTED = "conditional_token_minted"
EVENT_APPROVAL_RECORDED = "approval_recorded"
EVENT_PUBLISH_RELEASED = "publish_released"
EVENT_BLOCKED = "blocked"

#: Keys the caller MUST supply for every event.  A conditional decision
#: without a manifest binding, a mode or an outcome is not auditable,
#: so :func:`record_gate_b_event` refuses to log it.
REQUIRED_EVENT_KEYS = ("event_type", "manifest_hash", "mode", "outcome")

#: Policy version used when ``conditional`` cannot be imported (e.g. a
#: checkout / test run where that module is not present yet).
FALLBACK_POLICY_VERSION = "1.0"


def _default_policy_version() -> str:
    """Policy version from ``conditional`` if available, else ``"1.0"``.

    Resolved on every call (not frozen at import time) so a run that
    imports this module before ``conditional`` exists still picks the
    real version up once that module lands.  Any import / attribute
    failure falls back to :data:`FALLBACK_POLICY_VERSION`.
    """
    # Relative import first (the normal in-package path), then the
    # absolute ``app.`` path used when backend/ is on sys.path.
    for loader in (
        lambda: __import__(
            "app.pipeline.gates.conditional",
            fromlist=["MINOR_FLAG_POLICY_VERSION"],
        ),
        lambda: __import__(
            "conditional",
            fromlist=["MINOR_FLAG_POLICY_VERSION"],
        ),
    ):
        try:
            module = loader()
            version = getattr(module, "MINOR_FLAG_POLICY_VERSION", None)
            if version is not None and str(version).strip():
                return str(version)
        except Exception:
            continue
    # Direct relative import as a last resort (works when this module
    # was imported as part of the ``gates`` package).
    try:
        from . import conditional as _conditional  # type: ignore[attr-defined]

        version = getattr(_conditional, "MINOR_FLAG_POLICY_VERSION", None)
        if version is not None and str(version).strip():
            return str(version)
    except Exception:
        pass
    return FALLBACK_POLICY_VERSION


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (``...Z`` suffix)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Recording / reading
# ---------------------------------------------------------------------------


def record_gate_b_event(sink_path: Union[str, Path], event: dict) -> None:
    """Append one Gate B conditional-policy event to the JSONL log.

    ``sink_path`` may be a ``str`` or :class:`~pathlib.Path`; parent
    directories are created if needed.  The file is opened in append
    mode and exactly one JSON object (one line) is written -- prior
    lines are never rewritten.

    The event is enriched (on a copy; the caller's dict is not
    mutated) with:

    * ``ts`` -- UTC ISO-8601 timestamp, if the caller did not supply
      one;
    * ``policy_version`` -- ``conditional.MINOR_FLAG_POLICY_VERSION``
      if the caller did not supply one (falling back to ``"1.0"``
      when that module / constant is unavailable);
    * ``event_id`` -- a ``uuid4`` hex string, if the caller did not
      supply one.

    Required caller keys are ``event_type``, ``manifest_hash``,
    ``mode`` and ``outcome``; a :class:`ValueError` listing every
    missing key is raised (and nothing is written) if any are absent
    or ``None``.  Optional keys -- ``qc_flags``, ``minor_flags``,
    ``asset_ids``, ``actor``, ``notes`` -- and any other keys the
    caller supplies are passed through untouched.
    """
    if not isinstance(event, Mapping):
        raise ValueError(
            "Gate B audit event must be a dict with keys: "
            + ", ".join(REQUIRED_EVENT_KEYS)
        )
    missing = [
        key
        for key in REQUIRED_EVENT_KEYS
        if key not in event or event[key] is None
    ]
    if missing:
        raise ValueError(
            "Gate B audit event missing required keys: " + ", ".join(missing)
        )

    enriched: dict[str, Any] = dict(event)
    if not enriched.get("ts"):
        enriched["ts"] = _utc_now_iso()
    if not enriched.get("policy_version"):
        enriched["policy_version"] = _default_policy_version()
    if not enriched.get("event_id"):
        enriched["event_id"] = uuid.uuid4().hex

    path = Path(sink_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(enriched, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_gate_b_events(sink_path: Union[str, Path]) -> list[dict]:
    """Read the JSONL audit log at ``sink_path`` back, in file order.

    A missing file returns ``[]`` (no events have been logged yet is
    not an error).  Blank lines are skipped.
    """
    path = Path(sink_path)
    if not path.exists():
        return []
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Footage provenance: stock asset IDs from licence-ledger rows
# ---------------------------------------------------------------------------

#: Kinds that are never stock footage in the real ledger -- the
#: whole-video generated tracks.  In the real Round 3 cricket ledger
#: (``outputs/round3/phase0/other_cricket/licence_ledger.csv``) these
#: are the ``narration_voice`` row (``kind=voice``) and the
#: ``music_bed`` row (``kind=music``).
_NON_STOCK_KINDS = frozenset({"voice", "music", "audio", "narration", "voiceover"})

#: ``asset_id`` placeholder values that carry no provenance.  The
#: ``unknown-stock-asset`` fallback is produced by
#: :func:`app.integration.stock_ledger.ledger_row_for_stock_asset`
#: when an asset object had neither a provider id nor a scene id --
#: logging it would fabricate provenance, so it is skipped.
_PLACEHOLDER_ASSET_IDS = frozenset(
    {"", "unknown-stock-asset", "unknown", "none", "null", "n/a", "na"}
)

#: ``provider_asset_id=<id>`` embedded in a ledger row's ``notes``
#: field -- the labelled evidence string built by
#: ``stock_ledger.ledger_row_for_stock_asset``.
_NOTES_PROVIDER_ID_RE = re.compile(r"provider_asset_id=([^\s;]+)")


def _row_value(row: Any, *names: str) -> Any:
    """First non-empty value for ``names`` on a dict or object row."""
    for name in names:
        if isinstance(row, Mapping):
            value = row.get(name)
        else:
            value = getattr(row, name, None)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _is_own_generated(row: Any) -> bool:
    """True for rows the ledger marks as generated in-app.

    Real-ledger evidence: the voice/music rows carry
    ``source="own/generated (edge-tts synthesis)"`` /
    ``source="own/generated (local tone bed)"`` -- see the module
    docstring of :mod:`app.integration.ledger` (``"own/generated"``
    style sources for generated assets).
    """
    source = str(_row_value(row, "source", "provider") or "").strip().lower()
    return "own/generated" in source or source.startswith("own/")


def asset_ids_from_ledger(rows: Iterable[Any]) -> list[str]:
    """Extract stock asset IDs from licence-ledger row dicts.

    Column used -- evidence from the real ledger: the CSV header of
    ``outputs/round3/phase0/other_cricket/licence_ledger.csv`` is
    exactly ``asset_id,scene_id,source,licence,kind,local_path,notes``
    (mirroring :data:`app.integration.ledger.CSV_COLUMNS`), and its
    stock rows carry the provider's own numeric id in the first
    column, e.g. ``189845,scene_0001,Pixabay stock video,...``.
    :mod:`app.integration.stock_ledger` documents the same semantics:
    "``asset_id`` is the provider's own asset id" and its superset
    rows carry that same value under ``provider_asset_id`` as well.

    Extraction, per row, in order (first hit wins, duplicates removed
    preserving first-seen order, IDs returned as stripped strings):

    1. ``provider_asset_id`` -- preferred when present, because on
       superset rows (the ``stock_ledger`` dicts) it is unambiguously
       the provider id, whereas ``asset_id`` falls back to a scene id
       there when no provider id exists.
    2. ``provider_asset_id=<id>`` parsed from ``notes`` -- the labelled
       evidence string superset rows embed in that field.
    3. ``asset_id`` -- the CSV column described above.

    Never fabricate IDs: a row contributes NOTHING when it carries no
    usable ID -- in particular the real ledger's generated whole-video
    rows (``narration_voice`` with ``kind=voice`` and ``music_bed``
    with ``kind=music``, both ``source="own/generated ..."``), rows
    whose kind is voice/music/audio, own/generated rows, rows with a
    missing/empty ``asset_id``, and rows whose only ID is a
    placeholder such as ``unknown-stock-asset``.  No ID is ever
    derived from a URL or a ``local_path`` -- the real ledger carries
    the ID explicitly, so deriving one would be guessing.
    """
    asset_ids: list[str] = []
    seen: set[str] = set()

    def _add(candidate: Any) -> bool:
        if candidate is None:
            return False
        text = str(candidate).strip()
        if text.lower() in _PLACEHOLDER_ASSET_IDS:
            return False
        if text in seen:
            return True  # already recorded -- counts as found
        seen.add(text)
        asset_ids.append(text)
        return True

    for row in rows or []:
        if row is None:
            continue
        # 1. Explicit provider id (superset rows) -- always stock.
        if _add(_row_value(row, "provider_asset_id")):
            continue
        # 2. Provider id embedded in the notes evidence string.
        notes = _row_value(row, "notes")
        if notes:
            match = _NOTES_PROVIDER_ID_RE.search(str(notes))
            if match and _add(match.group(1)):
                continue
        # 3. The CSV ``asset_id`` column -- but only for stock rows.
        kind = str(_row_value(row, "kind") or "").strip().lower()
        if kind in _NON_STOCK_KINDS:
            continue
        if _is_own_generated(row):
            continue
        _add(_row_value(row, "asset_id"))

    return asset_ids
