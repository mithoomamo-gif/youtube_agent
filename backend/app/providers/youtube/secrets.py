"""Secret-store contract for YouTube credentials.

OAuth refresh tokens and the OAuth client secret are handled ONLY through
the :class:`SecretStore` interface. The production implementation is the
Windows Credential Manager / DPAPI store (another team's job); this module
defines the protocol plus an in-memory implementation for tests and dev.

Rules enforced across the whole package:

- Token *values* are never logged and never returned to the frontend/UI.
  The UI only ever sees status strings (connected / expired /
  needs_reauth) via :class:`~.tokens.CredentialStatus`.
- Blobs are stored as opaque strings under caller-chosen refs, e.g.
  ``youtube:channel:<channel_id>``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretStore(Protocol):
    """Minimal key/value secret store (values are opaque strings)."""

    def get(self, ref: str) -> str | None:
        """Return the secret stored under ``ref``, or ``None``."""
        ...

    def put(self, ref: str, value: str) -> None:
        """Store ``value`` under ``ref``, replacing any previous value."""
        ...

    def delete(self, ref: str) -> None:
        """Remove ``ref`` if present (no error when absent)."""
        ...


class InMemorySecretStore:
    """Dict-backed :class:`SecretStore` for tests and local dev only."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self._data.get(ref)

    def put(self, ref: str, value: str) -> None:
        self._data[ref] = value

    def delete(self, ref: str) -> None:
        self._data.pop(ref, None)

    def __contains__(self, ref: str) -> bool:
        return ref in self._data
