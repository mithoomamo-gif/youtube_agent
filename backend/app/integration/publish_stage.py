"""ROUND 2 integration — Worker 5: publish stage.

Takes the assembled pack and produces the frozen publish package:

    licence ledger rows (from the assemble stage / bundle)
      -> copyright gates G1+G2+G6   (pipeline.gates.copyright)
      -> policy similarity gate      (pipeline.gates.policy, no history)
      -> technical QC verdict        (from the assemble stage)
      -> Gate B tokens bound to the publish-manifest hash
      -> verify_gate_b               (pipeline.gates.aggregator)
      -> frozen PublishManifest      (pipeline.publishing.manifest)
      -> publish_manifest.json in the pack dir
      -> FAKE-provider workflow run only (NO real upload)

Honesty rules honoured here:

* A gate token carries the gate's OWN verdict — if copyright, policy or
  QC did not PASS, the token says so, ``verify_gate_b`` fails, and
  ``gate_b_passed`` is False.  Nothing is fabricated into a PASS.
* The only upload ever attempted is through the in-module
  :class:`FakeProvider` (the publishing package ships no fake of its
  own; the scripted one in the Round-1 tests is test-local), and the
  resulting state is reported as ``fake_provider_upload_simulated`` —
  never as a real publication.
* Gate A is part of Gate B (orchestration contract: Gate B requires
  Gate A plus the three PASS tokens), so ``gate_a_passed=False``
  also fails Gate B here.

Gate B verdict labelling (Round 6 closure): this function is the
ONE Gate B evaluator for both the package stage and the publish
finaliser (the finaliser re-runs it on the frozen pack).  Its result
carries the shared ``gate_b_verdict`` label + ``gate_b_reason_codes``
(``app.pipeline.gates.aggregator.gate_b_verdict_label`` /
``collect_gate_b_reason_codes``): a conditional release is labelled
``conditional_pass``, never a bare pass, and a provenance block
keeps its typed reason (``G2_PROVENANCE_FAILED``) alongside the
strict problem code.  Asset-provider labels are canonicalised here
(``local_plate`` -> ``generated``) so every caller judges the same
registry identically.

Token/manifest binding: gate verdicts are first issued against the
pre-freeze draft manifest hash (the manifest cannot contain its own
final token ids before it exists — its hash covers the token fields),
the frozen manifest then embeds those token ids, and the RETURNED
tokens are re-issued with identical verdicts / reason codes / evidence
bound to the FROZEN manifest hash, which is what
:func:`verify_gate_b` checks.  Both hashes are recorded in the manifest
JSON (``gate_b.draft_manifest_hash`` / ``gate_b.binding_manifest_hash``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.pipeline.gates.aggregator import (
    collect_gate_b_reason_codes,
    evaluate_gate_b,
    gate_b_verdict_label,
    verify_gate_b,
)
from app.pipeline.gates.copyright import (
    AssetProvenance,
    LicenceRecord,
    UsageSegment,
    run_copyright_gates,
)
from app.pipeline.gates.policy import VideoFingerprint, check_similarity
from app.pipeline.gates.tokens import (
    GATE_QC_TECHNICAL,
    GateToken,
    Verdict,
    issue_token,
)
from app.pipeline.publishing.manifest import PublishManifest
from app.pipeline.publishing.provider import (
    ChunkResult,
    VideoMetadata,
    VideoStatus,
)
from app.pipeline.publishing.store import InMemoryStore
from app.pipeline.publishing.workflow import PublishingWorkflow
from app.pipeline.qc.technical import probe_media

from .assemble_stage import _first_text, _get, _nested

__all__ = ["PublishResult", "FakeProvider", "finalize_publish"]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class PublishResult:
    """Contract result of the publish stage (Worker 5)."""

    gate_b_passed: bool
    manifest: dict
    manifest_path: str
    publish_state: str
    tokens: list[dict]
    # -- extras ---------------------------------------------------------------
    gate_b_problems: list[dict] = field(default_factory=list)
    video_id: Optional[str] = None
    notes: dict = field(default_factory=dict)
    # -- Round 4 conditional-pass extras (additive; defaults = legacy) -------
    gate_b_outcome: Optional[str] = None  # "pass" | "conditional" | "block"
    gate_b_conditional: dict = field(default_factory=dict)
    # -- Round 6 closure (W7): shared Gate B verdict labelling ----------
    #: Canonical verdict label from
    #: ``aggregator.gate_b_verdict_label``: "pass" |
    #: "conditional_pass" | "conditional_blocked" | "block".  The
    #: package report and the publish finaliser both surface THIS
    #: label (never a bare ``gate_b_passed`` bool alone), so a
    #: conditional release can never read as a strict pass.
    gate_b_verdict: str = "block"
    #: Ordered typed reason codes from
    #: ``aggregator.collect_gate_b_reason_codes`` — token reasons
    #: (e.g. ``G2_PROVENANCE_FAILED``, QC minor flags) followed by
    #: problem codes (e.g. ``VERDICT_NOT_PASS``).
    gate_b_reason_codes: list = field(default_factory=list)
    # -- Round 6 Compliance Gate disclosure (additive) ------------------------
    #: Visibility decision applied to this publish: overall state,
    #: requested vs effective visibility, forced_private flag and the
    #: typed reason when public publishing was refused pre-PASS.
    compliance_gate: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fake provider (publishing Protocol stand-in — records, never networks)
# ---------------------------------------------------------------------------


class FakeProvider:
    """In-memory :class:`YouTubeProvider` satisfying the publishing
    Protocol.  Simulates a resumable upload that completes, processing
    that succeeds immediately, and post-release state consistent with the
    manifest, so :class:`PublishingWorkflow` can run end-to-end with
    zero real YouTube calls.  Every call is recorded in ``calls``."""

    def __init__(self, expected_duration_s: float = 0.0) -> None:
        self.expected_duration_s = float(expected_duration_s)
        self.calls: list[tuple] = []
        self._sessions: dict[str, dict] = {}
        self._videos: dict[str, dict] = {}
        self._session_seq = 0
        self._video_seq = 0

    # -- resumable upload ------------------------------------------------------
    def create_resumable_session(self, metadata: VideoMetadata, file_size: int) -> str:
        self.calls.append(("create_resumable_session", metadata.title, file_size))
        self._session_seq += 1
        uri = f"fake-session://{self._session_seq}"
        self._sessions[uri] = {
            "metadata": metadata, "file_size": file_size,
            "confirmed": 0, "video_id": None,
        }
        return uri

    def _complete(self, session: dict) -> str:
        self._video_seq += 1
        video_id = f"fake-video-{self._video_seq}"
        session["video_id"] = video_id
        meta: VideoMetadata = session["metadata"]
        self._videos[video_id] = {
            "title": meta.title,
            "privacy": "private",
            "publish_at": None,
            "published_at": None,
            "thumbnail": False,
            "captions": [],
        }
        return video_id

    def upload_chunk(self, session_uri: str, data: bytes, offset: int) -> ChunkResult:
        self.calls.append(("upload_chunk", session_uri, offset, len(data)))
        session = self._sessions[session_uri]
        if offset == session["confirmed"]:
            session["confirmed"] += len(data)
        if session["confirmed"] >= session["file_size"]:
            video_id = session["video_id"] or self._complete(session)
            return ChunkResult(201, session["file_size"], video_id)
        return ChunkResult(308, session["confirmed"])

    def query_upload_status(self, session_uri: str) -> ChunkResult:
        self.calls.append(("query_upload_status", session_uri))
        session = self._sessions[session_uri]
        if session["video_id"]:
            return ChunkResult(201, session["file_size"], session["video_id"])
        return ChunkResult(308, session["confirmed"])

    # -- enrichment --------------------------------------------------------------
    def set_thumbnail(self, video_id: str, thumbnail_path: str) -> None:
        self.calls.append(("set_thumbnail", video_id, thumbnail_path))
        self._videos[video_id]["thumbnail"] = True

    def insert_captions(self, video_id: str, srt_path: str, language: str) -> str:
        self.calls.append(("insert_captions", video_id, srt_path, language))
        self._videos[video_id]["captions"].append(language)
        return "fake-caption-1"

    def add_to_playlist(self, playlist_id: str, video_id: str) -> str:
        self.calls.append(("add_to_playlist", playlist_id, video_id))
        return "fake-playlist-item-1"

    # -- updates / status ----------------------------------------------------------
    def update_video(self, video_id: str, update: Any) -> None:
        self.calls.append(("update_video", video_id, update.privacy_status))
        record = self._videos[video_id]
        if getattr(update, "privacy_status", None) is not None:
            record["privacy"] = update.privacy_status
        if getattr(update, "clear_publish_at", False):
            record["publish_at"] = None
        if getattr(update, "publish_at", None) is not None:
            record["publish_at"] = update.publish_at

    def get_video_status(self, video_id: str) -> VideoStatus:
        self.calls.append(("get_video_status", video_id))
        record = self._videos[video_id]
        return VideoStatus(
            video_id=video_id,
            privacy_status=record["privacy"],
            publish_at=record["publish_at"],
            published_at=record["published_at"],
            upload_status="processed",
            processing_status="succeeded",
            duration_seconds=self.expected_duration_s or None,
            definition="hd",
            title=record["title"],
            description="",
            thumbnail_present=record["thumbnail"],
            caption_languages=list(record["captions"]),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Providers this pipeline's sourcing layer uses, with their G2 tier.
_PROVIDER_TIERS = {
    "generated": 1, "ai_image": 1, "ai_generated": 1, "own": 1,
    "pexels": 1, "pixabay": 1, "stock": 1, "youtube_audio_library": 1,
    "local": 1, "local_plate": 1, "manual_upload": 2, "licensed_stock": 2,
}

#: Provider spellings the visuals/assemble stages use for the app's
#: OWN locally generated Pillow plates.  Round 6 closure (W7): this
#: canonicalisation used to live ONLY in the two package-stage
#: callers (``produce.py`` / ``stage_bridge.run_package`` translated
#: ``local_plate`` -> ``generated`` before calling
#: :func:`finalize_publish`), while the publish finaliser passed the
#: DAG's raw licence segments through untouched — so the SAME asset
#: registry passed G2 at package time (Tier-1 ``generated``) and
#: failed G2 in the finaliser (unknown tier for ``local_plate``,
#: ``G2_PROVENANCE_FAILED``), and the package report claimed a pass
#: the finaliser would not honour (frozen-produce smoke, Round 6).
#: The translation now lives HERE, in the shared finalize code every
#: caller funnels through, so the registry is judged identically no
#: matter which entry point evaluates it.  The assets are unchanged;
#: only the provider label is canonicalised, exactly as the two
#: package callers already did (their translations are now redundant
#: but harmless/idempotent).
_LOCAL_PLATE_PROVIDERS = frozenset(
    {"local_plate", "local-plates", "local plate", "localplate"}
)


def _canonical_provider(provider: Any) -> Optional[str]:
    """Canonical provider label for Gate B provenance (see above)."""
    if provider is None:
        return None
    text = str(provider).strip()
    if not text:
        return None
    if text.lower() in _LOCAL_PLATE_PROVIDERS:
        return "generated"
    return text


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return bool(value)


def _gate_a_value(gate_a_passed: Any) -> bool:
    if isinstance(gate_a_passed, bool):
        return gate_a_passed
    if gate_a_passed is None:
        return False
    nested = _get(gate_a_passed, "passed", "allowed", "gate_a_passed")
    if isinstance(nested, bool):
        return nested
    verdict = _get(gate_a_passed, "verdict")
    if verdict is not None:
        return str(getattr(verdict, "value", verdict)).upper() == "PASS"
    return bool(gate_a_passed)


def _approval_recorded(gate_b_approval: Any) -> bool:
    """Interpret ``gate_b_approval`` (bool | record | None) as recorded-or-not.

    A *record* is a mapping / object carrying an explicit approval flag
    (``approved`` / ``recorded`` / ``has_recorded_approval`` /
    ``approval_recorded``) or, failing that, a non-empty approval /
    actor identifier.  A plain non-empty string counts as an approval
    record id.  ``None`` / ``False`` / empty never count.
    """
    if gate_b_approval is None:
        return False
    if isinstance(gate_b_approval, bool):
        return gate_b_approval
    if isinstance(gate_b_approval, str):
        return bool(gate_b_approval.strip())
    if isinstance(gate_b_approval, Mapping):
        for key in ("approved", "recorded", "has_recorded_approval",
                    "approval_recorded", "gate_b_approval"):
            if key in gate_b_approval:
                return bool(gate_b_approval[key])
        for key in ("approval_id", "record_id", "id", "actor", "approved_by"):
            value = gate_b_approval.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if value:
                return True
        return bool(gate_b_approval)
    # Object-shaped record.
    for attr in ("approved", "recorded", "has_recorded_approval",
                 "approval_recorded"):
        value = getattr(gate_b_approval, attr, None)
        if value is not None:
            return bool(value)
    for attr in ("approval_id", "record_id", "id", "actor", "approved_by"):
        value = getattr(gate_b_approval, attr, None)
        if value:
            return True
    return bool(gate_b_approval)


def _approval_actor(gate_b_approval: Any) -> str:
    if isinstance(gate_b_approval, Mapping):
        for key in ("actor", "approved_by", "reviewer", "user"):
            value = gate_b_approval.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return "reviewer"
    if gate_b_approval is not None and not isinstance(gate_b_approval, (bool, str)):
        for attr in ("actor", "approved_by", "reviewer", "user"):
            value = getattr(gate_b_approval, attr, None)
            if isinstance(value, str) and value.strip():
                return value
    return "reviewer"


def _tuple_of(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


def _record_gate_b_audit(sink_path: Any, event: dict) -> None:
    """Guarded call to Worker 3's audit module (no-op if not landed)."""
    try:
        from app.pipeline.gates.audit import record_gate_b_event

        record_gate_b_event(sink_path, event)
    except Exception:
        return


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_licence_rows(assemble: Any, bundle: Any, qc_info: Any) -> list[dict]:
    rows: list[dict] = []
    for source in (
        _get(assemble, "licence_segments", "usage_segments", "licence_rows"),
        _nested(bundle, "licence_ledger", "licence_rows", "ledger_rows",
                "licences", "usage_segments", "assets"),
        _get(qc_info, "licence_segments", "licence_rows"),
    ):
        if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            for row in source:
                if isinstance(row, Mapping):
                    rows.append(dict(row))
                elif row is not None:
                    rows.append(
                        {
                            "asset_id": _first_text(_get(row, "asset_id")),
                            "start_s": _get(row, "start_s"),
                            "end_s": _get(row, "end_s"),
                            "licence_id": _first_text(
                                _get(row, "licence_id", "licence_ref")),
                            "provider": _first_text(_get(row, "provider")),
                            "content_hash": _first_text(
                                _get(row, "content_hash", "asset_hash")),
                        }
                    )
    return rows


