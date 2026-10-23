"""Execution control flags, persisted in the ``settings`` table.

Two boolean kill-switches govern the :class:`~app.execution.runner.JobRunner`:

* ``pause_all`` — runner stops claiming new jobs (in-flight jobs finish).
* ``stop_all``  — runner cancels in-flight jobs and exits its loop.

Each flag may be stored under a namespaced key (``execution.pause_all`` /
``execution.stop_all``, the canonical form written by :func:`set_flag`) or a
bare key (``pause_all`` / ``stop_all``); the namespaced key wins when both
exist.  Values are JSON booleans in ``value_json``, but string forms
(``"true"`` / ``"1"`` / ``"false"`` / ``"0"``) are accepted too.

Reading is deliberately tolerant: a missing ``settings`` table, missing keys
or unparsable values all yield ``False`` defaults rather than raising — a
runner must never crash because a control flag could not be read.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

__all__ = ["Flags", "read_flags", "set_flag"]


@dataclass(frozen=True)
class Flags:
    """Snapshot of the execution control flags."""

    pause_all: bool = False
    stop_all: bool = False


#: Flag name -> keys to try, in priority order (namespaced first).
_FLAG_KEYS: dict[str, tuple[str, ...]] = {
    "pause_all": ("execution.pause_all", "pause_all"),
    "stop_all": ("execution.stop_all", "stop_all"),
}

_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", ""})


def _coerce_bool(value) -> bool:
    """Best-effort coercion of a stored ``value_json`` payload to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip()
        # value_json normally holds JSON ("true"/"false"/"1"); fall back to
        # a plain string comparison when it is not valid JSON.
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            lowered = text.lower()
            if lowered in _TRUE_STRINGS:
                return True
            if lowered in _FALSE_STRINGS:
                return False
            return False
        if isinstance(parsed, str) and parsed is not value:
            return _coerce_bool(parsed)
        return _coerce_bool(parsed)
    return False


def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def read_flags(conn_factory) -> Flags:
    """Read the current flags via ``conn_factory`` (zero-arg -> Connection).

    Never raises for missing table/keys; returns all-False defaults instead.
    """
    all_keys = [key for keys in _FLAG_KEYS.values() for key in keys]
    stored: dict[str, object] = {}
    conn = None
    try:
        conn = conn_factory()
        placeholders = ", ".join("?" for _ in all_keys)
        rows = conn.execute(
            f"SELECT key, value_json FROM settings WHERE key IN ({placeholders})",
            all_keys,
        ).fetchall()
        for row in rows:
            # Positional access works for sqlite3.Row and plain tuples.
            stored[row[0]] = row[1]
    except Exception:
        return Flags()
    finally:
        if conn is not None:
            _close_quietly(conn)

    def pick(name: str) -> bool:
        for key in _FLAG_KEYS[name]:
            if key in stored:
                return _coerce_bool(stored[key])
        return False

    return Flags(pause_all=pick("pause_all"), stop_all=pick("stop_all"))


def set_flag(conn_factory, name: str, value: bool) -> str:
    """Persist one flag; returns the canonical (namespaced) key written.

    ``name`` may be ``"pause_all"`` / ``"stop_all"`` or the already
    namespaced ``"execution.pause_all"`` / ``"execution.stop_all"``.
    Both the canonical and the bare key are written so any reader (old or
    new) observes the same value.  Creates the ``settings`` table (with the
    section-15 shape) when it does not exist yet.
    """
    short = name[len("execution."):] if name.startswith("execution.") else name
    if short not in _FLAG_KEYS:
        raise ValueError(
            f"unknown execution flag {name!r}; expected one of "
            f"{sorted(_FLAG_KEYS)}"
        )
    canonical = f"execution.{short}"
    payload = json.dumps(bool(value))
    now_ms = int(time.time() * 1000)

    conn = conn_factory()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
        if not columns:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY, "
                "value_json TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
            columns = {"key", "value_json", "updated_at"}
        for key in (canonical, short):
            if "updated_at" in columns:
                conn.execute(
                    "INSERT INTO settings (key, value_json, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value_json = excluded.value_json, "
                    "updated_at = excluded.updated_at",
                    (key, payload, now_ms),
                )
            else:
                conn.execute(
                    "INSERT INTO settings (key, value_json) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value_json = excluded.value_json",
                    (key, payload),
                )
        try:
            conn.commit()
        except Exception:
            pass  # autocommit connections (app.db) need no commit
    finally:
        _close_quietly(conn)
    return canonical
