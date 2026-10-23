"""Gate token model for the YouTube Agent pipeline.

Every quality / copyright / policy gate emits a deterministic
``PASS`` / ``WARN`` / ``BLOCK`` verdict as a :class:`GateToken`.  A token is
bound to the exact publish-manifest hash it was issued against: any later
edit creates a new manifest (new hash) and therefore invalidates every
previously issued token (MASTER_ROADMAP Part 7.5 / Gate B).

Tokens carry an integrity digest (SHA-256 over a canonical JSON encoding of
the token payload) so a tampered or re-bound token is detectable without a
database lookup.  Verification is purely local and deterministic -- no
network, no clock dependence beyond the recorded ``issued_at`` string.

Gate id conventions
-------------------
``qc.technical``              Agent 32 technical QC (see ``pipeline.qc``).
``copyright``                 Aggregate copyright verdict (G1+G2+G6).
``copyright.g1_*``            G1 licence completeness.
``copyright.g2_*``            G2 provenance & contamination scan.
``copyright.g6_*``            G6 attribution & metadata compliance.
``policy.similarity``         Agent 34 cross-video similarity / variation.
``policy.source_synthesis``   >=3-source synthesis rule (roadmap section 13).

Out-of-scope gates (implemented elsewhere, never faked here) are named in
:data:`EXTERNAL_GATES`: G3 audio fingerprint pre-screen, G4 private-upload
dry run, and G5 human originality / reused-content review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Union

__all__ = [
    "Verdict",
    "GateToken",
    "issue_token",
    "verify_token_binding",
    "token_binding_problems",
    "compute_manifest_hash",
    "worst_verdict",
    "GATE_QC_TECHNICAL",
    "GATE_COPYRIGHT",
    "GATE_COPYRIGHT_G1",
    "GATE_COPYRIGHT_G2",
    "GATE_COPYRIGHT_G6",
    "GATE_G3_FINGERPRINT",
    "GATE_G4_PRIVATE_UPLOAD_DRY_RUN",
    "GATE_G5_ORIGINALITY_REVIEW",
    "GATE_POLICY_SIMILARITY",
    "GATE_POLICY_SYNTHESIS",
    "EXTERNAL_GATES",
]


# ---------------------------------------------------------------------------
# Gate ids
# ---------------------------------------------------------------------------

GATE_QC_TECHNICAL = "qc.technical"
GATE_COPYRIGHT = "copyright"
GATE_COPYRIGHT_G1 = "copyright.g1_licence_completeness"
GATE_COPYRIGHT_G2 = "copyright.g2_provenance"
GATE_COPYRIGHT_G6 = "copyright.g6_attribution"
GATE_POLICY_SIMILARITY = "policy.similarity"
GATE_POLICY_SYNTHESIS = "policy.source_synthesis"

# Gates that exist in the roadmap (section 16) but are deliberately NOT
# implemented in this package.  They are named here so no caller can
# accidentally treat their absence as a pass.
GATE_G3_FINGERPRINT = "copyright.g3_fingerprint"
GATE_G4_PRIVATE_UPLOAD_DRY_RUN = "copyright.g4_private_upload_dry_run"
GATE_G5_ORIGINALITY_REVIEW = "policy.g5_originality_review"

EXTERNAL_GATES: Mapping[str, str] = {
    GATE_G3_FINGERPRINT: (
        "Audio fingerprint pre-screen vs a commercial recognition DB "
        "(ACRCloud/AudD). Implemented by the Fingerprint Pre-Screen "
        "connector step, not in this package."
    ),
    GATE_G4_PRIVATE_UPLOAD_DRY_RUN: (
        "Private/Unlisted upload to the target channel so YouTube's own "
        "ingest checks run before publication. Implemented by the Upload "
        "Agent workflow, not in this package."
    ),
    GATE_G5_ORIGINALITY_REVIEW: (
        "Originality / reused-content review. Owned by the Policy & "
        "Monetisation Agent plus a human reviewer; a failure here is a "
        "human decision and is never auto-faked."
    ),
}


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


class Verdict(str, Enum):
    """Gate verdict. ``WARN`` = auto-fixable once, then re-check.
    ``BLOCK`` = human required / hard stop (roadmap section 13)."""

    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY = {Verdict.PASS: 0, Verdict.WARN: 1, Verdict.BLOCK: 2}


def worst_verdict(verdicts: Iterable[Verdict]) -> Verdict:
    """Return the most severe verdict in ``verdicts`` (PASS if empty)."""
    worst = Verdict.PASS
    for verdict in verdicts:
        if verdict.severity > worst.severity:
            worst = verdict
    return worst


# ---------------------------------------------------------------------------
# Canonicalisation + hashing helpers
# ---------------------------------------------------------------------------


def _canonicalize(value: Any) -> Any:
    """Convert ``value`` into a deterministic, JSON-serialisable structure."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonicalize(v) for v in value), key=repr)
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _canonicalize(getattr(value, name))
            for name in value.__dataclass_fields__  # type: ignore[attr-defined]
        }
    return str(value)


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding used for digests and manifest hashes."""
    return json.dumps(
        _canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def compute_manifest_hash(data: Union[bytes, bytearray, str, Mapping[str, Any], Any]) -> str:
    """SHA-256 hex digest identifying a publish manifest.

    Accepts raw bytes, a string, or any JSON-able structure (encoded with
    :func:`canonical_json`, so key order never changes the hash).
    """
    if isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    elif isinstance(data, str):
        payload = data.encode("utf-8")
    else:
        payload = canonical_json(data).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_hash(manifest_hash: str) -> str:
    return manifest_hash.strip().lower()


def _format_issued_at(issued_at: Union[None, str, datetime]) -> str:
    if issued_at is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(issued_at, datetime):
        dt = issued_at if issued_at.tzinfo else issued_at.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(issued_at)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateToken:
    """An immutable gate verdict bound to one manifest hash.

    ``digest`` is the SHA-256 of the canonical payload (gate id, verdict,
    reason codes, evidence, manifest hash, timestamp).  ``token_id`` is the
    digest prefix -- identical inputs re-issue the identical token, which
    keeps issuance deterministic and idempotent.
    """

    gate_id: str
    verdict: Verdict
    manifest_hash: str
    reason_codes: tuple = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    issued_at: str = ""
    token_id: str = ""
    digest: str = ""

    def payload(self) -> dict:
        """The exact structure covered by :attr:`digest`."""
        return {
            "gate_id": self.gate_id,
            "verdict": self.verdict.value,
            "manifest_hash": _normalize_hash(self.manifest_hash),
            "reason_codes": list(self.reason_codes),
            "evidence": _canonicalize(dict(self.evidence)),
            "issued_at": self.issued_at,
        }

    def compute_digest(self) -> str:
        return hashlib.sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        data = self.payload()
        data["token_id"] = self.token_id
        data["digest"] = self.digest
        return data


def issue_token(
    gate_id: str,
    verdict: Verdict,
    manifest_hash: str,
    *,
    reason_codes: Iterable[str] = (),
    evidence: Optional[Mapping[str, Any]] = None,
    issued_at: Union[None, str, datetime] = None,
) -> GateToken:
    """Create a :class:`GateToken` bound to ``manifest_hash``."""
    token = GateToken(
        gate_id=gate_id,
        verdict=Verdict(verdict),
        manifest_hash=_normalize_hash(manifest_hash),
        reason_codes=tuple(reason_codes),
        evidence=dict(evidence or {}),
        issued_at=_format_issued_at(issued_at),
    )
    digest = token.compute_digest()
    return GateToken(
        gate_id=token.gate_id,
        verdict=token.verdict,
        manifest_hash=token.manifest_hash,
        reason_codes=token.reason_codes,
        evidence=token.evidence,
        issued_at=token.issued_at,
        token_id=digest[:32],
        digest=digest,
    )


def token_binding_problems(token: GateToken, manifest_hash: str) -> list:
    """Return a list of binding/integrity problems (empty == valid).

    Possible entries: ``"MANIFEST_HASH_MISMATCH"`` (token was issued for a
    different manifest) and ``"TOKEN_TAMPERED"`` (stored digest does not
    match the token payload, e.g. the verdict was edited after issuance).
    """
    problems = []
    if _normalize_hash(token.manifest_hash) != _normalize_hash(manifest_hash):
        problems.append("MANIFEST_HASH_MISMATCH")
    if not token.digest or token.digest != token.compute_digest():
        problems.append("TOKEN_TAMPERED")
    return problems


def verify_token_binding(token: GateToken, manifest_hash: str) -> bool:
    """True iff ``token`` is intact and was issued for ``manifest_hash``."""
    return not token_binding_problems(token, manifest_hash)
