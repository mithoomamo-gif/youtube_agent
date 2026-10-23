"""Safety gates for the YouTube Agent pipeline.

Gate tokens (:mod:`.tokens`), copyright gates G1/G2/G6
(:mod:`.copyright`), policy / similarity gates (:mod:`.policy`) and the
Gate B pre-publish aggregator (:mod:`.aggregator`).
"""

from .aggregator import REQUIRED_GATE_B_TOKENS, GateBProblem, GateBResult, verify_gate_b
from .copyright import (
    AssetProvenance,
    CopyrightReport,
    G1Report,
    G2Report,
    G6Report,
    LicenceRecord,
    UsageSegment,
    check_g1_licence_completeness,
    check_g2_provenance,
    check_g6_attribution,
    run_copyright_gates,
)
from .policy import (
    SimilarityReport,
    SynthesisReport,
    VideoFingerprint,
    check_similarity,
    check_source_synthesis,
    script_similarity,
)
from .tokens import (
    EXTERNAL_GATES,
    GATE_COPYRIGHT,
    GATE_COPYRIGHT_G1,
    GATE_COPYRIGHT_G2,
    GATE_COPYRIGHT_G6,
    GATE_G3_FINGERPRINT,
    GATE_G4_PRIVATE_UPLOAD_DRY_RUN,
    GATE_G5_ORIGINALITY_REVIEW,
    GATE_POLICY_SIMILARITY,
    GATE_POLICY_SYNTHESIS,
    GATE_QC_TECHNICAL,
    GateToken,
    Verdict,
    compute_manifest_hash,
    issue_token,
    verify_token_binding,
    worst_verdict,
)

__all__ = [
    "Verdict",
    "GateToken",
    "issue_token",
    "verify_token_binding",
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
    "LicenceRecord",
    "UsageSegment",
    "AssetProvenance",
    "G1Report",
    "G2Report",
    "G6Report",
    "CopyrightReport",
    "check_g1_licence_completeness",
    "check_g2_provenance",
    "check_g6_attribution",
    "run_copyright_gates",
    "VideoFingerprint",
    "SimilarityReport",
    "SynthesisReport",
    "check_similarity",
    "check_source_synthesis",
    "script_similarity",
    "REQUIRED_GATE_B_TOKENS",
    "GateBProblem",
    "GateBResult",
    "verify_gate_b",
]
