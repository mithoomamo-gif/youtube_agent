"""Live tests are opt-in: they are SKIPPED unless explicitly selected
with ``-m live`` (they need real network access to external services).

Harness-level proxy workaround (Round 3)
----------------------------------------
On this VM the egress-proxy environment contains bracketed IPv6 entries
in ``NO_PROXY``/``no_proxy`` (e.g. ``[::1]``). httpx (default
``trust_env=True``) parses those entries as URL patterns at
``httpx.AsyncClient`` construction time and raises
``httpx.InvalidURL: Invalid port: ':1]'`` *before any request is made* —
so every httpx-based live test (Gemini, Pixabay) fails at client
construction, not at the provider.

The shared HTTP layer may fix this properly later; until then this
conftest applies the minimal harness-only equivalent of the explicit
proxy handling in ``app/providers/tts/adapters.py`` (which passes the
proxy explicitly because libraries mishandle the env): strip the
brackets from bracketed IPv6 ``NO_PROXY`` entries, preserving every
entry and the proxy variables themselves untouched. httpx expects
unbracketed IPv6 hosts in patterns, and external API hosts are not in
``NO_PROXY`` at all, so routing semantics are unchanged — external
calls still go through ``*_proxy``, loopback still bypasses it.

This runs at conftest import time, before any test module creates an
httpx client. It mutates only this pytest process's environment.
"""

import os

import pytest


def _sanitize_no_proxy_env() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(var)
        if not raw:
            continue
        cleaned: list[str] = []
        for entry in raw.split(","):
            e = entry.strip()
            if not e:
                continue
            if e.startswith("[") and e.endswith("]"):
                e = e[1:-1]
            if e not in cleaned:
                cleaned.append(e)
        new = ",".join(cleaned)
        if new != raw:
            os.environ[var] = new


_sanitize_no_proxy_env()


def pytest_collection_modifyitems(config, items):
    markexpr = getattr(config.option, "markexpr", "") or ""
    if "live" in markexpr:
        return
    skip_live = pytest.mark.skip(
        reason="live tests are opt-in: run with `pytest -m live`")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
