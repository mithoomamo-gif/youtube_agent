"""Licence ledger for one integration production run.

Every asset that ends up in a produced video gets exactly one row here,
keyed by ``asset_id`` + ``scene_id``. The ledger is written into the
project pack as ``licence_ledger.csv`` and is the audit trail that lets
the Copyright gate (and the user) answer "where did this come from and
under what licence may we use it?" for every second of the video.

Honesty rule (enforced by the callers, documented here because this is
the file of record): a row may only be added for an asset that was
actually created or acquired by this run. Generated assets use
``source`` values in the ``"own/generated"`` style (e.g.
``"own/generated: edge-tts voice"``, ``"own/generated: PIL plate"``);
external assets must name their real source. Never fabricate a row for
an external asset that was not actually used.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

__all__ = [
    "CSV_COLUMNS",
    "LicenceLedger",
    "LicenceRow",
]

#: CSV columns, exactly and in order — the pack contract.
CSV_COLUMNS: tuple[str, ...] = (
    "asset_id",
    "scene_id",
    "source",
    "licence",
    "kind",
    "local_path",
    "notes",
)


@dataclass
class LicenceRow:
    """One ledger row: one asset used in one scene (or the whole video).

    ``scene_id`` is ``""`` / ``"all"`` for whole-video assets such as the
    voiceover track, background music and thumbnails — those are still
    generated assets and still get a row.
    """

    asset_id: str
    scene_id: str
    source: str
    #: Licence name, e.g. "own work" / "Pixabay Content License".
    #: (Written to the CSV ``licence`` column.)
    licence: str
    #: Asset kind, e.g. "voice" / "music" / "image" / "video" / "thumbnail".
    kind: str
    local_path: str
    notes: str = ""

    @property
    def licence_name(self) -> str:
        """Alias for :attr:`licence` (the name ``add()`` takes)."""
        return self.licence

    def to_csv_dict(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "scene_id": self.scene_id,
            "source": self.source,
            "licence": self.licence,
            "kind": self.kind,
            "local_path": self.local_path,
            "notes": self.notes,
        }


@dataclass
class LicenceLedger:
    """In-memory licence ledger for one project; writes the pack CSV."""

    _rows: list[LicenceRow] = field(default_factory=list)

    def add(
        self,
        asset_id: str,
        scene_id: str,
        source: str,
        licence_name: str,
        kind: str,
        local_path: str | Path,
        notes: str = "",
    ) -> LicenceRow:
        """Record one actually-used asset and return its row.

        ``local_path`` may be a :class:`~pathlib.Path`; it is stored as
        a string so the CSV is identical on every platform.
        """
        row = LicenceRow(
            asset_id=str(asset_id),
            scene_id=str(scene_id),
            source=str(source),
            licence=str(licence_name),
            kind=str(kind),
            local_path=str(local_path),
            notes=str(notes),
        )
        self._rows.append(row)
        return row

    @property
    def rows(self) -> list[LicenceRow]:
        """All rows, in insertion order (a copy — mutate via ``add``)."""
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self) -> Iterator[LicenceRow]:
        return iter(self._rows)

    def write_csv(self, path: str | Path) -> Path:
        """Write the ledger to ``path`` and return the path written.

        The header is exactly :data:`CSV_COLUMNS`. Parent directories
        are created; an empty ledger still writes a valid header-only
        CSV so the pack always contains the file.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            for row in self._rows:
                writer.writerow(row.to_csv_dict())
        return target
