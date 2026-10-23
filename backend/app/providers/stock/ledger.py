"""Licence ledger (§24) — mandatory, append-only, render-gating.

RULE enforced in code: no ledger row = asset cannot enter render
(``assert_renderable`` raises). Ledger rows are NEVER deleted: there is
deliberately no delete API, and ``delete_row`` raises if anything calls it.
``retrieved_by_key_id`` stores the key REF, never the key value.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .licences import LicenceRecord
from .models import Candidate, DownloadedAsset, SensitiveFlags, utcnow

CSV_FIELDS = [
    "ledger_id",
    "project_id",
    "scene_id",
    "provider",
    "provider_asset_id",
    "asset_page_url",
    "download_url",
    "author_name",
    "author_url",
    "licence_name",
    "licence_url",
    "licence_snapshot_date",
    "licence_terms_hash",
    "commercial_use_allowed",
    "attribution_required",
    "standalone_use_prohibited",
    "modification_allowed",
    "sensitive_flags",
    "retrieved_at",
    "retrieved_by_key_id",
    "local_file_path",
    "file_sha256",
    "width",
    "height",
    "duration_s",
    "fps",
    "times_used_in_project",
    "times_used_on_channel",
    "qc_status",
]


class LicenceLedgerError(Exception):
    """Raised by the render gate when an asset has no (valid) ledger row."""


@dataclass
class LedgerRow:
    project_id: str
    scene_id: str
    provider: str
    provider_asset_id: str
    asset_page_url: str = ""
    download_url: str = ""
    author_name: str = ""
    author_url: str = ""
    licence_name: str = ""
    licence_url: str = ""
    licence_snapshot_date: str = ""
    licence_terms_hash: str = ""
    commercial_use_allowed: bool = False
    attribution_required: bool = False
    standalone_use_prohibited: bool = True
    modification_allowed: bool = False
    sensitive_flags: dict = field(default_factory=dict)
    retrieved_at: str = ""
    retrieved_by_key_id: str = ""  # key REF only, never the value
    local_file_path: str = ""
    file_sha256: str = ""
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    fps: float | None = None
    times_used_in_project: int = 1
    times_used_on_channel: int = 1
    qc_status: str = "pass"
    ledger_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_csv_dict(self) -> dict:
        row = self.to_dict()
        row["sensitive_flags"] = json.dumps(self.sensitive_flags, sort_keys=True)
        return {k: row.get(k, "") for k in CSV_FIELDS}

    def is_complete(self) -> bool:
        """A COMPLETE licence record (required for manual-library assets)."""
        return bool(
            self.provider
            and self.provider_asset_id
            and self.asset_page_url
            and self.licence_name
            and self.licence_snapshot_date
            and self.licence_terms_hash
            and self.retrieved_at
            and self.file_sha256
        )


def build_row(
    *,
    project_id: str,
    scene_id: str,
    candidate: Candidate,
    licence: LicenceRecord,
    retrieved_by_key_id: str = "",
    local_file_path: str = "",
    file_sha256: str = "",
    width: int | None = None,
    height: int | None = None,
    duration_s: float | None = None,
    fps: float | None = None,
    times_used_in_project: int = 1,
    times_used_on_channel: int = 1,
    retrieved_at: datetime | None = None,
) -> LedgerRow:
    return LedgerRow(
        project_id=project_id,
        scene_id=scene_id,
        provider=candidate.provider,
        provider_asset_id=candidate.provider_asset_id,
        asset_page_url=candidate.page_url,
        download_url=candidate.download_url,
        author_name=candidate.author_name,
        author_url=candidate.author_url,
        licence_name=licence.name,
        licence_url=licence.url,
        licence_snapshot_date=licence.snapshot_date,
        licence_terms_hash=licence.terms_hash,
        commercial_use_allowed=licence.commercial_use_allowed,
        attribution_required=licence.attribution_required,
        standalone_use_prohibited=licence.standalone_use_prohibited,
        modification_allowed=licence.modification_allowed,
        sensitive_flags=candidate.sensitive_flags.to_dict(),
        retrieved_at=(retrieved_at or candidate.retrieved_at or utcnow()).isoformat(),
        retrieved_by_key_id=retrieved_by_key_id,
        local_file_path=local_file_path,
        file_sha256=file_sha256,
        width=width if width is not None else candidate.width,
        height=height if height is not None else candidate.height,
        duration_s=duration_s if duration_s is not None else candidate.duration_s,
        fps=fps if fps is not None else candidate.fps,
        times_used_in_project=times_used_in_project,
        times_used_on_channel=times_used_on_channel,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LicenceLedger(ABC):
    """Writer interface for the licence ledger."""

    @abstractmethod
    def write_row(self, row: LedgerRow) -> LedgerRow:
        ...

    @abstractmethod
    def rows(self, project_id: str | None = None) -> list[LedgerRow]:
        ...

    def record_download(
        self,
        *,
        project_id: str,
        scene_id: str,
        asset: DownloadedAsset,
        licence: LicenceRecord,
        retrieved_by_key_id: str = "",
    ) -> LedgerRow:
        row = build_row(
            project_id=project_id,
            scene_id=scene_id,
            candidate=asset.candidate,
            licence=licence,
            retrieved_by_key_id=retrieved_by_key_id,
            local_file_path=asset.local_path,
            file_sha256=asset.file_sha256,
            width=asset.width,
            height=asset.height,
            duration_s=asset.duration_s,
            fps=asset.fps,
        )
        written = self.write_row(row)
        asset.ledger_id = written.ledger_id
        return written

    def find_row(
        self,
        *,
        provider: str,
        provider_asset_id: str,
        project_id: str | None = None,
        file_sha256: str | None = None,
    ) -> LedgerRow | None:
        for row in self.rows(project_id=project_id):
            if row.provider == provider and row.provider_asset_id == provider_asset_id:
                if file_sha256 is not None and row.file_sha256 != file_sha256:
                    continue
                return row
        return None

    def assert_renderable(
        self,
        asset: DownloadedAsset | Candidate,
        *,
        project_id: str | None = None,
    ) -> LedgerRow:
        """Render gate: raise LicenceLedgerError if no ledger row exists."""
        if isinstance(asset, DownloadedAsset):
            candidate = asset.candidate
            sha = asset.file_sha256 or None
        else:
            candidate = asset
            sha = None
        row = self.find_row(
            provider=candidate.provider,
            provider_asset_id=candidate.provider_asset_id,
            project_id=project_id,
            file_sha256=sha,
        )
        if row is None:
            raise LicenceLedgerError(
                f"asset {candidate.asset_key} has no licence ledger row; "
                "no ledger row = asset cannot enter render"
            )
        if not row.commercial_use_allowed:
            raise LicenceLedgerError(
                f"asset {candidate.asset_key} ledger row does not allow commercial use"
            )
        return row

    def delete_row(self, ledger_id: str) -> None:  # pragma: no cover - guard
        raise LicenceLedgerError("ledger rows are never deleted")

    def export_csv(self, target: str | Path | TextIO) -> str:
        """Export rows in ``licence_ledger.csv`` format; returns CSV text."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in self.rows():
            writer.writerow(row.to_csv_dict())
        text = buffer.getvalue()
        if isinstance(target, (str, Path)):
            Path(target).write_text(text, encoding="utf-8")
        else:
            target.write(text)
        return text


