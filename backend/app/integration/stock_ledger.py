"""Stock-asset licence-ledger rows (Round 3).

Bridges the stock-fetch layer (:class:`StockSceneAsset`-shaped objects
produced when a scene is sourced from a real stock provider) and the two
Round 2 consumers of licence data:

1.  The pack licence ledger — :class:`app.integration.ledger.LicenceLedger`
    writes ``licence_ledger.csv`` with the fixed header
    ``asset_id,scene_id,source,licence,kind,local_path,notes``
    (:data:`app.integration.ledger.CSV_COLUMNS`).  A row produced here
    carries exactly those seven fields, with those exact key names and
    semantics: ``source`` is the provider (``"pixabay"``), ``asset_id``
    is the provider's own asset id, ``licence`` is the licence name
    (``"Pixabay Content License"``), ``kind`` is ``"video"`` /
    ``"photo"`` and ``local_path`` is the downloaded file.

2.  The Gate B copyright join — ``publish_stage.finalize_publish``
    collects per-scene rows (``assemble.licence_segments``) and
    ``publish_stage._build_copyright_inputs`` translates them into the
    ``pipeline.gates.copyright`` records.  That translation reads a
    DIFFERENT vocabulary than the CSV header: ``provider`` (not
    ``source``), ``licence_id`` / ``licence_ref`` (not ``licence``),
    ``content_hash`` (not a notes field) and ``start_s`` / ``end_s``
    timeline fields the CSV does not have at all.  A row carrying ONLY
    the seven CSV fields therefore cannot pass Gate G1/G2 through
    ``finalize_publish``: its licence id resolves to ``None``
    (``LICENCE_ID_MISSING``), its provider to ``None``
    (``PROVENANCE_*``) and it contributes no timeline coverage
    (``G1_TIMELINE_COVERAGE_GAP``).

So — exactly like the Round 2 visuals stage, whose ``ledger_rows``
already carry ``licence`` AND ``licence_id`` AND ``content_hash`` side
by side — the dict returned here is a superset: the seven CSV fields
verbatim, plus the gate-join aliases (``provider``,
``provider_asset_id``, ``licence_id``, ``licence_ref``,
``licence_name``, ``content_hash``, ``start_s``, ``end_s``, ``tier``,
review/commercial flags) so the SAME row object can be written to the
CSV via ``LicenceLedger.add(...)`` and handed to the publish stage as a
``licence_segments`` entry without re-shaping.  ``LicenceLedger``
itself still only ever writes the seven CSV columns.

The ``notes`` field is the YPP-appeal evidence string: it embeds, in
labelled ``key=value`` form, everything needed to prove the licence
later without the original API session — the asset page URL, the
download URL, the hash of the licence terms text captured at download
time (``terms_hash``) and the downloaded file's SHA-256
(``file_sha256``).

Honesty rule (inherited from ``ledger.py``): rows are built only from
asset objects the caller actually downloaded — this module never
invents an asset, a URL or a hash; missing inputs stay empty strings /
``None`` rather than being fabricated.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "PIXABAY_LICENCE_NAME",
    "DEFAULT_STOCK_PROVIDER",
    "ledger_row_for_stock_asset",
    "summarise_sources",
]

#: Licence name recorded for every Pixabay asset (the provider's own
#: name for the licence its content is distributed under).
PIXABAY_LICENCE_NAME = "Pixabay Content License"

#: Provider label used when an asset does not name one (this module's
#: rows are for the Pixabay stock path).
DEFAULT_STOCK_PROVIDER = "pixabay"

#: Providers the publish stage tiers as Tier-2 (conditional paid);
#: everything else this pipeline sources stock from is Tier-1.  Mirrors
#: ``publish_stage._PROVIDER_TIERS`` — kept local so this module does
#: not import the publish stage.
_TIER2_PROVIDERS = frozenset({"manual_upload", "licensed_stock"})

#: Kind normalisation onto the two stock kinds the ledger uses.
_KIND_ALIASES = {
    "video": "video",
    "clip": "video",
    "movie": "video",
    "footage": "video",
    "photo": "photo",
    "image": "photo",
    "picture": "photo",
    "still": "photo",
    "img": "photo",
}


# ---------------------------------------------------------------------------
# Defensive accessors (assets/scenes arrive as dataclasses, pydantic
# models, plain objects or dicts depending on the producing worker)
# ---------------------------------------------------------------------------


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """First non-empty attribute/key among ``names``."""
    for name in names:
        if obj is None:
            break
        if isinstance(obj, Mapping):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None and value != "":
            return value
    return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_kind(value: Any) -> str:
    raw = _text(value).lower()
    if not raw:
        return "video"
    return _KIND_ALIASES.get(raw, raw)


def _scene_fields(
    scene: Any, asset: Any
) -> tuple[str, Optional[float], Optional[float], Optional[float]]:
    """(scene_id, start_s, end_s, duration_s) from scene, then asset."""
    if isinstance(scene, str):
        scene_id = scene.strip()
        start_s = _float_or_none(_get(asset, "start_s", "start"))
        end_s = _float_or_none(_get(asset, "end_s", "end"))
    else:
        scene_id = _text(_get(scene, "scene_id", "id"))
        if not scene_id:
            scene_id = _text(_get(asset, "scene_id"))
        # NB: 0.0 is a legitimate start_s — never chain these with `or`.
        start_raw = _get(scene, "start_s", "start")
        if start_raw is None:
            start_raw = _get(asset, "start_s", "start")
        end_raw = _get(scene, "end_s", "end")
        if end_raw is None:
            end_raw = _get(asset, "end_s", "end")
        start_s = _float_or_none(start_raw)
        end_s = _float_or_none(end_raw)
    duration_s = _float_or_none(
        _get(scene, "duration_s", "duration") if not isinstance(scene, str) else None
    )
    if duration_s is None:
        duration_s = _float_or_none(_get(asset, "duration_s", "duration"))
    if duration_s is None and start_s is not None and end_s is not None:
        duration_s = round(end_s - start_s, 3)
    return scene_id, start_s, end_s, duration_s


# ---------------------------------------------------------------------------
# Ledger row for one downloaded stock asset
# ---------------------------------------------------------------------------


def ledger_row_for_stock_asset(asset: Any, scene: Any = None) -> dict:
    """Build the licence-ledger row for one downloaded stock asset.

    ``asset`` is a ``stock_fetch.StockSceneAsset``-shaped object (or a
    dict / stand-in with the same fields): ``local_path``, ``provider``,
    ``provider_asset_id``, ``asset_page_url``, ``download_url``,
    ``licence_name``, ``terms_hash``, ``file_sha256``, ``width``,
    ``height``, ``duration_s``, ``kind``.  ``scene`` is the scene the
    asset was fetched for — a scene record / dict carrying ``scene_id``
    and (for the Gate B timeline join) ``start_s`` / ``end_s``, a plain
    scene-id string, or ``None``.

    Returns a dict whose first seven keys are EXACTLY the
    ``LicenceLedger`` CSV columns (``asset_id``, ``scene_id``,
    ``source``, ``licence``, ``kind``, ``local_path``, ``notes``),
    followed by the gate-join aliases the publish stage reads (see the
    module docstring for why both vocabularies are present).
    """
    provider = (
        _text(_get(asset, "provider", "source_provider")) or DEFAULT_STOCK_PROVIDER
    ).lower()
    provider_asset_id = _text(
        _get(asset, "provider_asset_id", "provider_id", "asset_id", "id")
    )
    local_path = _text(
        _get(asset, "local_path", "asset_path", "path", "file_path")
    )
    licence_name = (
        _text(_get(asset, "licence_name", "licence", "license_name", "license"))
        or PIXABAY_LICENCE_NAME
    )
    page_url = _text(
        _get(asset, "asset_page_url", "page_url", "source_url", "url")
    )
    download_url = _text(_get(asset, "download_url", "download", "file_url"))
    terms_hash = _text(
        _get(asset, "terms_hash", "terms_sha256", "licence_terms_hash")
    )
    file_sha256 = _text(
        _get(asset, "file_sha256", "sha256", "content_hash", "asset_hash")
    )
    kind = _normalise_kind(_get(asset, "kind", "asset_kind", "media_type"))

    scene_id, start_s, end_s, duration_s = _scene_fields(scene, asset)

    # The ledger's asset key IS the provider's own asset id: it is the
    # one identifier that resolves both in the provider's catalogue
    # (for a YPP appeal) and as the G1/G2 join key downstream.
    asset_id = provider_asset_id or scene_id or "unknown-stock-asset"
    licence_id = (
        f"{provider}-{provider_asset_id}" if provider_asset_id else licence_name
    )

    # Appeal evidence: every provenance fact, labelled, in one string.
    notes_parts = []
    if provider_asset_id:
        notes_parts.append(f"provider_asset_id={provider_asset_id}")
    if page_url:
        notes_parts.append(f"asset_page_url={page_url}")
    if download_url:
        notes_parts.append(f"download_url={download_url}")
    if terms_hash:
        notes_parts.append(f"terms_hash={terms_hash}")
    if file_sha256:
        notes_parts.append(f"file_sha256={file_sha256}")
    notes_parts.append(f"licence={licence_name}")
    notes = "; ".join(notes_parts)

    width = _get(asset, "width", "width_px")
    height = _get(asset, "height", "height_px")

    return {
        # -- the LicenceLedger CSV columns, exactly -------------------
        "asset_id": asset_id,
        "scene_id": scene_id,
        "source": provider,
        "licence": licence_name,
        "kind": kind,
        "local_path": local_path,
        "notes": notes,
        # -- Gate B join aliases (publish_stage._build_copyright_inputs)
        "provider": provider,
        "provider_asset_id": provider_asset_id,
        "licence_id": licence_id,
        "licence_ref": licence_id,
        "licence_name": licence_name,
        "content_hash": file_sha256,
        "file_sha256": file_sha256,
        "terms_hash": terms_hash,
        "asset_page_url": page_url,
        "download_url": download_url,
        "asset_path": local_path,
        "start_s": start_s,
        "end_s": end_s,
        "duration_s": duration_s,
        "role": "b_roll",
        "tier": 2 if provider in _TIER2_PROVIDERS else 1,
        "commercial_use_ok": True,
        "monetized_youtube_ok": True,
        "attribution_required": False,
        "review_status": "approved",
        "width": width,
        "height": height,
    }


# ---------------------------------------------------------------------------
# Production-report source summary
# ---------------------------------------------------------------------------

_STOCK_PROVIDER_NAMES = frozenset(
    {"pixabay", "pexels", "stock", "licensed_stock", "shutterstock", "getty"}
)
_PLATE_MARKERS = ("plate", "generated", "pillow", "own/", "own-", "local")


def _entry_asset_scene(entry: Any) -> tuple[Any, Any, bool]:
    """(asset, scene, had_asset) for pair / wrapper-dict entries."""
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        return entry[0], entry[1], True
    if isinstance(entry, Mapping):
        asset = entry.get("asset") or entry.get("stock_asset")
        if asset is not None:
            return asset, entry.get("scene"), True
    return None, None, False


def _classify_entry(entry: Any) -> str:
    """Classify one per-scene source entry: "stock" | "plate" | "other"."""
    asset, _scene, had_asset = _entry_asset_scene(entry)
    if had_asset:
        provider = _text(_get(asset, "provider", "source_provider")).lower()
        if _text(_get(asset, "provider_asset_id", "asset_id")) or (
            provider in _STOCK_PROVIDER_NAMES
        ):
            return "stock"
        source_text = _text(_get(asset, "source")).lower()
        if any(marker in source_text for marker in _PLATE_MARKERS):
            return "plate"
        return "stock" if provider else "other"

    if isinstance(entry, str):
        label = entry.strip().lower()
        if label in _STOCK_PROVIDER_NAMES or "stock" in label:
            return "stock"
        if any(marker in label for marker in _PLATE_MARKERS):
            return "plate"
        return "other"

    # Dict / object scene record or ledger row: read its source fields.
    provider = _text(
        _get(entry, "provider", "source_provider", "source")
    ).lower()
    if _text(_get(entry, "provider_asset_id")):
        return "stock"
    if provider in _STOCK_PROVIDER_NAMES or "stock" in provider:
        return "stock"
    chosen = _text(
        _get(entry, "chosen_source", "decision_source", "generation", "source")
    ).lower()
    haystack = f"{provider} {chosen}"
    if any(marker in haystack for marker in _PLATE_MARKERS):
        return "plate"
    if _text(_get(entry, "download_url", "asset_page_url")):
        return "stock"
    return "other"


def _ledger_row_for_entry(entry: Any) -> Optional[dict]:
    """Ledger row for one stock entry, or None when it carries no
    asset payload (a bare label cannot honestly produce a row)."""
    asset, scene, had_asset = _entry_asset_scene(entry)
    if had_asset:
        return ledger_row_for_stock_asset(asset, scene)
    if isinstance(entry, Mapping) and {
        "asset_id",
        "scene_id",
        "source",
        "licence",
    } <= set(entry):
        # Already a ledger-format row: pass it through unchanged.
        return dict(entry)
    return None


def summarise_sources(per_scene_sources: Sequence[Any]) -> dict:
    """Summarise per-scene sourcing for the production report.

    ``per_scene_sources`` is one entry per scene, in any of the shapes
    the stages produce: a ``(asset, scene)`` pair, a
    ``{"asset": ..., "scene": ...}`` wrapper, a scene/ledger record
    (dict or object) with source fields, or a plain source label
    string (``"pixabay"``, ``"local_plate"``, ...).

    Returns ``{"stock_count", "plate_count", "stock_share",
    "ledger_rows"}`` where ``stock_share`` is
    ``stock_count / (stock_count + plate_count)`` (``0.0`` when neither
    is present) and ``ledger_rows`` holds one
    :func:`ledger_row_for_stock_asset` row per stock entry that
    carried an actual asset (entries that are already ledger rows pass
    through; bare labels contribute counts only — no row is fabricated
    for an asset this run did not download).
    """
    entries = list(per_scene_sources or [])
    stock_count = 0
    plate_count = 0
    ledger_rows: list[dict] = []
    for entry in entries:
        kind = _classify_entry(entry)
        if kind == "stock":
            stock_count += 1
            row = _ledger_row_for_entry(entry)
            if row is not None:
                ledger_rows.append(row)
        elif kind == "plate":
            plate_count += 1
    classified = stock_count + plate_count
    stock_share = (stock_count / classified) if classified else 0.0
    return {
        "stock_count": stock_count,
        "plate_count": plate_count,
        "stock_share": stock_share,
        "ledger_rows": ledger_rows,
    }
