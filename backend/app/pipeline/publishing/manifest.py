"""Publish Manifest — the frozen, immutable input to publishing (G0).

Identity: project_id + idempotency key = hash(master + channel + title).
Any edit after freezing derives manifest v2 and invalidates ALL
approvals / PASS-token bindings, so a human can never approve one
title/thumbnail while the system publishes another (Section 34).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .errors import ManifestError


class ReleaseMode(str, Enum):
    IMMEDIATE = "immediate"
    SCHEDULED = "scheduled"


def compute_idempotency_key(master_checksum: str, channel_id: str, title: str) -> str:
    """hash(master_file + channel_id + title).

    master_checksum IS the hash of the master file's bytes, so this binds
    the exact file, the exact channel and the exact title together.
    """
    raw = f"{master_checksum}\x00{channel_id}\x00{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class PublishManifest(BaseModel):
    """Frozen publish package. Construct, pass tokens in, then freeze()."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    channel_id: str
    playlist_id: str | None = None

    # Assets: paths + checksums + duration + resolution.
    master_path: str
    master_checksum: str
    master_size_bytes: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    resolution_width: int = Field(gt=0)
    resolution_height: int = Field(gt=0)
    thumbnail_path: str
    caption_path: str
    caption_language: str = "en"

    # Metadata snapshot (description carries real chapter timestamps).
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list)
    category_id: str = "22"
    default_language: str | None = None
    default_audio_language: str | None = None
    licence: str = "youtube"
    embeddable: bool = True

    # Declarations.
    made_for_kids: bool = False
    contains_synthetic_media: bool = False
    sensitive_niche_disclaimer: bool = False

    # Release intent.
    release_mode: ReleaseMode = ReleaseMode.IMMEDIATE
    target_slot: datetime | None = None
    fallback_slots: list[datetime] = Field(default_factory=list)

    # Gate evidence: PASS token ids from QC / Copyright / Policy agents.
    qc_pass_token: str | None = None
    copyright_pass_token: str | None = None
    policy_pass_token: str | None = None
    licence_ledger_completeness: float = Field(default=0.0, ge=0.0, le=1.0)

    manifest_version: int = 1
    manifest_hash: str | None = None
    is_frozen: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def idempotency_key(self) -> str:
        return compute_idempotency_key(
            self.master_checksum, self.channel_id, self.title
        )

    @property
    def resolution(self) -> str:
        return f"{self.resolution_width}x{self.resolution_height}"

    @model_validator(mode="after")
    def _scheduled_needs_slot(self) -> "PublishManifest":
        if self.release_mode is ReleaseMode.SCHEDULED and self.target_slot is None:
            raise ManifestError("scheduled release requires a target_slot")
        return self

    def _canonical_payload(self) -> str:
        data = self.model_dump(mode="json", exclude={"manifest_hash", "is_frozen"})
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def compute_hash(self) -> str:
        return hashlib.sha256(self._canonical_payload().encode("utf-8")).hexdigest()

    def gate_problems(self) -> list[str]:
        """Everything G0 still needs before this manifest may publish."""
        problems: list[str] = []
        if not self.qc_pass_token:
            problems.append("missing QC PASS token")
        if not self.copyright_pass_token:
            problems.append("missing Copyright PASS token")
        if not self.policy_pass_token:
            problems.append("missing Policy PASS token")
        if self.licence_ledger_completeness < 1.0:
            problems.append(
                "licence ledger completeness must be 100% "
                f"(got {self.licence_ledger_completeness:.0%})"
            )
        return problems

    def freeze(self) -> "PublishManifest":
        """Freeze: requires all PASS tokens + 100% licence ledger.

        Returns a NEW frozen instance carrying its manifest hash; the
        original is untouched (the model is immutable throughout).
        """
        problems = self.gate_problems()
        if problems:
            raise ManifestError("cannot freeze manifest: " + "; ".join(problems))
        frozen = self.model_copy(
            update={"manifest_hash": self.compute_hash(), "is_frozen": True}
        )
        if frozen.manifest_hash != frozen.compute_hash():
            raise ManifestError("manifest hash mismatch after freeze")
        return frozen

    def derive_new_version(self, **changes: object) -> "PublishManifest":
        """Edit after freezing → v+1, unfrozen, approvals invalidated.

        PASS-token bindings are cleared and the hash dropped: the new
        version must collect fresh gate evidence and be re-frozen.
        """
        updates: dict[str, object] = dict(changes)
        updates.update(
            {
                "manifest_version": self.manifest_version + 1,
                "manifest_hash": None,
                "is_frozen": False,
                "qc_pass_token": None,
                "copyright_pass_token": None,
                "policy_pass_token": None,
            }
        )
        return self.model_copy(update=updates)  # type: ignore[arg-type]
