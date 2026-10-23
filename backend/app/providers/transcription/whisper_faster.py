"""faster-whisper provider — the REAL transcription backend (Round 2).

This module is the provider-side wiring for the subtitles pipeline's
:class:`~app.pipeline.subtitles.whisper_wrapper.WhisperTranscriber`.

Why this lives in ``providers/`` and touches NO pipeline file
-------------------------------------------------------------
``WhisperTranscriber`` already exposes the established injection seam:
its ``model_loader`` constructor argument (``() -> model`` callable).
The pipeline wrapper's own internal ``_load_model()`` constructs
``WhisperModel(model_size, device=..., compute_type=...)`` *without* a
``download_root``, so model weights would land in the default
Hugging Face cache.  Supplying ``model_loader`` from the provider side
is therefore not just the clean seam — it is the only way to control
*where* the model is downloaded to without editing pipeline code.

Disk rule (Round 2 constraint): ``/tmp`` on the build VM is a small,
full tmpfs.  Model weights MUST live on the workspace disk.  The
default download root is therefore ``<repo>/models/whisper/``
(overridable via ``YTA_WHISPER_MODEL_DIR``), and callers should also
point ``HF_HOME`` / ``TMPDIR`` at the workspace for their processes —
the factory below already pins ``download_root`` explicitly, which is
the path faster-whisper actually uses for the weights.

Round 6 data-dir contract: when ``YTA_DATA_DIR`` is explicitly set
(packaged/frozen runs, whose install tree may be read-only), the
default download root moves under the data root instead:
``<data_dir>/models/whisper``.  Dev behaviour (override unset) is
unchanged.

Defaults are deliberately conservative for integration machines:
``tiny`` model, CPU, ``int8`` compute — fast to download (~75 MB),
fast to run, and accurate enough on clean TTS speech to gate on WER.
Production callers can pass larger models (``base`` / ``medium`` /
``large-v3-turbo``) and GPU settings through the same factory.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

#: Integration default: smallest real model. Production may override.
DEFAULT_MODEL_SIZE = "tiny"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"

#: Env var that overrides the model download/cache directory.
MODEL_DIR_ENV_VAR = "YTA_WHISPER_MODEL_DIR"


def default_model_dir() -> Path:
    """Directory faster-whisper weights are downloaded into.

    Resolution order:

    1. ``$YTA_WHISPER_MODEL_DIR``
    2. ``$YTA_DATA_DIR/models/whisper`` when the data-dir override is
       set (Round 6 contract — see the module docstring);
    3. ``<repo-root>/models/whisper/`` — repo root is four parents up
       from this file (``backend/app/providers/transcription/``),
       i.e. the workspace checkout, never ``/tmp``.
    """
    override = (os.environ.get(MODEL_DIR_ENV_VAR) or "").strip()
    if override:
        return Path(override).expanduser()
    from app.data_dir import data_dir_layout, explicit_data_dir

    explicit = explicit_data_dir()
    if explicit is not None:
        return data_dir_layout(explicit).whisper_model_dir
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "models" / "whisper"


def load_faster_whisper_model(
    model_size: str = DEFAULT_MODEL_SIZE,
    *,
    download_root: str | Path | None = None,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
) -> Any:
    """Construct a real ``faster_whisper.WhisperModel`` (lazy import).

    ``download_root`` is pinned to :func:`default_model_dir` unless the
    caller passes one explicitly, so weights never fall back to a
    tmpfs-backed default cache.
    """
    from faster_whisper import WhisperModel  # lazy: heavy + optional dep

    root = Path(download_root) if download_root is not None else default_model_dir()
    root.mkdir(parents=True, exist_ok=True)
    return WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
        download_root=str(root),
    )


def make_model_loader(
    model_size: str = DEFAULT_MODEL_SIZE,
    *,
    download_root: str | Path | None = None,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
) -> Callable[[], Any]:
    """Return the ``model_loader`` callable for ``WhisperTranscriber``."""

    def _loader() -> Any:
        return load_faster_whisper_model(
            model_size,
            download_root=download_root,
            device=device,
            compute_type=compute_type,
        )

    return _loader


def create_transcriber(
    model_size: str = DEFAULT_MODEL_SIZE,
    *,
    download_root: str | Path | None = None,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
    **transcriber_kwargs: Any,
):
    """Build a pipeline ``WhisperTranscriber`` wired to the REAL model.

    ``stub_mode=False`` is forced unless the caller explicitly passes a
    ``stub_mode`` of their own: the real path is attempted first, and
    the pipeline wrapper's documented behaviour still falls back to
    the alignment stub (with ``last_error`` recorded) if the model
    genuinely cannot load — it just never *starts* in stub mode.
    """
    # Imported here, not at module top, so importing this provider
    # package stays cheap and side-effect free, mirroring the lazy
    # import discipline of the pipeline wrapper itself.
    from app.pipeline.subtitles.whisper_wrapper import WhisperTranscriber

    transcriber_kwargs.setdefault("stub_mode", False)
    return WhisperTranscriber(
        model_size=model_size,
        device=device,
        compute_type=compute_type,
        model_loader=make_model_loader(
            model_size,
            download_root=download_root,
            device=device,
            compute_type=compute_type,
        ),
        **transcriber_kwargs,
    )


__all__ = [
    "DEFAULT_COMPUTE_TYPE",
    "DEFAULT_DEVICE",
    "DEFAULT_MODEL_SIZE",
    "MODEL_DIR_ENV_VAR",
    "create_transcriber",
    "default_model_dir",
    "load_faster_whisper_model",
    "make_model_loader",
]
