"""ROUND 6 — Invariant 1 proof pack: no secret ever in plaintext anywhere.

Roadmap invariant 1: *no secret ever in plaintext anywhere* — API keys
and OAuth tokens live only in the OS credential store
(``app.secrets_store``, service ``YouTubeAgent``); SQLite holds only an
opaque reference plus a last-4 display hint.

This module is a re-runnable, automated sweep (extends the Round-1 L9
scanner in ``tests/contract/test_no_secrets_in_repo.py``, which covers
only backend/src/prompts with three patterns):

* STRICT scopes — tracked source (``backend/app``, ``src``,
  ``src-tauri``, ``prompts``, ``tools``, ``oauth-pages``, ``.github``,
  ``packaging``, ``scripts``), ``docs/``, repo-root config files, and
  the text artefacts under ``outputs/`` / ``backend/outputs/`` — must
  contain ZERO secret-shaped material at deliberately oversensitive
  (loose) thresholds.
* TEST scopes (``backend/tests``, ``tests``) legitimately contain
  synthetic credential fixtures. A hit there is excused ONLY by an
  exact ``(file, pattern)`` entry in ``FIXTURE_ALLOWLIST`` *and* only
  when the matched value is provably not a real secret: it either
  carries an obvious fake marker (fake/test/fixture/seed/…) or is far
  shorter than the real-world minimum for its token type. Anything
  else fails — including in allowlisted files.
* The repo-root ``.env`` (the user's git-ignored dev secrets file) is
  governed by a dedicated test: it must stay git-ignored, untracked
  and mode-0600, and its only secret-shaped content must be the one
  known finding R6-F1 (see ``docs/ROUND6_INVARIANTS.md``).

Findings are reported as ``file:line pattern fingerprint(first-4)``
ONLY — a matched value is never printed in full, in assertion
messages or anywhere else.

Positive controls at the bottom prove the scanner can actually say NO
(a gate never seen to fail is assumed broken).
"""

from __future__ import annotations

import math
import re
import stat
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Scanner core
# ---------------------------------------------------------------------------


def _shannon(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# Loose thresholds on purpose: fixtures of 20–40 chars trip the prefix
# patterns in test trees (and are adjudicated there), while real tokens
# (Google access tokens ~100–250 chars, JWTs 100+ chars, API keys 39+)
# can never slip under them in the strict scopes.
PREFIX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z\-_]{20,}")),
    ("google_client_secret", re.compile(r"GOCSPX-[A-Za-z0-9\-_]{10,}")),
    ("google_access_token", re.compile(r"ya29\.[A-Za-z0-9\-_]{10,}")),
    ("google_refresh_token", re.compile(r"1//[A-Za-z0-9\-_]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9\-_]{20,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9\-_]{8,}\.eyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}"
        ),
    ),
    (
        "bearer_value",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/=]{20,}"),
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "tauri_signing_key_value",
        re.compile(r"TAURI_SIGNING_PRIVATE_KEY\s*[:=]\s*[\"']?[A-Za-z0-9+/=]{20,}"),
    ),
)

ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)\b(api[_-]?key|api[_-]?secret|client[_-]?secret|access[_-]?token
       |refresh[_-]?token|auth[_-]?token|private[_-]?key|password|passwd
       |secret|token)\b\s*[:=]\s*["'](?P<value>[^"'\n]{8,})["']"""
)

_PLACEHOLDER_HINTS = (
    "example", "dummy", "placeholder", "changeme", "change-me", "your-",
    "your_", "test", "fake", "sample", "redacted", "xxx", "<", "${", "{{",
    "os.environ", "os.getenv", "getenv", "environ", "settings.", "config.",
    "none", "null", "seed-", "mock", "stub", "hunter", "fixtur",
)

#: Substrings that mark a value as an obvious synthetic fixture.
FAKE_MARKERS = (
    "fake", "test", "fixtur", "seed", "dummy", "mock", "example",
    "sample", "synthetic",
)

#: Real-world minimum lengths: a matched value shorter than this cannot
#: be a real token of its type (real ya29. access tokens are ~100–250
#: chars; real JWTs are 100+ chars; real Bearer credentials for this
#: app are 32+ chars).
SHORT_LENGTH_LIMITS = {
    "google_access_token": 40,
    "jwt": 100,
    "bearer_value": 40,
    # Assignment-shaped hits in test trees only: real provider keys are
    # 32+ chars and are independently caught by the prefix patterns;
    # synthetic launch-token fixtures are shorter than 40.
    "high_entropy_assignment": 40,
}

ALLOW_MARKERS = ("secret-scan: allow", "round6-scan: allow")


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if any(hint in lowered for hint in _PLACEHOLDER_HINTS):
        return True
    if len(set(lowered)) <= 3:  # e.g. "aaaaaaaa" / "xxxx-xxxx"
        return True
    return False


def scan_line(line: str) -> list[tuple[str, str]]:
    """Return ``(pattern_name, matched_value)`` hits for one line."""
    if any(marker in line for marker in ALLOW_MARKERS):
        return []
    hits: list[tuple[str, str]] = []
    for name, pattern in PREFIX_PATTERNS:
        for match in pattern.finditer(line):
            hits.append((name, match.group(0)))
    for match in ASSIGNMENT_PATTERN.finditer(line):
        value = match.group("value")
        if _is_placeholder(value):
            continue
        if len(value) >= 16 and _shannon(value) >= 3.5:
            hits.append(
                ("high_entropy_assignment:" + match.group(1).lower(), value)
            )
    return hits


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, pattern_name, matched_value)`` for a whole file."""
    out: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, value in scan_line(line):
            out.append((lineno, name, value))
    return out