def _build_copyright_inputs(rows: list[dict]):
    licences: dict[str, LicenceRecord] = {}
    provenances: dict[str, AssetProvenance] = {}
    segments: list[UsageSegment] = []
    for row in rows:
        asset_id = _first_text(row.get("asset_id"), row.get("scene_id"))
        if not asset_id:
            continue
        start = row.get("start_s")
        end = row.get("end_s")
        segments.append(
            UsageSegment(
                asset_id=asset_id,
                start_s=float(start) if start is not None else None,
                end_s=float(end) if end is not None else None,
                role=str(row.get("role", "b_roll")),
            )
        )
        licences[asset_id] = LicenceRecord(
            asset_id=asset_id,
            licence_id=_first_text(
                row.get("licence_id"), row.get("licence_ref")) or None,
            commercial_use_ok=_coerce_bool(
                row.get("commercial_use_ok", row.get("commercial_ok")), True),
            monetized_youtube_ok=_coerce_bool(
                row.get("monetized_youtube_ok", row.get("monetised_ok")), True),
            attribution_required=_coerce_bool(row.get("attribution_required")),
            attribution_text=row.get("attribution_text") or None,
            review_status=str(row.get("review_status", "approved")),
        )
        provider = _canonical_provider(_first_text(row.get("provider")))
        tier = row.get("tier")
        if tier not in (1, 2, 3):
            tier = _PROVIDER_TIERS.get((provider or "").strip().lower())
        provenances[asset_id] = AssetProvenance(
            asset_id=asset_id,
            provider=provider,
            tier=int(tier) if tier in (1, 2, 3) else None,
            acquisition_hash=_first_text(
                row.get("content_hash"), row.get("acquisition_hash"),
                row.get("asset_hash")) or None,
            acquisition_record_id=_first_text(
                row.get("acquisition_record_id"), row.get("licence_id")) or None,
            watermark_detected=_coerce_bool(row.get("watermark_detected")),
        )
    return segments, licences, provenances


