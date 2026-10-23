"""Pipeline integration skeletons (L4 — hermetic pipeline).

The full L4 suite runs the whole DAG on frozen fixtures with every
provider faked (roadmap section 17: F-EN-BASIC, F-UR-BASIC,
F-OTHER-CUSTOM, F-FAIL-* …). Those fixtures live in ``tests/fixtures/``
(owned by a separate worker), so this module is the *skeleton*: it pins
the earliest integration contracts and skips cleanly until the code —
and the third-party deps it needs — are present in the test env.

Contracts pinned here:

1. **Upload idempotency** — the publishing layer must expose an
   idempotency-key helper. The key binds file + channel + title
   (``hash(file+channel+title)`` per section 17; the landed
   implementation is ``app.pipeline.publishing.manifest.
   compute_idempotency_key(master_checksum, channel_id, title)``) so
   running the upload stage twice records exactly ONE
   ``videos.insert``. Wrong-channel upload is risk R17 ("the one
   unforgivable error"), which is why the channel is part of the key.
2. **Hermetic pipeline module presence** — the pipeline package must be
   importable without network or keys (providers are faked in L4).

Skip semantics (important): a module that *exists on disk* but cannot be
imported in this environment because a third-party dependency is missing
(e.g. the manifest module needs pydantic, which a pytest-only env does
not have) produces a **skip**, not a failure — the contract turns hard
automatically in an env that has the backend's deps installed.
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest

pytestmark = pytest.mark.integration

# Modules that own (or will own) the upload idempotency-key helper,
# most specific first. Both import layouts are listed because the
# backend package is reachable as ``app.*`` (backend/ on sys.path) and
# as ``backend.app.*`` (repo root on sys.path, namespace package).
_HELPER_MODULE_CANDIDATES = (
    "app.pipeline.publishing.manifest",
    "backend.app.pipeline.publishing.manifest",
    "app.jobs",
    "backend.app.jobs",
    "backend.jobs",
    "app.pipeline.publishing",
    "backend.app.pipeline.publishing",
    "app.upload",
    "backend.app.upload",
    "jobs",
)

# Accepted names for the idempotency-key helper.
_KEY_HELPERS = (
    "compute_idempotency_key",
    "make_idempotency_key",
    "idempotency_key",
    "upload_idempotency_key",
    "build_idempotency_key",
)

# Pipeline packages whose import proves hermetic importability.
_PIPELINE_CANDIDATES = (
    "app.pipeline",
    "backend.app.pipeline",
    "app.jobs",
    "backend.app.jobs",
)


def _spec_exists(modname: str) -> bool:
    """True when *modname* exists on disk (without importing it)."""
    try:
        return importlib.util.find_spec(modname) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _find_idempotency_helper():
    """Return ``(module, helper)`` or skip with a precise reason.

    * No candidate module on disk yet          -> skip (not landed).
    * Candidate on disk but a dependency of it
      is missing in this env                   -> skip (deps not installed).
    * Candidate imports but exposes no helper,
      and no other candidate provides one      -> FAIL (contract broken).
    """
    landed_but_unimportable: list[str] = []
    imported_without_helper: list[str] = []

    for modname in _HELPER_MODULE_CANDIDATES:
        if not _spec_exists(modname):
            continue
        try:
            module = importlib.import_module(modname)
        except ImportError as exc:
            # The module landed; something it imports has not (in *this*
            # environment). That is an env gap, not a contract break.
            landed_but_unimportable.append(f"{modname} ({exc})")
            continue
        helper = next(
            (
                getattr(module, name)
                for name in _KEY_HELPERS
                if callable(getattr(module, name, None))
            ),
            None,
        )
        if helper is not None:
            return module, helper
        imported_without_helper.append(modname)

    if landed_but_unimportable:
        pytest.skip(
            "idempotency module landed but its dependencies are not "
            "installed in this test env: " + "; ".join(landed_but_unimportable)
        )
    if imported_without_helper:
        pytest.fail(
            "modules imported but none exposes an idempotency-key helper "
            f"{_KEY_HELPERS}: {imported_without_helper} — upload "
            "idempotency (section 17, L4) requires one."
        )
    pytest.skip(
        "no jobs/publishing module landed yet (tried: "
        + ", ".join(_HELPER_MODULE_CANDIDATES)
        + ")"
    )


def test_jobs_module_exposes_idempotency_key_helper():
    """The publishing/jobs layer must expose an idempotency-key helper."""
    module, helper = _find_idempotency_helper()
    assert callable(helper)
    assert module is not None


def test_idempotency_key_is_deterministic_and_channel_bound():
    """Same (file, channel, title) -> same key; different channel -> different key.

    The helper's exact signature is the backend team's choice; this test
    tries the documented positional shapes and skips if none fit yet.
    If a shape fits, the assertions are hard — determinism and channel
    binding are the contract, not implementation details.
    """
    _, helper = _find_idempotency_helper()

    shapes = (
        ("deadbeef-master-checksum", "channel-A", "My Title"),
        ("video.mp4", "channel-A", "My Title"),
        (b"file-bytes", "channel-A", "My Title"),
    )
    for args in shapes:
        try:
            key1 = helper(*args)
            key2 = helper(*args)
            other_channel = helper(args[0], "channel-B", args[2])
        except TypeError:
            continue  # signature differs; try the next documented shape
        assert key1 == key2, "idempotency key is not deterministic"
        assert key1 != other_channel, (
            "idempotency key does not bind the channel — wrong-channel "
            "uploads (risk R17) would not be caught"
        )
        return
    pytest.skip("idempotency-key helper signature not recognised yet")


def test_hermetic_pipeline_imports_without_secrets():
    """Pipeline package imports in a redacted env (no keys, no network).

    Skips until the pipeline package lands; the autouse redaction
    fixture in conftest.py has already stripped *_API_KEY variables, so
    a successful import here proves import-time hermeticity.
    """
    for modname in _PIPELINE_CANDIDATES:
        if not _spec_exists(modname):
            continue
        try:
            module = importlib.import_module(modname)
        except ImportError as exc:
            pytest.skip(f"{modname} landed but not importable in this env: {exc}")
        assert module is not None
        return
    pytest.skip(
        "no pipeline module landed yet (tried: " + ", ".join(_PIPELINE_CANDIDATES) + ")"
    )