def _fingerprint(value: str) -> str:
    """Redacted fingerprint: first 4 chars + length. Never the value."""
    return f"{value[:4]}…(len={len(value)})"


# ---------------------------------------------------------------------------
# File iteration
# ---------------------------------------------------------------------------

TEXT_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".md", ".txt", ".html", ".sh", ".ps1",
    ".srt", ".csv", ".xml", ".env", ".example",
}
SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "target", ".tmp-yta",
}
FULL_READ_LIMIT = 2 * 1024 * 1024  # outputs: larger files are head/tail sampled
EDGE_SAMPLE = 128 * 1024


def _iter_files(roots: list[str]):
    for root in roots:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        if base.is_file():
            yield base
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() in TEXT_EXTENSIONS or path.name.startswith(".env"):
                yield path


def _read_for_scan(path: Path) -> str:
    """Read a file for scanning; oversize files contribute head+tail.

    Secret material in generated outputs lives in small metadata /
    report files (read in full); the head+tail rule bounds runtime on
    multi-MB transcript/pack JSON while still covering the regions
    where a leaked header/credential block would appear. Source and
    docs files are far below the limit and always read in full.
    """
    size = path.stat().st_size
    if size <= FULL_READ_LIMIT:
        return path.read_text(encoding="utf-8", errors="ignore")
    with path.open("rb") as fh:
        head = fh.read(EDGE_SAMPLE)
        fh.seek(max(0, size - EDGE_SAMPLE))
        tail = fh.read(EDGE_SAMPLE)
    return head.decode("utf-8", errors="ignore") + "\n" + tail.decode(
        "utf-8", errors="ignore"
    )


def _scan_roots(roots: list[str]) -> list[tuple[str, int, str, str]]:
    """All hits as ``(relpath, lineno, pattern, redacted_fingerprint)``."""
    hits: list[tuple[str, int, str, str]] = []
    for path in _iter_files(roots):
        try:
            text = _read_for_scan(path)
        except OSError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, name, value in scan_text(text):
            hits.append((rel, lineno, name, _fingerprint(value)))
    return hits


# ---------------------------------------------------------------------------
# (a) Strict scopes — zero hits allowed, no allowlist
# ---------------------------------------------------------------------------

STRICT_SOURCE_ROOTS = [
    "backend/app", "src", "src-tauri", "prompts", "tools", "oauth-pages",
    ".github", "packaging", "scripts", "docs",
    # repo-root config files
    "package.json", "Makefile", "README.md", ".env.example",
]
OUTPUT_ROOTS = ["outputs", "backend/outputs"]
OUTPUT_EXTENSIONS = {".json", ".md", ".txt", ".srt", ".csv", ".log",
                     ".html", ".xml", ".yaml", ".yml"}


def test_strict_source_and_docs_have_zero_secret_shaped_hits():
    """Invariant 1, strict scope: shipped source + docs are clean."""
    hits = _scan_roots(STRICT_SOURCE_ROOTS)
    assert hits == [], (
        "secret-shaped material in strict scope (file:line pattern fp):\n"
        + "\n".join(f"{r}:{ln} {n} {fp}" for r, ln, n, fp in hits)
    )


