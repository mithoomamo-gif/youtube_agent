"""Pixabay LIVE harness — real API, runs ONLY when a key exists.

Marked ``live`` and SKIPPED by default: when ``PIXABAY_API_KEY`` is not in
the environment the test skips cleanly (see ``-rs`` output), so the normal
suite never touches the network and never needs a key. See
``tests/providers_live/README.md`` for how to supply the key.

What it does when the key IS present (exactly ONE real search call):

1. Builds the EXISTING adapter exactly as the mocked suite does —
   ``PixabayProvider(secret_provider, key_ref="pixabay_api_key", ...)``
   (``app.providers.stock.pixabay.PixabayProvider.search(query,
   media_type, filters)``) — with a real ``httpx.AsyncClient`` instead of
   a ``MockTransport``.
2. Takes the first real ``Candidate`` and creates a licence-ledger row
   from it through the EXISTING ledger API — ``build_row(...)`` +
   ``InMemoryLicenceLedger.write_row(...)`` from
   ``app.providers.stock.ledger``, with the licence from
   ``pixabay_licence()`` (``app.providers.stock.licences``), the same
   calls ``tests/providers/test_stock_media.py`` makes.
3. Asserts the row exists (``find_row`` / ``assert_renderable``) with the
   expected fields: provider/source, licence info, asset id and URLs.
   The ledger records the key REF (``pixabay_api_key``) only — never the
   key value.

Key source (dev only): the environment variable ``PIXABAY_API_KEY``,
resolved through the EXISTING env-based SecretProvider
``EnvSecretProvider`` (``app.providers.secrets_env``) under the adapter's
key ref ``pixabay_api_key`` (its ref->env mapping resolves
``PIXABAY_API_KEY``). The Round-2 dependency this harness originally
waited on has landed, so the earlier ``DictSecretProvider`` wiring is
gone — the rest of this test is unchanged.
In production the key never comes from the environment at all: the user
enters it in Settings and it is stored in Windows Credential Manager via
``keyring`` (see README.md).

Sandbox note (harness only, adapter untouched): on this VM a plain
``httpx.AsyncClient()`` dies at construction with
``httpx.InvalidURL: Invalid port: ':1]'`` because httpx parses the
sandbox's ``no_proxy`` env entries (``[::1]`` etc.) when ``trust_env``
is on. ``_live_client()`` below applies the same local workaround the
TTS adapters use: read the proxy URL from the env explicitly, pass it
as ``proxy=`` with ``trust_env=False``, and verify TLS with
``ssl.create_default_context()`` so the egress proxy's CA (via
``SSL_CERT_FILE``) verifies. With no proxy env present it is just a
plain client, exactly as before.
"""
from __future__ import annotations

import asyncio
import os
import ssl

import httpx
import pytest

from app.providers.secrets_env import EnvSecretProvider
from app.providers.stock import (
    InMemoryLicenceLedger,
    MediaType,
    SearchFilters,
)
from app.providers.stock.ledger import build_row
from app.providers.stock.licences import pixabay_licence
from app.providers.stock.pixabay import PixabayProvider

pytestmark = pytest.mark.live

#: Env var the USER supplies for dev runs of this harness (never hardcoded).
ENV_VAR = "PIXABAY_API_KEY"
#: The adapter's key reference — the default ``key_ref`` of PixabayProvider
#: and the same ref the mocked provider tests use.
PIXABAY_KEY_REF = "pixabay_api_key"

PROJECT_ID = "live-pixabay-smoke"
SCENE_ID = "scene-live-1"


def _live_client() -> httpx.AsyncClient:
    """Real client for the live run (see sandbox note in module docstring)."""
    proxy = next(
        (os.environ[v] for v in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
         if os.environ.get(v)),
        None,
    )
    if proxy is None:
        return httpx.AsyncClient(timeout=30.0)
    return httpx.AsyncClient(
        timeout=30.0,
        trust_env=False,  # never parse the broken no_proxy env entries
        proxy=proxy,
        verify=ssl.create_default_context(),
    )


@pytest.fixture(scope="module")
def pixabay_api_key() -> str:
    key = os.environ.get(ENV_VAR, "").strip()
    if not key:
        pytest.skip(
            f"{ENV_VAR} is not set in the environment — skipping Pixabay "
            "live test (no real key available; see "
            "tests/providers_live/README.md for how to supply one)"
        )
    return key


def test_pixabay_live_search_creates_licence_ledger_row(pixabay_api_key: str):
    async def go():
        # Our own real client so it can be closed; the adapter would
        # otherwise create (and own) one itself.
        async with _live_client() as client:
            provider = PixabayProvider(
                EnvSecretProvider(dotenv_path=False),
                PIXABAY_KEY_REF,
                http_client=client,
            )
            # No network: availability is key-present + breaker-closed.
            assert await provider.is_available() is True
            # The ONE real search call this harness is allowed to make.
            return await provider.search(
                "ocean",
                MediaType.VIDEO,
                SearchFilters(media_type=MediaType.VIDEO),
            )

    results = asyncio.run(go())

    assert results, "Pixabay live search for 'ocean' (video) returned no results"
    candidate = results[0]

    # --- the real result, through the existing Candidate vocabulary ------
    assert candidate.provider == "pixabay"
    assert candidate.provider_asset_id
    assert candidate.media_type == MediaType.VIDEO
    assert candidate.page_url  # asset page URL on pixabay.com
    assert candidate.download_url  # best download tier URL from the hit
    licence = pixabay_licence()
    assert candidate.licence_id == licence.licence_id

    # --- licence-ledger row via the existing ledger API ------------------
    ledger = InMemoryLicenceLedger()
    row = build_row(
        project_id=PROJECT_ID,
        scene_id=SCENE_ID,
        candidate=candidate,
        licence=licence,
        retrieved_by_key_id=PIXABAY_KEY_REF,
    )
    # The ledger stores the key REF, never the key value.
    assert row.retrieved_by_key_id == PIXABAY_KEY_REF
    assert row.retrieved_by_key_id != pixabay_api_key

    written = ledger.write_row(row)
    assert written.ledger_id

    found = ledger.find_row(
        provider="pixabay",
        provider_asset_id=candidate.provider_asset_id,
        project_id=PROJECT_ID,
    )
    assert found is not None
    # Source / provider
    assert found.provider == "pixabay"
    assert found.project_id == PROJECT_ID
    assert found.scene_id == SCENE_ID
    # Asset id / URLs
    assert found.provider_asset_id == candidate.provider_asset_id
    assert found.asset_page_url == candidate.page_url
    assert found.download_url == candidate.download_url
    # Licence info
    assert found.licence_name == "Pixabay Content License"
    assert found.licence_url == "https://pixabay.com/service/license-summary/"
    assert found.licence_snapshot_date
    assert found.licence_terms_hash
    assert found.commercial_use_allowed is True
    assert found.attribution_required is False
    assert found.retrieved_at

    # Render gate: with the row present, the asset is renderable.
    assert ledger.assert_renderable(candidate, project_id=PROJECT_ID).ledger_id == row.ledger_id
