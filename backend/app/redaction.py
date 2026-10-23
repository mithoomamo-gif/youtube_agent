"""Single redaction choke point for the backend.

Every log line, error message and WS frame that might carry a secret must
pass through here (see ``logging_setup`` — :class:`RedactionFilter` is
attached to every handler). Redaction happens at write/emit time, never at
read time (roadmap §31).

Covered patterns:

* ``Authorization: Bearer <token>`` values
* ``x-api-key`` / ``x-local-token`` header values
* Google API keys (``AIza…``)
* OpenAI-style keys (``sk-…``, incl. ``sk-proj-…`` / ``sk-ant-…``)
* ``refresh_token`` / ``access_token`` / ``api_key`` / ``client_secret`` /
  ``password`` values in JSON-ish or ``key=value`` text
* the per-launch token (``YTA_TOKEN``) — both as an env value picked up
  automatically and via :func:`register_secret` for explicitly known values
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

REDACTED = "***"

# Exact secret values registered at runtime (e.g. the launch token once
# settings are loaded). Values shorter than this are never treated as exact
# secrets — replacing tiny strings would mangle ordinary text.
_MIN_EXACT_SECRET_LEN = 8
_extra_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register an exact secret value so :func:`redact_text` scrubs it."""
    if value and len(value) >= _MIN_EXACT_SECRET_LEN:
        _extra_secrets.add(value)


def _exact_secrets() -> list[str]:
    secrets = set(_extra_secrets)
    env_token = os.environ.get("YTA_TOKEN")
    if env_token and len(env_token) >= _MIN_EXACT_SECRET_LEN:
        secrets.add(env_token)
    # Longest first so overlapping values are scrubbed completely.
    return sorted(secrets, key=len, reverse=True)


# (pattern, replacement) — applied in order. Group 1, when present, is the
# label/prefix that is kept; the secret itself is replaced with REDACTED.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization: Bearer <token>
    (
        re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/=]+"),
        rf"\1{REDACTED}",
    ),
    # Header-style secrets: "x-api-key: …", "X-Local-Token: …"
    (
        re.compile(r"(?i)\b((?:x-api-key|x-local-token)\s*[:=]\s*)[^\s,;\"']+"),
        rf"\1{REDACTED}",
    ),
    # Key=value / JSON-ish fields carrying secrets.
    (
        re.compile(
            r"(?i)\b((?:refresh_token|access_token|id_token|api[_-]?key|"
            r"client_secret|launch[_-]?token|password)\s*\"?\s*[:=]\s*\"?)"
            r"[^\s,\"'}]+"
        ),
        rf"\1{REDACTED}",
    ),
    # Google API keys: AIza + 35 url-safe chars (accept >=10 to be safe).
    (re.compile(r"AIza[0-9A-Za-z\-_]{10,}"), REDACTED),
    # OpenAI-style keys: sk-…, sk-proj-…, sk-ant-…
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{10,}"), REDACTED),
]


def redact_text(s: str) -> str:
    """Return ``s`` with every known secret pattern replaced by ``***``."""
    if not isinstance(s, str) or not s:
        return s
    out = s
    for secret in _exact_secrets():
        if secret in out:
            out = out.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


# Dict keys whose values are always secrets, whatever they contain.
_SENSITIVE_KEYS = {
    "authorization",
    "x-api-key",
    "x-local-token",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "launch_token",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "secret",
    "password",
    "credential",
    "credentials",
}


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith(("_token", "_secret", "_api_key"))


def redact_obj(obj: Any) -> Any:
    """Recursively redact ``obj`` (dicts, lists, tuples, sets, strings)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {
            key: (REDACTED if _is_sensitive_key(key) else redact_obj(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(item) for item in obj)
    if isinstance(obj, (set, frozenset)):
        return type(obj)(redact_obj(item) for item in obj)
    return obj


class RedactionFilter(logging.Filter):
    """Logging filter that scrubs a record's message and args in place."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = redact_obj(record.args)
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact_obj(arg) if isinstance(arg, (dict, list, tuple, set)) else
                        redact_text(arg) if isinstance(arg, str) else arg
                        for arg in record.args
                    )
        except Exception:  # never let redaction break logging
            pass
        return True


__all__ = [
    "REDACTED",
    "RedactionFilter",
    "redact_obj",
    "redact_text",
    "register_secret",
]