def _resolve_qc_verdict(assemble: Any, qc_info: Any) -> tuple[Verdict, tuple, Any]:
    """(verdict, reason_codes, qc_report_or_None) from qc_info / assemble."""
    report = qc_info if hasattr(qc_info, "to_token") else _get(assemble, "qc_report")
    if report is not None and hasattr(report, "verdict"):
        codes = tuple(
            code for c in getattr(report, "checks", ()) for code in c.reason_codes
        )
        return Verdict(report.verdict), codes, report
    if isinstance(qc_info, Mapping):
        verdict_raw = qc_info.get("verdict")
        if verdict_raw:
            codes: list[str] = []
            checks = qc_info.get("checks", {})
            if isinstance(checks, Mapping):
                for check in checks.values():
                    if isinstance(check, Mapping):
                        codes.extend(check.get("reason_codes", []))
            return Verdict(str(verdict_raw)), tuple(codes), None
    summary = _get(assemble, "qc_summary") or {}
    if isinstance(summary, Mapping) and summary.get("verdict"):
        codes = []
        checks = summary.get("checks", {})
        if isinstance(checks, Mapping):
            for check in checks.values():
                if isinstance(check, Mapping):
                    codes.extend(check.get("reason_codes", []))
        return Verdict(str(summary["verdict"])), tuple(codes), None
    passed = _get(assemble, "qc_passed")
    return (Verdict.PASS if passed else Verdict.BLOCK, (), None)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _resolve_compliance(
    compliance_state: Any, requested_visibility: str
) -> tuple[str, dict]:
    """(overall, decision) for this publish (Round 6 Compliance Gate).

    ``compliance_state=None`` resolves the STORED state from the
    app's default DB — production behaviour.  Any failure to read a
    stored state resolves to ``not_started``: a gate that cannot
    prove a pass never acts as one.  An explicit value (overall
    string / bool / state dict) is honoured as given, which is how
    tests and the publish finaliser (which reads its own DB) drive
    the gate.
    """
    from app.compliance.gate import coerce_overall, evaluate_visibility

    overall: Optional[str] = None
    if compliance_state is not None:
        overall = coerce_overall(compliance_state)
    else:
        try:
            from app.compliance.service import get_state_from_default_db

            overall = coerce_overall(get_state_from_default_db())
        except Exception:
            overall = None
    overall = overall or "not_started"
    return overall, dict(evaluate_visibility(requested_visibility, overall))


