"""Write-only secret storage backed by the OS keyring.

Implements the secret-handling rules from roadmap section 14 (security):
API keys and OAuth refresh tokens live ONLY in the OS credential store
(Windows Credential Manager on the target platform, via ``keyring``) under
service name ``"YouTubeAgent"``.  SQLite stores nothing but an opaque
reference id plus display metadata (e.g. last-4) in the
``provider_credentials`` table — never the secret itself.

Rules enforced here:

* :func:`get_secret` is for INTERNAL backend use only (provider adapters
  minting calls).  Routers must NEVER return its result across the
  REST/WS boundary — the providers API is write-only for keys.
* Any keyring backend failure (notably headless Linux, where no Secret
  Service / KWallet is running) is surfaced as ``ApiError`` 503 with code
  ``credential_store_unavailable`` so the UI can show a plain-language
  "credential store unavailable" state instead of a stack trace.
"""

from __future__ import annotations

import sys
from typing import Optional

try:  # ApiError + envelope are owned by worker 2 (app.errors).
    from app.errors import ApiError
except Exception:  # pragma: no cover - fallback until app.errors lands

    class ApiError(Exception):  # type: ignore[no-redef]
        """Fallback ApiError with the same public shape as app.errors.ApiError."""

        def __init__(
            self,
            status_code: int = 500,
            code: str = "internal_error",
            message: str = "",
            details: Optional[dict] = None,
            retryable: bool = False,
        ) -> None:
            super().__init__(message or code)
            self.status_code = status_code
            self.code = code
            self.message = message or code
            self.details = details or {}
            self.retryable = retryable


try:
    import keyring  # type: ignore
except Exception:  # pragma: no cover - keyring not installed
    keyring = None  # type: ignore[assignment]

#: Credential-store service name (roadmap section 14: service "YouTubeAgent").
SERVICE = "YouTubeAgent"


def _unavailable(detail: str = "") -> ApiError:
    return ApiError(
        503,
        "credential_store_unavailable",
        "The OS credential store is unavailable on this machine.",
        details={"detail": detail} if detail else {},
        retryable=True,
    )


def _is_windows(platform: Optional[str] = None) -> bool:
    return (platform or sys.platform).startswith("win")


def _backend_class_name(kr) -> str:
    """``module.Class`` name of the keyring backend ``kr`` would use."""
    try:
        backend = kr.get_keyring()
    except Exception:
        return "unknown"
    if backend is None:
        return "none"
    cls = type(backend)
    return f"{cls.__module__}.{cls.__name__}"


def select_keyring_backend(kr=None, platform: Optional[str] = None) -> str:
    """Ensure the platform-appropriate keyring backend is selected.

    On Windows the store MUST be Windows Credential Manager, i.e.
    ``keyring.backends.Windows.WinVaultKeyring``. The ``keyring``
    library normally auto-selects it by priority, but a machine whose
    auto-detection fell back to the fail/null backend would silently
    have nowhere to store keys — so on Windows we explicitly install
    ``WinVaultKeyring`` whenever the current backend is not already it.

    On every other platform this is a no-op (the library's own
    selection — Secret Service / KWallet / Keychain — stands).

    Defensive by design: any failure (odd keyring builds, test fakes
    without ``get_keyring``) leaves the current selection untouched.
    Returns the ``module.Class`` name of the backend in effect.
    """
    if kr is None:
        kr = keyring
    if kr is None:
        return "unavailable"
    if _is_windows(platform):
        try:
            from keyring.backends.Windows import WinVaultKeyring

            current = kr.get_keyring()
            if not isinstance(current, WinVaultKeyring):
                kr.set_keyring(WinVaultKeyring())
        except Exception:
            pass  # never break secret access over backend selection
    return _backend_class_name(kr)


def credential_store_info(platform: Optional[str] = None) -> dict:
    """Describe the credential store selection (never any secret).

    ``windows_credential_manager`` is True only when running on
    Windows AND the selected backend really is the WinVault backend —
    it is reported honestly as False everywhere else.
    """
    if keyring is None:
        return {
            "platform": platform or sys.platform,
            "backend": "unavailable",
            "available": False,
            "windows_credential_manager": False,
        }
    backend = select_keyring_backend(keyring, platform)
    return {
        "platform": platform or sys.platform,
        "backend": backend,
        "available": not backend.endswith("fail.Keyring") and backend != "none",
        "windows_credential_manager": bool(
            _is_windows(platform) and "WinVaultKeyring" in backend
        ),
    }


def _backend():
    """Return the keyring module or raise 503 credential_store_unavailable."""
    if keyring is None:
        raise _unavailable("keyring package is not installed")
    # Selection seam: on Windows this pins Credential Manager (see
    # select_keyring_backend); elsewhere it is a defensive no-op.
    select_keyring_backend(keyring)
    return keyring


def set_secret(ref_id: str, value: str) -> None:
    """Store ``value`` under ``ref_id`` in the OS credential store.

    ``ref_id`` is the opaque reference kept in SQLite, e.g.
    ``"apikey/gemini"`` or ``"oauth/youtube/<channel_id>"``.
    """
    kr = _backend()
    try:
        kr.set_password(SERVICE, ref_id, value)
    except Exception as exc:  # keyring.errors.KeyringError and friends
        raise _unavailable(str(exc)) from exc


def get_secret(ref_id: str) -> Optional[str]:
    """Return the stored secret for ``ref_id`` (INTERNAL USE ONLY).

    .. warning::
        This value must never be returned by a router, logged, or placed
        in any agent's context.  Only provider adapters inside the
        backend may call this, just-in-time, to mint an external call.
    """
    kr = _backend()
    try:
        return kr.get_password(SERVICE, ref_id)
    except Exception as exc:
        raise _unavailable(str(exc)) from exc


def delete_secret(ref_id: str) -> bool:
    """Delete the secret for ``ref_id``.  Returns True if one existed.

    Deleting a reference that is not present is NOT an error (disconnect
    flows must be safely repeatable); a failing backend still raises 503.
    """
    kr = _backend()
    # Distinguish "nothing stored" from "backend broken": some backends
    # raise PasswordDeleteError for a missing entry, others return None
    # from get_password.
    try:
        existing = kr.get_password(SERVICE, ref_id)
    except Exception as exc:
        raise _unavailable(str(exc)) from exc
    if existing is None:
        return False
    try:
        kr.delete_password(SERVICE, ref_id)
    except Exception as exc:
        # PasswordDeleteError for an entry we just read is unexpected;
        # treat delete failures generically as an unavailable store.
        raise _unavailable(str(exc)) from exc
    return True


def has_secret(ref_id: str) -> bool:
    """True when a secret is stored under ``ref_id`` (never returns it)."""
    kr = _backend()
    try:
        return kr.get_password(SERVICE, ref_id) is not None
    except Exception as exc:
        raise _unavailable(str(exc)) from exc
