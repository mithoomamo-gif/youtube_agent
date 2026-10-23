"""Licence mappers: provider terms -> common ledger vocabulary (§24).

Terms change without notice, so every record carries a snapshot date and a
hash of the canonical terms text that applied at retrieval time.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class LicenceRecord:
    licence_id: str
    name: str
    url: str
    commercial_use_allowed: bool
    attribution_required: bool
    standalone_use_prohibited: bool
    modification_allowed: bool
    terms_text: str = ""
    snapshot_date: str = ""

    @property
    def terms_hash(self) -> str:
        payload = self.terms_text or f"{self.name}|{self.url}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def is_complete(self) -> bool:
        return bool(
            self.licence_id
            and self.name
            and self.url
            and self.snapshot_date
            and self.commercial_use_allowed is not None
            and self.attribution_required is not None
            and self.standalone_use_prohibited is not None
            and self.modification_allowed is not None
        )


def _today() -> str:
    return date.today().isoformat()


_PIXABAY_TERMS = (
    "Pixabay Content License: free commercial use, no attribution required; "
    "no standalone redistribution of unaltered files, no competing stock "
    "service, no trademark/logo use, no sale of unaltered copies."
)
_PEXELS_TERMS = (
    "Pexels License: free commercial use, attribution not required; "
    "prohibited: selling unaltered copies, redistribution on other stock/"
    "wallpaper platforms, implying endorsement by depicted people/brands, "
    "use as trademark, identifiable people in offensive/bad light."
)
_GENERATED_TERMS = (
    "AI-generated asset produced by the app's image worker for this project; "
    "flagged GENERATED in the ledger; YouTube synthetic-content rules apply."
)


def pixabay_licence(snapshot_date: str | None = None) -> LicenceRecord:
    return LicenceRecord(
        licence_id="pixabay-content-license",
        name="Pixabay Content License",
        url="https://pixabay.com/service/license-summary/",
        commercial_use_allowed=True,
        attribution_required=False,
        standalone_use_prohibited=True,
        modification_allowed=True,
        terms_text=_PIXABAY_TERMS,
        snapshot_date=snapshot_date or _today(),
    )


def pexels_licence(snapshot_date: str | None = None) -> LicenceRecord:
    return LicenceRecord(
        licence_id="pexels-license",
        name="Pexels License",
        url="https://www.pexels.com/license/",
        commercial_use_allowed=True,
        attribution_required=False,
        standalone_use_prohibited=True,
        modification_allowed=True,
        terms_text=_PEXELS_TERMS,
        snapshot_date=snapshot_date or _today(),
    )


def generated_licence(snapshot_date: str | None = None) -> LicenceRecord:
    return LicenceRecord(
        licence_id="generated",
        name="Generated (AI image)",
        url="",
        commercial_use_allowed=True,
        attribution_required=False,
        standalone_use_prohibited=True,
        modification_allowed=True,
        terms_text=_GENERATED_TERMS,
        snapshot_date=snapshot_date or _today(),
    )


LICENCE_MAPPERS = {
    "pixabay": pixabay_licence,
    "pexels": pexels_licence,
    "generated": generated_licence,
}


def licence_for_provider(provider: str, snapshot_date: str | None = None) -> LicenceRecord:
    mapper = LICENCE_MAPPERS.get(provider)
    if mapper is None:
        return LicenceRecord(
            licence_id=f"{provider}-unknown",
            name=f"{provider} (unmapped licence)",
            url="",
            commercial_use_allowed=False,
            attribution_required=True,
            standalone_use_prohibited=True,
            modification_allowed=False,
            terms_text="",
            snapshot_date=snapshot_date or _today(),
        )
    return mapper(snapshot_date)