def test_generated_outputs_have_zero_secret_shaped_hits():
    """Invariant 1, outputs scope: generated packs/reports are clean.

    Only text artefacts can carry a plaintext secret (media binaries
    cannot be 'plaintext'), so the scan covers the text extensions.
    """
    hits: list[tuple[str, int, str, str]] = []
    for path in _iter_files(OUTPUT_ROOTS):
        if path.suffix.lower() not in OUTPUT_EXTENSIONS:
            continue
        try:
            text = _read_for_scan(path)
        except OSError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, name, value in scan_text(text):
            hits.append((rel, lineno, name, _fingerprint(value)))
    assert hits == [], (
        "secret-shaped material in generated outputs:\n"
        + "\n".join(f"{r}:{ln} {n} {fp}" for r, ln, n, fp in hits)
    )


# ---------------------------------------------------------------------------
# (a) Test scopes — fixtures adjudicated by exact allowlist + guards
# ---------------------------------------------------------------------------

#: Exact (relpath, pattern-prefix) fixture entries verified 2026-10-23.
#: Every entry must ALSO pass the fake-marker / short-length guard in
#: the test below — an entry alone never excuses a hit.
FIXTURE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("backend/tests/test_channels_connect.py", "google_access_token"):
        "synthetic ya29.* fixtures for the OAuth connect flow; each is "
        "<40 chars (real Google access tokens are ~100-250 chars)",
    ("backend/tests/test_comments_fetch.py", "bearer_value"):
        "mock-transport assertion value carrying a 'fake' marker",
    ("backend/tests/test_style_lab_metadata.py", "bearer_value"):
        "mock-transport assertion value carrying a 'fake' marker",
    ("backend/tests/test_analytics_ingestion_e2e.py", "high_entropy_assignment"):
        "fixture access token carrying a 'fixture' marker",
    ("backend/tests/test_config.py", "high_entropy_assignment"):
        "synthetic JWT-shaped config fixture, <100 chars (real JWTs "
        "are 100+ chars)",
    ("backend/tests/test_data_dir_contract.py", "high_entropy_assignment"):
        "synthetic launch-token fixture, <40 chars (production launch "
        "tokens are 64-char hex from 32 random bytes)",
    ("tests/integration/test_approvals_persistence.py",
     "high_entropy_assignment"):
        "synthetic launch-token fixture, <40 chars (production launch "
        "tokens are 64-char hex from 32 random bytes)",
}


def _excused_by_guard(pattern: str, value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in FAKE_MARKERS):
        return True
    base_pattern = pattern.split(":", 1)[0]
    limit = SHORT_LENGTH_LIMITS.get(base_pattern)
    if limit is not None and len(value) < limit:
        return True
    # A high-entropy-assignment hit whose *value* is itself a short
    # JWT-shaped fixture is judged by the JWT length rule.
    if base_pattern == "high_entropy_assignment" and value.startswith("eyJ"):
        return len(value) < SHORT_LENGTH_LIMITS["jwt"]
    return False


def test_test_tree_fixtures_are_exactly_the_allowlisted_ones():
    """Test trees may hold synthetic fixtures — only the known ones."""
    offenders: list[str] = []
    unguarded: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in _iter_files(["backend/tests", "tests"]):
        if path.resolve() == Path(__file__).resolve():
            # The scanner's own positive controls are fragment-assembled
            # and never match at rest; skip self for clarity anyway.
            continue
        try:
            text = _read_for_scan(path)
        except OSError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, name, value in scan_text(text):
            base = name.split(":", 1)[0]
            key = (rel, name) if (rel, name) in FIXTURE_ALLOWLIST else (rel, base)
            if key not in FIXTURE_ALLOWLIST:
                offenders.append(f"{rel}:{lineno} {name} {_fingerprint(value)}")
                continue
            seen.add(key)
            if not _excused_by_guard(name, value):
                unguarded.append(
                    f"{rel}:{lineno} {name} {_fingerprint(value)} "
                    "(allowlisted file/pattern but value has no fake "
                    "marker and is not below the real-token length floor)"
                )
    assert offenders == [], (
        "non-allowlisted secret-shaped hits in test trees:\n"
        + "\n".join(offenders)
    )
    assert unguarded == [], (
        "allowlisted fixture hits that fail the fake/short guards:\n"
        + "\n".join(unguarded)
    )


# ---------------------------------------------------------------------------
# (a) The repo-root .env — known finding R6-F1, governed not hidden
# ---------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, timeout=30,
    )


