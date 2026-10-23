"""No-secrets-in-repo scanner (L9 — security & privacy, every PR, hard fail).

Scans the *tracked source* trees (``backend/``, ``src/``, ``prompts/`` —
whichever exist) for key-shaped literals:

* OpenAI-style keys:      ``sk-…`` (20+ token chars)
* Google API keys:        ``AIza…`` (35 token chars after the prefix)
* Literal assignments:    ``api_key = "…"`` / ``api_secret: "…"`` etc.
  with a plausible secret value (placeholders / env lookups are allowed)

``tests/`` itself is excluded: fixtures and this scanner legitimately
*describe* key shapes. The same applies to test trees at any depth
(e.g. ``backend/tests/``): unit tests for token managers and provider
adapters legitimately use dummy credential fixtures such as
``access_token="seed-access-token"``. The L9 gate protects *shipped
source*; this mirrors the CI ``secret-scan`` job's exclusion of test
paths. A separate worker owns ``tests/fixtures/`` dummy strings, which
are likewise out of scope.

Positive controls below prove the scanner can actually say NO — per
roadmap section 17, *a gate never seen to fail is assumed broken*.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("backend", "src", "prompts")
TEXT_SUFFIXES = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".md", ".txt", ".env", ".example", ".sh", ".ps1",
}
SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "target", "tests"}
ALLOW_MARKER = "secret-scan: allow"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key (sk-...)", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("google api key (AIza...)", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    (
        "hardcoded api_key/secret literal",
        re.compile(
            r"""(?ix)
            \b(?:api[_-]?key|api[_-]?secret|client[_-]?secret|access[_-]?token
               |auth[_-]?token|private[_-]?key)\b
            \s*[:=]\s*
            ["'](?P<value>[^"'\n]{8,})["']
            """,
        ),
    ),
)

# Values that are obviously not real secrets.
_PLACEHOLDER_HINTS = (
    "example", "dummy", "placeholder", "changeme", "change-me", "your-",
    "your_", "test", "fake", "sample", "redacted", "xxx", "<", "${", "{{",
    "os.environ", "os.getenv", "getenv", "environ", "settings.", "config.",
    "none", "null",
)


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(hint in lowered for hint in _PLACEHOLDER_HINTS)


def scan_text(text: str) -> list[str]:
    """Return human-readable findings for one file's text."""
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for label, pattern in _PATTERNS:
            for match in pattern.finditer(line):
                value = match.groupdict().get("value") if match.re.groups else None
                if value is not None and _is_placeholder(value):
                    continue
                findings.append(f"line {lineno}: {label}: {match.group(0)[:12]}…")
    return findings


def iter_source_files(root: Path):
    for dirname in SCAN_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env"):
                yield path


def test_no_secrets_in_tracked_sources(repo_root):
    """No key-shaped literal may exist in backend/, src/ or prompts/."""
    offenders: list[str] = []
    for path in iter_source_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for finding in scan_text(text):
            offenders.append(f"{path.relative_to(repo_root)}: {finding}")
    assert not offenders, "possible secrets committed to the repo:\n" + "\n".join(offenders)


# --- Positive controls: the gate must be able to fail ----------------------
# Fake keys are assembled from fragments so this file never contains a
# literal that matches the patterns above (defence in depth — tests/ is
# excluded from the scan, but the file should still be safe to copy).

_FAKE_OPENAI = "s" + "k-" + "A" * 24
_FAKE_GOOGLE = "AI" + "za" + "B" * 35


def test_scanner_detects_planted_openai_key():
    findings = scan_text(f'KEY = "{_FAKE_OPENAI}"\n')
    assert findings, "scanner missed a planted OpenAI-style key — gate is broken"


def test_scanner_detects_planted_google_key():
    findings = scan_text(f'KEY = "{_FAKE_GOOGLE}"\n')
    assert findings, "scanner missed a planted Google API key — gate is broken"


def test_scanner_detects_hardcoded_assignment():
    findings = scan_text('api_key = "hunter2hunter2"\n')
    assert findings, "scanner missed a hardcoded api_key literal — gate is broken"


def test_scanner_allows_env_lookups_and_placeholders():
    assert scan_text('api_key = os.environ["YOUTUBE_API_KEY"]\n') == []
    assert scan_text('api_key = "your-api-key-here"\n') == []
    assert scan_text('# api_key = "example-dummy-value"  # docs\n') == []
