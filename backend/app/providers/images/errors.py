"""Error types for the image-generation layer."""

from __future__ import annotations


class ImageError(Exception):
    """Base class for all image-generation errors."""


class LicensingBlocked(ImageError):
    """Raised when a model is not on the allow-list for the requested use.

    This is the defence against the #1 licensing trap in this product:
    community ComfyUI workflows defaulting to FLUX.1 [dev] checkpoints,
    whose weights are NON-COMMERCIAL. A blocked model can only be used
    when the caller explicitly passes
    ``holds_bfl_commercial_licence=True`` (a per-channel attestation that
    the channel owner holds a BFL commercial licence).
    """


class PromptBlockedError(ImageError):
    """Raised when the prompt safety layer rejects a prompt before any
    provider call is made (real brand / celebrity / copyrighted character /
    real-person likeness)."""

    def __init__(self, message: str, violations: list[str] | None = None) -> None:
        super().__init__(message)
        self.violations: list[str] = violations or []


class ProviderUnavailable(ImageError):
    """A provider cannot serve the request right now (server down, queue
    saturated, missing API key, ...). The router treats this as the signal
    to overflow to the next provider."""


class ProviderError(ImageError):
    """A provider was reached but returned an error / failed generation."""


class DiegeticTextDisabled(ImageError):
    """Raised when the experimental diegetic text-in-image path is called
    while disabled (it is OFF by default — see flux_api module)."""