def test_dot_env_is_ignored_untracked_restricted_and_contains_only_r6_f1():
    """The dev ``.env`` is the ONE known plaintext exception (R6-F1).

    It holds the user's own Google OAuth *client* secret for local dev
    (Round 3 decision; production uses the OS keyring instead). This
    test pins its governance: git-ignored, untracked, mode 0600, and
    its only secret-shaped line is the known client-secret line. A
    second secret appearing here fails this test loudly.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        pytest.skip(".env absent on this machine — nothing to govern")

    assert _git("check-ignore", "-q", ".env").returncode == 0, (
        ".env must be git-ignored"
    )
    assert _git("ls-files", "--error-unmatch", ".env").returncode != 0, (
        ".env must never be tracked by git"
    )
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode & 0o077 == 0, f".env permissions {oct(mode)} too open"

    hits = []
    for lineno, name, value in scan_text(
        env_path.read_text(encoding="utf-8", errors="ignore")
    ):
        hits.append((lineno, name, _fingerprint(value)))
    # Exactly one hit is expected: the OAuth client secret (GOCSPX-…).
    # The client *id* line (…apps.googleusercontent.com) is a public
    # identifier and must NOT match any secret pattern.
    assert [(ln, n) for ln, n, _ in hits] == [(5, "google_client_secret")], (
        f".env secret-shaped hits differ from the single known R6-F1 "
        f"finding: {hits}"
    )


# ---------------------------------------------------------------------------
# (a) Providers API is write-only — keys never come back over REST
# ---------------------------------------------------------------------------


class _FakeKeyring:
    """In-memory keyring stand-in (headless Linux has no real backend)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


def test_providers_api_is_write_only_for_key_material(client, monkeypatch):
    """PUT a key -> it is stored via the secret layer, and NO response
    (PUT echo or provider list) ever contains the key value — only
    ``configured`` + ``key_last4`` metadata (roadmap sections 14/31).
    """
    from app import secrets_store

    monkeypatch.setattr(secrets_store, "keyring", _FakeKeyring())
    fake_value = "r6-fake-provider-key-0123456789abcdef"  # round6-scan: allow

    put = client.put("/api/v1/providers/gemini/key", json={"value": fake_value})
    assert put.status_code == 200, put.text
    assert fake_value not in put.text
    assert put.json()["key_last4"] == fake_value[-4:]

    listing = client.get("/api/v1/providers")
    assert listing.status_code == 200, listing.text
    assert fake_value not in listing.text
    gemini = next(
        item for item in listing.json()["items"] if item["id"] == "gemini"
    )
    assert gemini["configured"] is True
    assert gemini["key_last4"] == fake_value[-4:]


def test_secrets_store_never_persists_to_sqlite_schema():
    """The DB schema stores only references: no secret/value column
    exists on provider_credentials (source-level invariant)."""
    ddl = (REPO_ROOT / "backend" / "app" / "schema_ddl.py").read_text(
        encoding="utf-8", errors="ignore"
    )
    chunks = ddl.split("CREATE TABLE")
    bodies = [
        c for c in chunks
        if c.lstrip().startswith("IF NOT EXISTS provider_credentials")
    ]
    assert bodies, "provider_credentials DDL not found"
    body = bodies[0].lower()
    assert "keyring_ref" in body
    for forbidden in ("secret_value", "api_key_value", "token_value",
                      "refresh_token", "access_token", "password"):
        assert forbidden not in body, (
            f"provider_credentials must not store {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# Positive controls — the scanner must be able to say NO
# (fake secrets are fragment-assembled so this file never matches at rest)
# ---------------------------------------------------------------------------

_FAKE_GOOGLE_KEY = "AI" + "za" + "B" * 35
_FAKE_CLIENT_SECRET = "GOC" + "SPX-" + "x" * 28
_FAKE_OPENAI_KEY = "s" + "k-" + "A" * 30
_FAKE_JWT = (
    "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0"
    + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
)


def test_scanner_detects_planted_google_api_key():
    assert scan_text(f'KEY = "{_FAKE_GOOGLE_KEY}"\n'), "scanner is broken"


def test_scanner_detects_planted_google_client_secret():
    assert scan_text(f'CLIENT_SECRET = "{_FAKE_CLIENT_SECRET}"\n')


def test_scanner_detects_planted_openai_key():
    assert scan_text(f'KEY = "{_FAKE_OPENAI_KEY}"\n')


def test_scanner_detects_planted_jwt():
    assert scan_text(f'TOKEN = "{_FAKE_JWT}"\n')


def test_scanner_detects_planted_high_entropy_assignment():
    planted = 'api_key = "' + "Zx9Q" + "w8Er7Ty6" + "Ui5Op4As3Df2" + '"\n'
    assert scan_text(planted)


def test_scanner_allows_env_lookups_and_placeholders():
    assert scan_text('api_key = os.environ["GEMINI_API_KEY"]\n') == []
    assert scan_text('api_key = "your-api-key-here"\n') == []
    assert scan_text('refresh_token = "test-refresh-token-value"\n') == []