def finalize_publish(
    assemble: Any,
    qc_info: Any,
    bundle: Any,
    profile: Any,
    ctx: Any,
    gate_a_passed: Any,
    *,
    mode: str = "review_first",
    publish_mode: Optional[str] = None,
    gate_b_approval: Any = None,
    audit_sink: Any = None,
    compliance_state: Any = None,
    requested_visibility: str = "public",
) -> PublishResult:
    """Freeze the publish manifest + Gate B verdict (see module docstring).

    Round-4 conditional-pass additions (all keyword-only, defaults
    preserve the legacy PASS/BLOCK behaviour exactly):

    * ``mode`` / ``publish_mode`` — the publish mode
      (``"review_first"`` | ``"auto"``; ``publish_mode`` wins when both
      are given).  Defaults to ``"review_first"``.
    * ``gate_b_approval`` — ``None`` (default), a bool, or an approval
      *record* (mapping/object/string id) representing a recorded human
      approval of a conditional Gate B pass.  A conditional QC result
      (WARN, minor-only flags) stays frozen without it, releases to
      the fake-provider simulation with it in ``review_first`` mode,
      and never releases in ``auto`` mode.
    * ``audit_sink`` — path for Worker 3's gate-B audit JSONL; defaults
      to ``<pack_dir>/gate_b_audit.jsonl`` for conditional decisions.
    """
    effective_mode = str(publish_mode or mode or "review_first")
    # Round 6 Compliance Gate (Gate C): resolved once, up front, and
    # disclosed in the notes / manifest / result whatever happens.
    compliance_overall, compliance_decision = _resolve_compliance(
        compliance_state, requested_visibility
    )
    has_recorded_approval = _approval_recorded(gate_b_approval)
    approval_actor = _approval_actor(gate_b_approval) if has_recorded_approval else "system"
    notes: dict[str, Any] = {"compliance_gate": dict(compliance_decision)}
    gate_a_ok = _gate_a_value(gate_a_passed)

    pack = _get(ctx, "pack_dir", "package_dir", "output_dir") or _get(assemble, "pack_dir")
    if pack is None:
        raise ValueError("neither ctx nor assemble carries a pack_dir")
    pack_dir = Path(str(pack))
    pack_dir.mkdir(parents=True, exist_ok=True)

    master_path = Path(str(_get(assemble, "final_mp4", "final_path", "path")))
    if not master_path.is_file():
        raise ValueError(f"assembled master does not exist: {master_path}")
    master_checksum = _file_sha256(master_path)
    master_size = master_path.stat().st_size

    duration_s = float(_get(assemble, "duration_s") or 0.0)
    width, height = 1280, 720
    try:
        probe = probe_media(master_path)
        streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
        if streams:
            width = int(streams[0].get("width") or width)
            height = int(streams[0].get("height") or height)
        if not duration_s:
            duration_s = float(probe.get("format", {}).get("duration") or 0.0)
    except Exception as exc:
        notes["probe_error"] = f"{type(exc).__name__}: {exc}"
    if duration_s <= 0:
        raise ValueError("cannot freeze a manifest without a positive duration")

    project_id = _first_text(
        _get(ctx, "project_id"), _nested(bundle, "project_id", "video_id", "id"),
        _get(assemble, "project_id"), "project",
    )
    channel_id = _first_text(
        _get(ctx, "channel_id"),
        _nested(bundle, "channel_id", "channel"),
        _get(profile, "channel_id"),
        "channel",
    )
    title = _first_text(_nested(bundle, "title", "video_title"), "Untitled Video")[:100]
    description = _first_text(
        _nested(bundle, "description", "video_description"))[:5000]
    tags = _get(bundle, "tags") or _nested(bundle, "tags", "keywords") or []
    tags = [str(t) for t in tags] if isinstance(tags, Sequence) else []
    category_id = _first_text(_nested(bundle, "category_id", "category"), "22")
    caption_language = _first_text(
        _nested(bundle, "caption_language"), "en")
    if caption_language not in ("en", "ur"):
        lang = caption_language.lower()
        caption_language = "ur" if "urdu" in lang else "en"
    thumbnails = _get(assemble, "thumbnails") or []
    thumbnail_path = str(thumbnails[0]) if thumbnails else ""
    caption_path = pack_dir / "captions.srt"
    if not caption_path.is_file():
        alt = _get(assemble, "captions_path", "caption_path")
        caption_path = Path(str(alt)) if alt else caption_path

    # -- copyright gates over the project's licence ledger rows ---------------
    rows = _collect_licence_rows(assemble, bundle, qc_info)
    if not rows:
        rows = [
            {
                "asset_id": "master-visuals",
                "start_s": 0.0,
                "end_s": duration_s,
                "licence_id": f"OWN-{project_id}",
                "provider": "generated",
                "content_hash": master_checksum,
                "role": "b_roll",
            }
        ]
        notes["licence_rows"] = (
            "no ledger rows supplied; single own/generated row covering "
            "the full timeline was synthesised from the master checksum"
        )
    segments, licences, provenances = _build_copyright_inputs(rows)
    copyright_report = run_copyright_gates(
        segments=segments,
        licences=licences,
        provenances=provenances,
        description=description,
        total_duration_s=duration_s,
    )
    if copyright_report.g1.verdict is Verdict.PASS:
        completeness = 1.0
    else:
        completeness = max(
            0.0, (duration_s - copyright_report.g1.offending_seconds) / duration_s
        )
    notes["copyright"] = {
        "g1": copyright_report.g1.verdict.value,
        "g2": copyright_report.g2.verdict.value,
        "g6": copyright_report.g6.verdict.value,
        "reason_codes": list(copyright_report.reason_codes),
        "licence_ledger_completeness": completeness,
    }

    # -- policy similarity (single video, no channel history -> real check) ---
    script_text = _first_text(
        _nested(bundle, "script_text", "script", "full_script"))
    history = _nested(bundle, "previous_videos", "history", "published_history") or []
    previous = []
    if isinstance(history, Sequence):
        for entry in history:
            if isinstance(entry, VideoFingerprint):
                previous.append(entry)
            elif entry is not None:
                previous.append(
                    VideoFingerprint(
                        video_id=_first_text(_get(entry, "video_id", "id"), "prev"),
                        script_text=_first_text(_get(entry, "script_text", "script")),
                    )
                )
    fingerprint = VideoFingerprint(
        video_id=project_id,
        script_text=script_text,
        structure_template_id=_first_text(
            _nested(bundle, "structure_template_id", "structure_id")) or None,
        hook_formula_id=_first_text(
            _nested(bundle, "hook_formula_id", "hook_id")) or None,
        thumbnail_template_id=_first_text(
            _nested(bundle, "thumbnail_template_id", "thumbnail_template")) or None,
        title_formula_id=_first_text(
            _nested(bundle, "title_formula_id", "title_formula")) or None,
    )
    policy_report = check_similarity(fingerprint, previous)
    notes["policy"] = {
        "verdict": policy_report.verdict.value,
        "reason_codes": list(policy_report.reason_codes),
        "previous_videos_compared": len(previous),
        "note": (
            "no published-video history for this channel yet"
            if not previous else f"compared against {len(previous)} prior videos"
        ),
    }

    qc_verdict, qc_reason_codes, qc_report = _resolve_qc_verdict(assemble, qc_info)
    notes["qc_verdict"] = qc_verdict.value

    # -- draft manifest -> draft tokens -> frozen manifest ---------------------
    draft = PublishManifest(
        project_id=project_id,
        channel_id=channel_id,
        master_path=str(master_path),
        master_checksum=master_checksum,
        master_size_bytes=master_size,
        duration_seconds=duration_s,
        resolution_width=width,
        resolution_height=height,
        thumbnail_path=thumbnail_path,
        caption_path=str(caption_path),
        caption_language=caption_language,
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        contains_synthetic_media=True,
        licence_ledger_completeness=completeness,
    )
    draft_hash = draft.compute_hash()

    if qc_report is not None:
        draft_qc_token = qc_report.to_token(draft_hash)
    else:
        draft_qc_token = issue_token(
            GATE_QC_TECHNICAL, qc_verdict, draft_hash,
            reason_codes=qc_reason_codes,
            evidence={"qc_passed": qc_verdict is Verdict.PASS, "via": "assemble_summary"},
        )
    draft_copyright_token = copyright_report.to_token(draft_hash)
    draft_policy_token = policy_report.to_token(draft_hash)

    manifest_path = pack_dir / "publish_manifest.json"
    problems: list[dict] = []

    try:
        frozen = draft.model_copy(
            update={
                "qc_pass_token": draft_qc_token.token_id,
                "copyright_pass_token": draft_copyright_token.token_id,
                "policy_pass_token": draft_policy_token.token_id,
            }
        ).freeze()
    except Exception as exc:
        # Manifest cannot freeze (e.g. licence ledger < 100%): Gate B
        # fails honestly; the unfrozen manifest is still written out.
        freeze_reason_codes = list(collect_gate_b_reason_codes(
            [c for t in (draft_qc_token, draft_copyright_token,
                         draft_policy_token)
             for c in (t.reason_codes or ())],
            [{"code": "FREEZE_FAILED"}],
        ))
        manifest_dict: dict[str, Any] = draft.model_dump(mode="json")
        manifest_dict.update(
            {
                "idempotency_key": draft.idempotency_key,
                "gate_a_passed": gate_a_ok,
                "gate_b": {
                    "passed": False,
                    "verdict": "block",
                    "outcome": "block",
                    "reason_codes": freeze_reason_codes,
                    "freeze_error": f"{type(exc).__name__}: {exc}",
                    "draft_manifest_hash": draft_hash,
                },
                "publish_state": "gate_b_failed_manifest_not_frozen",
                "compliance_gate": dict(compliance_decision),
            }
        )
        manifest_path.write_text(
            json.dumps(manifest_dict, indent=2, ensure_ascii=False), encoding="utf-8")
        return PublishResult(
            gate_b_passed=False,
            gate_b_verdict="block",
            gate_b_reason_codes=freeze_reason_codes,
            manifest=manifest_dict,
            manifest_path=str(manifest_path),
            publish_state="gate_b_failed_manifest_not_frozen",
            compliance_gate=dict(compliance_decision),
            tokens=[
                draft_qc_token.to_dict(),
                draft_copyright_token.to_dict(),
                draft_policy_token.to_dict(),
            ],
            gate_b_problems=[
                {"group": "manifest", "code": "FREEZE_FAILED", "detail": str(exc)}
            ],
            notes=notes,
        )

    final_hash = frozen.manifest_hash or frozen.compute_hash()

    # -- final tokens bound to the FROZEN manifest hash -------------------------
    if qc_report is not None:
        qc_token = qc_report.to_token(final_hash)
    else:
        qc_token = issue_token(
            GATE_QC_TECHNICAL, qc_verdict, final_hash,
            reason_codes=qc_reason_codes,
            evidence={"qc_passed": qc_verdict is Verdict.PASS, "via": "assemble_summary"},
        )
    copyright_token = copyright_report.to_token(final_hash)
    policy_token = policy_report.to_token(final_hash)
    final_tokens: list[GateToken] = [qc_token, copyright_token, policy_token]

    # Strict PASS-only verdict (unchanged) + conditional-aware policy.
    gate_b_strict = verify_gate_b(final_tokens, final_hash)
    gate_b_policy = evaluate_gate_b(
        final_tokens, final_hash,
        mode=effective_mode,
        has_recorded_approval=has_recorded_approval,
    )
    gate_b = gate_b_policy  # .allowed/.satisfied_groups/.problems shape-compatible
    problems = [
        {
            "group": p.group,
            "expected_gate_id": p.expected_gate_id,
            "code": p.code,
            "detail": p.detail,
        }
        for p in gate_b.problems
    ]
    if not gate_a_ok:
        problems.append(
            {
                "group": "gate_a",
                "expected_gate_id": "gate_a.script_lock",
                "code": "GATE_A_NOT_PASSED",
                "detail": "Gate B requires Gate A (script lock) to have passed",
            }
        )
    gate_b_passed = bool(gate_b.allowed and gate_a_ok)

    # -- shared verdict labelling (Round 6 closure, W7) -------------------
    # Derived from the SAME GateBPolicyResult the finaliser will act
    # on, via the shared aggregator helpers — never re-derived per
    # caller.  ``gate_b_reason_codes`` preserves the typed token
    # reasons (G2_PROVENANCE_FAILED, QC flags, ...) that the strict
    # problem code (VERDICT_NOT_PASS) alone would lose.
    gate_b_reason_codes = list(collect_gate_b_reason_codes(
        [c for t in final_tokens for c in (t.reason_codes or ())],
        problems,
    ))
    gate_b_verdict = gate_b_verdict_label(gate_b_policy.outcome, gate_b_passed)
    notes["gate_b_verdict"] = gate_b_verdict
    notes["gate_b_reason_codes"] = gate_b_reason_codes

    # -- conditional-pass bookkeeping + audit --------------------------------
    conditional_info: dict[str, Any] = {}
    if gate_b_policy.outcome == "conditional" and gate_b_policy.classification is not None:
        cls = gate_b_policy.classification
        dec = gate_b_policy.decision
        qc_flags = [str(c) for c in (qc_token.reason_codes or ())]
        minor_flags = [str(f) for f in gate_b_policy.minor_flags]
        try:
            from app.pipeline.gates.audit import asset_ids_from_ledger

            asset_ids = list(asset_ids_from_ledger(rows))
        except Exception:
            asset_ids = sorted({str(r.get("asset_id")) for r in rows if r.get("asset_id")})
        conditional_info = {
            "outcome": gate_b_policy.outcome,
            "mode": effective_mode,
            "has_recorded_approval": has_recorded_approval,
            "allowed": bool(gate_b_policy.allowed and gate_a_ok),
            "qc_flags": qc_flags,
            "minor_flags": minor_flags,
            "non_minor_flags": [str(f) for f in gate_b_policy.non_minor_flags],
            "policy_version": gate_b_policy.policy_version,
            "classification_reasons": [str(r) for r in _tuple_of(getattr(cls, "reasons", ()))],
            "decision_outcome": str(getattr(dec, "outcome", "")) if dec else None,
            "decision_reasons": [str(r) for r in _tuple_of(getattr(dec, "reasons", ()))] if dec else [],
            "strict_gate_b_passed": bool(gate_b_strict.allowed),
        }
        notes["gate_b_conditional"] = conditional_info
        if gate_b_policy.allowed and gate_a_ok:
            event_type = "publish_released"
            event_actor = approval_actor
        elif effective_mode == "auto":
            event_type = "blocked"
            event_actor = approval_actor if has_recorded_approval else "system"
        elif has_recorded_approval:
            # Approval recorded but Gate A / another group still blocks.
            event_type = "blocked"
            event_actor = approval_actor
        else:
            event_type = "conditional_token_minted"
            event_actor = "system"
        sink = audit_sink if audit_sink is not None else (pack_dir / "gate_b_audit.jsonl")
        _record_gate_b_audit(sink, {
            "event_type": event_type,
            "manifest_hash": final_hash,
            "mode": effective_mode,
            "outcome": gate_b_policy.outcome,
            "qc_flags": qc_flags,
            "minor_flags": minor_flags,
            "asset_ids": asset_ids,
            "policy_version": gate_b_policy.policy_version,
            "actor": event_actor,
        })
        if has_recorded_approval and gate_b_policy.allowed and gate_a_ok:
            # Approval itself is an auditable event, distinct from release.
            _record_gate_b_audit(sink, {
                "event_type": "approval_recorded",
                "manifest_hash": final_hash,
                "mode": effective_mode,
                "outcome": gate_b_policy.outcome,
                "qc_flags": qc_flags,
                "minor_flags": minor_flags,
                "asset_ids": asset_ids,
                "policy_version": gate_b_policy.policy_version,
                "actor": approval_actor,
            })

    # -- fake-provider workflow (dry run only; no real upload) -------------------
    publish_state = "manifest_frozen_not_uploaded"
    video_id: Optional[str] = None
    if gate_b_passed:
        try:
            fake = FakeProvider(expected_duration_s=duration_s)
            workflow = PublishingWorkflow(
                fake,
                InMemoryStore(),
                compliance_checker=lambda: compliance_overall,
            )
            data = master_path.read_bytes()
            job = workflow.run(frozen, data)
            video_id = job.video_id
            notes["fake_provider"] = {
                "calls": [c[0] for c in fake.calls],
                "job_state": job.state.value,
                "video_id": video_id,
            }
            if getattr(job, "compliance_block", None):
                # Compliance Gate hold: the simulated upload completed
                # but the video was FORCED PRIVATE — the public
                # release was refused with the typed reason.  The
                # publish_state stays the honest simulated-upload
                # state; the forced-private fact is disclosed in
                # notes / manifest / result compliance_gate blocks.
                notes["compliance_gate"] = {
                    **compliance_decision,
                    "forced_private": True,
                    "effective_visibility": "private",
                    "public_publish_refused": True,
                    "held_at_state": job.state.value,
                    "disclosure": (
                        "Simulated publish was forced to Private by "
                        "the Compliance Gate (compliance_not_passed); "
                        "no public release was attempted."
                    ),
                }
                compliance_decision = notes["compliance_gate"]
                publish_state = (
                    "fake_provider_upload_simulated"
                    if video_id
                    else "manifest_frozen_not_uploaded"
                )
            else:
                publish_state = (
                    "fake_provider_upload_simulated"
                    if job.state.value == "POST_LIVE_DONE"
                    else f"fake_provider_{job.state.value.lower()}"
                )
        except Exception as exc:
            notes["fake_provider_error"] = f"{type(exc).__name__}: {exc}"
            publish_state = "manifest_frozen_not_uploaded"
    else:
        notes["upload_skipped"] = "Gate B did not pass; no upload attempted"

    manifest_dict = frozen.model_dump(mode="json")
    manifest_dict.update(
        {
            "idempotency_key": frozen.idempotency_key,
            "gate_a_passed": gate_a_ok,
            "gate_b": {
                "passed": gate_b_passed,
                "verdict": gate_b_verdict,
                "reason_codes": gate_b_reason_codes,
                "outcome": gate_b_policy.outcome,
                "mode": effective_mode,
                "has_recorded_approval": has_recorded_approval,
                "conditional": conditional_info,
                "strict_passed": bool(gate_b_strict.allowed and gate_a_ok),
                "satisfied_groups": list(gate_b.satisfied_groups),
                "problems": problems,
                "binding_manifest_hash": final_hash,
                "draft_manifest_hash": draft_hash,
                "tokens": [t.to_dict() for t in final_tokens],
            },
            "publish_state": publish_state,
            "video_id": video_id,
            "compliance_gate": dict(compliance_decision),
            "notes": notes,
        }
    )
    manifest_path.write_text(
        json.dumps(manifest_dict, indent=2, ensure_ascii=False), encoding="utf-8")

    return PublishResult(
        gate_b_passed=gate_b_passed,
        manifest=manifest_dict,
        manifest_path=str(manifest_path),
        publish_state=publish_state,
        tokens=[t.to_dict() for t in final_tokens],
        gate_b_problems=problems,
        video_id=video_id,
        notes=notes,
        gate_b_outcome=gate_b_policy.outcome,
        gate_b_conditional=conditional_info,
        gate_b_verdict=gate_b_verdict,
        gate_b_reason_codes=gate_b_reason_codes,
        compliance_gate=dict(compliance_decision),
    )
