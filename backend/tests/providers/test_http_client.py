"""Shared provider HTTP factory tests — no network.

Regression coverage for the Round 3 proxy bug: httpx's default
``trust_env=True`` client construction died with
``httpx.InvalidURL: Invalid port: ':1]'`` whenever ``NO_PROXY``
contained bracketed IPv6 literals (``[::1]``, ``[fd8b:…::1]``), because
``httpx._utils.get_environment_proxies`` keys them ``all://*[::1]``.
The factory in ``app.providers.http_client`` must construct clients
under that exact environment shape — and stay a no-op without it.
"""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest

from app.providers.http_client import (
    create_async_client,
    create_client,
    environment_proxy_mounts,
    resolve_verify,
    sanitise_no_proxy_entry,
    sanitise_proxy_url,
)

# The sandbox's NO_PROXY shape: bare AND bracketed IPv6 side by side.
SANDBOX_NO_PROXY = (
    "localhost,127.0.0.1,::1,[::1],198.19.0.1,198.19.0.2,"
    "fd8b:4f84:7d32:99::1,[fd8b:4f84:7d32:99::1],"
    "fd8b:4f84:7d32:99::2,[fd8b:4f84:7d32:99::2]"
)
FAKE_PROXY = "http://user:pass@proxy.example.test:3128"


@pytest.fixture()
def hostile_env(monkeypatch):
    """Process env shaped exactly like the sandbox that broke httpx."""
    for var in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, FAKE_PROXY)
    monkeypatch.setenv("NO_PROXY", SANDBOX_NO_PROXY)
    monkeypatch.setenv("no_proxy", SANDBOX_NO_PROXY)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    return monkeypatch


@pytest.fixture()
def clean_env(monkeypatch):
    for var in ("http_proxy", "https_proxy", "all_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "NO_PROXY", "no_proxy", "SSL_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# sanitise_no_proxy_entry
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("::1", "all://[::1]"),
        ("[::1]", "all://[::1]"),  # the entry that crashed httpx
        ("fd8b:4f84:7d32:99::1", "all://[fd8b:4f84:7d32:99::1]"),
        ("[fd8b:4f84:7d32:99::1]", "all://[fd8b:4f84:7d32:99::1]"),
        ("[::1]:8080", "all://[::1]:8080"),
        ("127.0.0.1", "all://127.0.0.1"),
        ("192.168.0.0/16", "all://192.168.0.0/16"),
        ("localhost", "all://localhost"),
        ("example.com", "all://*example.com"),
        (".google.com", "all://*.google.com"),
        ("*.google.com", "all://*.google.com"),
        ("*", None),
        ("", None),
        ("[::1", None),   # malformed bracket — skipped, never raises
        ("[]", None),
    ],
)
def test_sanitise_no_proxy_entry(entry, expected):
    assert sanitise_no_proxy_entry(entry) == expected


def test_every_sanitised_key_is_httpx_parseable():
    for entry in SANDBOX_NO_PROXY.split(","):
        key = sanitise_no_proxy_entry(entry)
        assert key is not None
        assert "*[" not in key  # the exact bug shape
        url = httpx.URL(key)
        _ = url.port  # raises InvalidURL on the pre-fix key shape
        assert url.host


# ---------------------------------------------------------------------------
# sanitise_proxy_url / environment_proxy_mounts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("http://proxy.example.test:3128", True),
        ("proxy.example.test:3128", True),  # scheme-less gets http://
        ("http://user:pass@proxy.example.test:3128", True),
        ("http://proxy.example.test:notaport", False),
        ("http://[::1", False),
        ("ftp://proxy.example.test:21", False),
        ("", False),
        (None, False),
    ],
)
def test_sanitise_proxy_url(raw, ok):
    result = sanitise_proxy_url(raw)
    assert (result is not None) is ok


def test_mounts_under_sandbox_shape():
    env = {
        "https_proxy": FAKE_PROXY,
        "http_proxy": FAKE_PROXY,
        "all_proxy": FAKE_PROXY,
        "no_proxy": SANDBOX_NO_PROXY,
    }
    mounts = environment_proxy_mounts(env)
    assert mounts["https://"] == FAKE_PROXY
    assert mounts["http://"] == FAKE_PROXY
    assert mounts["all://"] == FAKE_PROXY
    # Bare + bracketed duplicates collapse into ONE bypass key each.
    assert mounts["all://[::1]"] is None
    assert mounts["all://[fd8b:4f84:7d32:99::1]"] is None
    assert mounts["all://[fd8b:4f84:7d32:99::2]"] is None
    assert mounts["all://localhost"] is None
    assert mounts["all://127.0.0.1"] is None
    assert len(mounts) == 3 + 7  # 3 proxy schemes + 7 deduped bypasses
    for key in mounts:
        assert "*[" not in key
        _ = httpx.URL(key).port


