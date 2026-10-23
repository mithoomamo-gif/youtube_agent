"""Transcription provider layer (faster-whisper).

Public surface: the factory that wires a REAL faster-whisper model
into the subtitles pipeline's ``WhisperTranscriber`` through its
``model_loader`` seam, plus the hand-rolled WER metric used to gate
transcription quality.  See :mod:`.whisper_faster` for why the wiring
lives here and not in the pipeline package.
"""
from .wer import normalize_words, word_error_rate
from .whisper_faster import (
    DEFAULT_COMPUTE_TYPE,
    DEFAULT_DEVICE,
    DEFAULT_MODEL_SIZE,
    MODEL_DIR_ENV_VAR,
    create_transcriber,
    default_model_dir,
    load_faster_whisper_model,
    make_model_loader,
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
    "normalize_words",
    "word_error_rate",
]
