"""Versioned calibration store — the planning-side reader of
``calibration_data.json`` (Round 4, Worker 2: profile persistence +
planner integration).

Worker 1 (:mod:`app.providers.tts.calibration`) MEASURES delivered WPM
with real edge-tts and writes the canonical file next to this module::

    {"version": 1, "measured_at": ISO-8601 str, "method": str,
     "voices": {"<voice_id>": {"rates_pct": {"-25": wpm, "-10": wpm,
                                              "0": wpm, "10": wpm},
                               "sample_text_id": str,
                               "language": "ur" | "en"}}}

This module is the only sanctioned way for planners to consume those
numbers.  Its contract:

* **Provenance is inseparable from the value.**  Every lookup returns a
  :class:`CalibratedWpm` carrying a :class:`CalibrationProvenance`
  (``measured_at`` / ``method`` / schema ``version`` / file path when
  the value is measured).  Callers log / persist the provenance next
  to the WPM they used — a bare float never leaves this module through
  the fallback path without its provenance attached.
* **Schema-version check.**  A file whose ``version`` is not
  :data:`SUPPORTED_CALIBRATION_VERSION` raises
  :class:`CalibrationError` with a clear message.  Malformed files
  (bad JSON, wrong shapes, non-positive WPM) raise the same error.
  Planning must never silently consume data it does not understand.
* **Explicit fallback.**  If the file is *missing* (e.g. Worker 1 has
  not measured yet, or the file was removed), lookups do NOT fail:
  :meth:`CalibrationStore.wpm_for` returns the caller-supplied
  fallback WPM with provenance source ``"unmeasured-default"`` — the
  fallback is always labelled, never dressed up as measured.
  :meth:`CalibrationStore.lookup` returns ``None`` in that case so
  callers who need to distinguish can.
* **The file may appear at any time.**  The store stats the file on
  every lookup and reloads when its (mtime, size) changes, so a
  calibration run that lands mid-process is picked up without a
  restart.

Rate selection: the contract measures the buckets ``-25, -10, 0, 10``
(percent).  A lookup for any other rate (e.g. a niche preset's
``-15%``) resolves to the *nearest measured bucket* (ties go to the
bucket closer to 0, then to the lower rate); the bucket actually used
is recorded on the result as ``rate_pct`` so the plan stays auditable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_CALIBRATION_PATH",
    "PROVENANCE_MEASURED",
    "PROVENANCE_UNMEASURED_DEFAULT",
    "SUPPORTED_CALIBRATION_VERSION",
    "CalibratedWpm",
    "CalibrationError",
    "CalibrationProvenance",
    "CalibrationStore",
    "get_calibration_store",
    "normalize_input_script",
    "reset_calibration_store",
    "resolve_planning_wpm",
]

logger = logging.getLogger(__name__)

#: The only ``calibration_data.json`` schema version this store reads.
#: Mirrors ``app.providers.tts.calibration.CALIBRATION_VERSION`` (the
#: team contract fixes it at 1); a mismatch is a hard, explicit error.
SUPPORTED_CALIBRATION_VERSION: int = 1

#: Provenance source labels.  ``measured`` = the value came from the
#: calibration file; ``unmeasured-default`` = the file was absent (or
#: the voice/rate unmeasured) and the caller's planning default was
#: used.  These strings are persisted on production artefacts, so they
#: are part of the audit contract — do not rename casually.
PROVENANCE_MEASURED: str = "measured"
PROVENANCE_UNMEASURED_DEFAULT: str = "unmeasured-default"

#: Canonical location written by Worker 1's measurement tooling.
DEFAULT_CALIBRATION_PATH: Path = Path(__file__).with_name("calibration_data.json")


class CalibrationError(ValueError):
    """Calibration data exists but cannot be trusted (bad version/shape)."""


@dataclass(frozen=True)
class CalibrationProvenance:
    """Where a planning WPM came from — attached wherever it is used."""

    #: PROVENANCE_MEASURED or PROVENANCE_UNMEASURED_DEFAULT.
    source: str
    #: Schema version of the calibration file (None when unmeasured).
    version: int | None = None
    #: ISO-8601 timestamp of the measurement run (None when unmeasured).
    measured_at: str | None = None
    #: Measurement method string from the file (None when unmeasured).
    method: str | None = None
    #: Absolute path of the calibration file (None when unmeasured and
    #: no path was configured — the default store always sets it).
    path: str | None = None

    @property
    def is_measured(self) -> bool:
        return self.source == PROVENANCE_MEASURED

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "measured_at": self.measured_at,
            "method": self.method,
            "path": self.path,
        }

    def label(self) -> str:
        """One-line human/audit label, e.g. for logs and prompt layers."""
        if self.is_measured:
            return (
                f"measured (calibration v{self.version}, "
                f"measured_at={self.measured_at}, method={self.method})"
            )
        return "unmeasured-default (no calibration data; planning constant)"


@dataclass(frozen=True)
class CalibratedWpm:
    """A WPM value plus the voice/rate it belongs to and its provenance."""

    voice_id: str
    #: The rate bucket the value was actually taken at (after nearest-
    #: bucket resolution), or the requested rate for a fallback value.
    rate_pct: float
    wpm: float
    provenance: CalibrationProvenance
    #: Sample text the voice was measured on (measured values only).
    sample_text_id: str | None = None
    #: Language tag from the calibration entry (measured values only).
    language: str | None = None
    #: TTS input script the value was measured for (R4-002):
    #: ``"roman"`` | ``"urdu_script"`` when the calibration entry is
    #: script-aware (or the script can be derived from its sample);
    #: the requested script for a fallback value; ``None`` when no
    #: script context exists at all.
    input_script: str | None = None

    @property
    def is_measured(self) -> bool:
        return self.provenance.is_measured

    def as_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "rate_pct": self.rate_pct,
            "wpm": self.wpm,
            "sample_text_id": self.sample_text_id,
            "language": self.language,
            "input_script": self.input_script,
            "provenance": self.provenance.as_dict(),
        }


def _parse_rate_key(key: Any, *, path: Path, voice: str) -> float:
    try:
        return float(str(key).strip())
    except (TypeError, ValueError):
        raise CalibrationError(
            f"calibration file {path}: voice {voice!r} has a non-numeric "
            f"rate key {key!r} (expected e.g. '-25', '0', '10')"
        ) from None


def _nearest_rate_key(rates: dict[float, float], requested: float) -> float:
    """Nearest measured bucket; ties prefer the bucket closer to 0,
    then the lower rate (deterministic, documented in the docstring)."""
    return min(rates, key=lambda r: (abs(r - requested), abs(r), r))


#: Known TTS input scripts (R4-002 script dimension).  Aliases map to
#: the canonical key used in the calibration file's ``scripts`` rows.
_INPUT_SCRIPT_ALIASES = {
    "roman": "roman",
    "roman_urdu": "roman",
    "latin": "roman",
    "urdu": "urdu_script",
    "urdu_script": "urdu_script",
}


def normalize_input_script(value: Any) -> str | None:
    """Canonical input-script key (``"roman"`` / ``"urdu_script"``),
    or ``None`` when ``value`` is empty/unrecognised."""
    if value is None:
        return None
    cleaned = str(value).strip().lower().replace("-", "_")
    return _INPUT_SCRIPT_ALIASES.get(cleaned)


def _script_for_sample(sample_text_id: Any) -> str | None:
    """Input script a legacy (script-blind) sample was measured in,
    derived from its sample text id; ``None`` when not derivable."""
    if not sample_text_id:
        return None
    text_id = str(sample_text_id).strip().lower()
    if "urdu_script" in text_id:
        return "urdu_script"
    if "roman" in text_id or "english" in text_id:
        return "roman"
    return None


class CalibrationStore:
    """Loader + lookup over one ``calibration_data.json`` file.

    The file is (re)read lazily on every lookup when its (mtime, size)
    signature changes, so the store handles the file appearing,
    being replaced by a fresh measurement run, or disappearing.
    """

    def __init__(self, path: str | Path | None = None):
        self._path: Path = (
            Path(path) if path is not None else DEFAULT_CALIBRATION_PATH
        )
        self._signature: tuple[int, int] | None = None
        self._data: dict[str, Any] | None = None
        #: Voice entries normalised to {voice: {"rates": {float: float},
        #: "sample_text_id": str|None, "language": str|None}}.
        self._voices: dict[str, dict[str, Any]] = {}

    # -- loading ------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def _stat_signature(self) -> tuple[int, int] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (int(st.st_mtime_ns), int(st.st_size))

    def _refresh(self) -> bool:
        """Load/reload if the file changed.  Returns True when measured
        data is available.  Raises :class:`CalibrationError` when the
        file exists but is unusable (bad version / malformed)."""
        signature = self._stat_signature()
        if signature is None:
            # Missing file -> explicit unmeasured state (not an error).
            if self._data is not None or self._signature is not None:
                logger.info(
                    "calibration file %s disappeared; planning falls back "
                    "to unmeasured defaults",
                    self._path,
                )
            self._data = None
            self._voices = {}
            self._signature = None
            return False
        if signature == self._signature and self._data is not None:
            return True
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CalibrationError(
                f"calibration file {self._path} could not be read: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalibrationError(
                f"calibration file {self._path} is not valid JSON: {exc}"
            ) from exc
        self._data = self._validate(data)
        self._signature = signature
        logger.info(
            "loaded TTS calibration v%s from %s (measured_at=%s, %d voices)",
            self._data["version"],
            self._path,
            self._data["measured_at"],
            len(self._voices),
        )
        return True

    def _validate(self, data: Any) -> dict[str, Any]:
        path = self._path
        if not isinstance(data, dict):
            raise CalibrationError(
                f"calibration file {path}: top level must be a JSON object, "
                f"got {type(data).__name__}"
            )
        version = data.get("version")
        if version != SUPPORTED_CALIBRATION_VERSION:
            raise CalibrationError(
                f"calibration file {path}: unsupported schema version "
                f"{version!r}; this build reads version "
                f"{SUPPORTED_CALIBRATION_VERSION} only — re-run the "
                f"calibration measurement or update the store"
            )
        measured_at = data.get("measured_at")
        if not isinstance(measured_at, str) or not measured_at.strip():
            raise CalibrationError(
                f"calibration file {path}: 'measured_at' must be a "
                f"non-empty ISO-8601 string, got {measured_at!r}"
            )
        method = data.get("method")
        if not isinstance(method, str) or not method.strip():
            raise CalibrationError(
                f"calibration file {path}: 'method' must be a non-empty "
                f"string, got {method!r}"
            )
        voices_raw = data.get("voices")
        if not isinstance(voices_raw, dict):
            raise CalibrationError(
                f"calibration file {path}: 'voices' must be an object "
                f"mapping voice id -> entry, got {type(voices_raw).__name__}"
            )
        voices: dict[str, dict[str, Any]] = {}
        for voice, entry in voices_raw.items():
            if not isinstance(entry, dict):
                raise CalibrationError(
                    f"calibration file {path}: voice {voice!r} entry must "
                    f"be an object, got {type(entry).__name__}"
                )
            rates = self._parse_rates(entry.get("rates_pct"), voice=str(voice))
            # Optional script dimension (R4-002): "scripts" maps an
            # input-script key ("roman" / "urdu_script") to its own
            # {"rates_pct": ..., "sample_text_id": ...} row.  Files
            # without it are legacy script-blind entries whose single
            # row is used for any script (its sample id says which).
            scripts: dict[str, dict[str, Any]] = {}
            scripts_raw = entry.get("scripts")
            if scripts_raw is not None:
                if not isinstance(scripts_raw, dict) or not scripts_raw:
                    raise CalibrationError(
                        f"calibration file {path}: voice {voice!r} has a "
                        f"malformed 'scripts' dimension "
                        f"({type(scripts_raw).__name__})"
                    )
                for script_key, script_entry in scripts_raw.items():
                    script = normalize_input_script(script_key)
                    if script is None or not isinstance(script_entry, dict):
                        raise CalibrationError(
                            f"calibration file {path}: voice {voice!r} has "
                            f"an unknown/malformed script row {script_key!r}"
                        )
                    scripts[script] = {
                        "rates": self._parse_rates(
                            script_entry.get("rates_pct"),
                            voice=f"{voice} [{script}]"),
                        "sample_text_id": script_entry.get("sample_text_id"),
                    }
            language = entry.get("language")
            voices[str(voice)] = {
                "rates": rates,
                "sample_text_id": entry.get("sample_text_id"),
                "language": str(language) if language else None,
                "scripts": scripts,
                "default_input_script": normalize_input_script(
                    entry.get("default_input_script")),
            }
        self._voices = voices
        return data

    def _parse_rates(self, rates_raw: Any, *, voice: str) -> dict[float, float]:
        """Validate one ``rates_pct`` mapping -> {rate_float: wpm}."""
        path = self._path
        if not isinstance(rates_raw, dict) or not rates_raw:
            raise CalibrationError(
                f"calibration file {path}: voice {voice!r} has no "
                f"'rates_pct' measurements"
            )
        rates: dict[float, float] = {}
        for key, value in rates_raw.items():
            rate = _parse_rate_key(key, path=path, voice=voice)
            try:
                wpm = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise CalibrationError(
                    f"calibration file {path}: voice {voice!r} rate "
                    f"{key!r} WPM is not a number: {value!r}"
                ) from None
            if wpm <= 0:
                raise CalibrationError(
                    f"calibration file {path}: voice {voice!r} rate "
                    f"{key!r} WPM must be positive, got {wpm}"
                )
            rates[rate] = wpm
        return rates

    # -- provenance -----------------------------------------------------

    def provenance(self) -> CalibrationProvenance:
        """Provenance of the currently loaded data (or the explicit
        unmeasured-default provenance when no file is available)."""
        if self._refresh():
            assert self._data is not None
            return CalibrationProvenance(
                source=PROVENANCE_MEASURED,
                version=int(self._data["version"]),
                measured_at=str(self._data["measured_at"]),
                method=str(self._data["method"]),
                path=str(self._path),
            )
        return CalibrationProvenance(
            source=PROVENANCE_UNMEASURED_DEFAULT,
            path=str(self._path),
        )

    def available_voices(self) -> list[str]:
        """Voice ids with measurements (empty when unmeasured)."""
        if not self._refresh():
            return []
        return sorted(self._voices)

    # -- lookup ---------------------------------------------------------

    def lookup(self, voice_id: str, rate_pct: float = 0.0,
               input_script: str | None = None) -> CalibratedWpm | None:
        """Measured WPM for ``voice_id`` at (nearest bucket to)
        ``rate_pct`` for the TTS input script ``input_script``
        (``"roman"`` / ``"urdu_script"``; R4-002).

        Script resolution: an explicit ``input_script`` wins; without
        one, the entry's ``default_input_script`` (the adopted
        default for that voice) applies.  On a script-aware entry an
        explicitly requested script with NO measured row returns
        ``None`` — a Roman row is never silently substituted for an
        Urdu-script production (that substitution was the Round 4
        run-1 +24% miss).  On a legacy script-blind entry (no
        ``scripts`` dimension) the single measured row is used for
        any script and ``input_script`` on the result is derived
        from its sample text id.  ``None`` also when no measurement
        exists for the voice (missing file included).  Raises
        :class:`CalibrationError` when the file exists but is
        unusable."""
        if not voice_id or not self._refresh():
            return None
        entry = self._voices.get(str(voice_id))
        if entry is None:
            return None
        scripts: dict[str, dict[str, Any]] = entry.get("scripts") or {}
        requested_script = normalize_input_script(input_script)
        rates: dict[float, float]
        sample_text_id = entry.get("sample_text_id")
        used_script: str | None
        if scripts:
            script = requested_script or entry.get("default_input_script")
            if script is not None:
                row = scripts.get(script)
                if row is None:
                    return None
                rates = row["rates"]
                sample_text_id = row.get("sample_text_id")
                used_script = script
            else:
                rates = entry["rates"]
                used_script = _script_for_sample(sample_text_id)
        else:
            rates = entry["rates"]
            used_script = (_script_for_sample(sample_text_id)
                           or requested_script)
        requested = float(rate_pct)
        bucket = requested if requested in rates else _nearest_rate_key(rates, requested)
        return CalibratedWpm(
            voice_id=str(voice_id),
            rate_pct=bucket,
            wpm=float(rates[bucket]),
            provenance=self.provenance(),
            sample_text_id=sample_text_id,
            language=entry.get("language"),
            input_script=used_script,
        )

    def wpm_for(
        self,
        voice_id: str,
        rate_pct: float = 0.0,
        *,
        fallback_wpm: float,
        input_script: str | None = None,
    ) -> CalibratedWpm:
        """The planning WPM for a voice+rate(+script): measured when
        available, otherwise ``fallback_wpm`` labelled
        ``unmeasured-default``.

        This is the planner-facing entry point — it never returns a
        bare number and never pretends a fallback was measured.
        """
        measured = self.lookup(voice_id, rate_pct, input_script=input_script)
        if measured is not None:
            return measured
        return CalibratedWpm(
            voice_id=str(voice_id or ""),
            rate_pct=float(rate_pct),
            wpm=float(fallback_wpm),
            provenance=CalibrationProvenance(
                source=PROVENANCE_UNMEASURED_DEFAULT,
                path=str(self._path),
            ),
            input_script=normalize_input_script(input_script),
        )


# ---------------------------------------------------------------------------
# Process-wide default store (the canonical file location)
# ---------------------------------------------------------------------------

_default_store: CalibrationStore | None = None


def get_calibration_store() -> CalibrationStore:
    """The shared store over :data:`DEFAULT_CALIBRATION_PATH`."""
    global _default_store
    if _default_store is None:
        _default_store = CalibrationStore()
    return _default_store


def reset_calibration_store() -> None:
    """Drop the shared store (tests / after replacing the file)."""
    global _default_store
    _default_store = None


def resolve_planning_wpm(
    voice_id: str,
    rate_pct: float = 0.0,
    *,
    fallback_wpm: float,
    store: CalibrationStore | None = None,
    input_script: str | None = None,
) -> CalibratedWpm:
    """Convenience wrapper over ``(store or default).wpm_for(...)``."""
    return (store or get_calibration_store()).wpm_for(
        voice_id, rate_pct, fallback_wpm=fallback_wpm,
        input_script=input_script,
    )