def assert_renderable(
    ledger: LicenceLedger,
    asset: DownloadedAsset | Candidate,
    *,
    project_id: str | None = None,
) -> LedgerRow:
    """Module-level render gate (same rule as ``LicenceLedger.assert_renderable``)."""
    return ledger.assert_renderable(asset, project_id=project_id)


class InMemoryLicenceLedger(LicenceLedger):
    def __init__(self):
        self._rows: list[LedgerRow] = []

    def write_row(self, row: LedgerRow) -> LedgerRow:
        self._rows.append(row)
        return row

    def rows(self, project_id: str | None = None) -> list[LedgerRow]:
        if project_id is None:
            return list(self._rows)
        return [r for r in self._rows if r.project_id == project_id]


class JsonLicenceLedger(LicenceLedger):
    """JSON-file-backed ledger; rows survive restarts and are never deleted."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._rows: list[LedgerRow] = []
        if self.path.exists() and self.path.read_text(encoding="utf-8").strip():
            for raw in json.loads(self.path.read_text(encoding="utf-8")):
                raw.pop("sensitive_flags_obj", None)
                self._rows.append(LedgerRow(**raw))

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([r.to_dict() for r in self._rows], indent=2), encoding="utf-8"
        )

    def write_row(self, row: LedgerRow) -> LedgerRow:
        self._rows.append(row)
        self._flush()
        return row

    def rows(self, project_id: str | None = None) -> list[LedgerRow]:
        if project_id is None:
            return list(self._rows)
        return [r for r in self._rows if r.project_id == project_id]