def test_mounts_empty_without_proxy_env():
    assert environment_proxy_mounts({}) == {}
    # Bypass entries alone (no proxy) configure nothing.
    assert environment_proxy_mounts({"no_proxy": "localhost,::1"}) == {}


def test_mounts_malformed_proxy_is_skipped_not_raised():
    env = {"https_proxy": "http://host:notaport", "no_proxy": "[::1]"}
    assert environment_proxy_mounts(env) == {}


def test_mounts_no_proxy_star_disables_proxying():
    env = {"https_proxy": FAKE_PROXY, "no_proxy": "localhost,*"}
    assert environment_proxy_mounts(env) == {}


def test_mounts_garbage_no_proxy_entries_never_crash():
    env = {
        "https_proxy": FAKE_PROXY,
        "no_proxy": "[::1,:::,[],..,http://x:bad,::1",
    }
    mounts = environment_proxy_mounts(env)
    assert mounts["https://"] == FAKE_PROXY
    assert mounts["all://[::1]"] is None
    for key in mounts:
        _ = httpx.URL(key).port


# ---------------------------------------------------------------------------
# resolve_verify
# ---------------------------------------------------------------------------
def test_resolve_verify_defaults_to_certifi_behaviour():
    assert resolve_verify({}) is True


def test_resolve_verify_uses_os_store_when_ssl_cert_file_set():
    import certifi

    result = resolve_verify({"SSL_CERT_FILE": certifi.where()})
    assert isinstance(result, ssl.SSLContext)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------
def test_client_constructs_under_hostile_env(hostile_env):
    client = create_client(timeout=5.0)
    try:
        patterns = {p.pattern for p in client._mounts}
        assert "all://[::1]" in patterns
        proxy_transport = client._transport_for_url(
            httpx.URL("https://example.com"))
        assert proxy_transport is not client._transport
        # NO_PROXY hosts go direct.
        assert client._transport_for_url(
            httpx.URL("http://localhost:8188")) is client._transport
        assert client._transport_for_url(
            httpx.URL("http://127.0.0.1:8188")) is client._transport
    finally:
        client.close()


def test_async_client_constructs_under_hostile_env(hostile_env):
    async def main():
        async with create_async_client(timeout=5.0) as client:
            patterns = {p.pattern for p in client._mounts}
            assert "all://[fd8b:4f84:7d32:99::1]" in patterns
            assert not any("*[" in p for p in patterns)

    asyncio.run(main())


def test_client_constructs_in_real_environment():
    """THE regression test: no env patching — whatever environment this
    suite runs in (including the sandbox that produced InvalidURL
    ':1]'), factory clients must construct."""
    client = create_client(timeout=5.0)
    client.close()
    sync_ok = True
    assert sync_ok


def test_async_client_constructs_in_real_environment():
    async def main():
        async with create_async_client(timeout=5.0):
            pass

    asyncio.run(main())


def test_client_is_direct_noop_without_proxy_env(clean_env):
    client = create_client(timeout=5.0)
    try:
        assert client._mounts == {}
    finally:
        client.close()


def test_injected_transport_wins_over_hostile_env(hostile_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async def main():
        async with create_async_client(
            transport=httpx.MockTransport(handler), timeout=5.0
        ) as client:
            resp = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}

    asyncio.run(main())


def test_explicit_proxy_overrides_env(clean_env):
    client = create_client(timeout=5.0, proxy=FAKE_PROXY)
    try:
        patterns = {p.pattern for p in client._mounts}
        assert "all://" in patterns
    finally:
        client.close()


def test_explicit_bad_proxy_raises_valueerror(clean_env):
    with pytest.raises(ValueError):
        create_client(timeout=5.0, proxy="http://host:notaport")


# ---------------------------------------------------------------------------
# Adapter-level regression: the original failure sites
# ---------------------------------------------------------------------------
class _NullSecrets:
    async def get_secret(self, ref: str):
        return None


def test_pixabay_client_constructs_under_hostile_env(hostile_env):
    from app.providers.stock.pixabay import PixabayProvider

    provider = PixabayProvider(_NullSecrets())
    client = provider._ensure_client()
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        asyncio.run(client.aclose())


def test_llm_client_constructs_under_hostile_env(hostile_env):
    from app.providers.llm.base import BaseAdapter

    adapter = BaseAdapter(_NullSecrets(), "apikey/test")
    client = adapter._client(5.0)
    assert isinstance(client, httpx.AsyncClient)
