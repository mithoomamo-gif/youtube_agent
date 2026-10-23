"""Shared provider HTTP client factory — the ONE place provider code
builds httpx clients.

Why this module exists (Round 3 bug)
------------------------------------
``httpx.AsyncClient()`` with its default ``trust_env=True`` parses the
process environment at construction time via
``httpx._utils.get_environment_proxies()``. That function mishandles
``NO_PROXY`` entries that are *bracketed* IPv6 literals (e.g. ``[::1]``,
``[fd8b:…::1]`` — both forms are common, and this sandbox exports bare
AND bracketed forms side by side):

* bare ``::1``      -> correctly keyed ``all://[::1]``
* bracketed ``[::1]`` -> ``is_ipv6_hostname("[::1]")`` is False (brackets
  are not part of an ``IPv6Address``), so the entry falls through to the
  domain-wildcard branch and is keyed ``all://*[::1]``.

``URLPattern("all://*[::1]")`` then makes httpx's URL parser read ``:1]``
as a port, and EVERY default client construction dies with::

    httpx.InvalidURL: Invalid port: ':1]'

— before any request is attempted. Unsetting ``*_PROXY`` does not help:
the poisoned entries come from ``NO_PROXY``/``no_proxy``, which httpx
processes even when no proxy is configured.

Fix strategy (mirrors the Round 2 edge-tts adapter pattern in
``tts/adapters.py``: explicit proxy passthrough + OS trust store)
------------------------------------------------------------------
* ``trust_env=False`` ALWAYS — httpx never parses the raw environment.
* The proxy configuration is read explicitly from the environment by
  THIS module, sanitised entry by entry: bracketed IPv6 ``NO_PROXY``
  entries are unbracketed and re-keyed correctly, duplicates collapse,
  and any entry or proxy URL that does not validate is SKIPPED rather
  than crashing construction. Valid proxies are passed to httpx as
  per-scheme transports plus ``None`` bypass mounts for ``NO_PROXY``.
* TLS verification uses ``ssl.create_default_context()`` (the OS trust
  store, honouring ``SSL_CERT_FILE``) whenever ``SSL_CERT_FILE`` is set
  — TLS-intercepting egress proxies whose CA lives only in the OS store
  fail certifi-only verification otherwise. When it is not set, httpx's
  default (certifi) verification is kept, so behaviour on a normal
  machine is unchanged.
* On a machine with no proxy variables at all this is a no-op: the
  factory returns a plain direct client. No proxy address is hardcoded.
* An explicitly injected ``transport`` (tests / MockTransport) or an
  explicitly injected client always wins and bypasses env handling
  entirely — the same precedence httpx itself applies.

Proxy URLs may embed credentials; they are treated like secrets here:
never logged, never included in error messages.
"""

from __future__ import annotations

import ipaddress
import os
import ssl
from typing import Mapping

import httpx

__all__ = [
    "create_async_client",
    "create_client",
    "environment_proxy_mounts",
    "resolve_verify",
    "sanitise_no_proxy_entry",
    "sanitise_proxy_url",
]

_PROXY_SCHEMES = ("http", "https", "all")


# ---------------------------------------------------------------------------
# Sanitised environment parsing (pure helpers — unit-testable, no network)
# ---------------------------------------------------------------------------
def _env_get(environ: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive env lookup; the lowercase spelling wins, matching
    ``urllib.request.getproxies`` convention."""
    return environ.get(name.lower()) or environ.get(name.upper())


def _is_ipv4(host: str) -> bool:
    try:
        ipaddress.IPv4Address(host.split("/")[0])
    except Exception:
        return False
    return True


def _is_ipv6(host: str) -> bool:
    try:
        ipaddress.IPv6Address(host.split("/")[0])
    except Exception:
        return False
    return True


def _valid_mount_key(key: str) -> bool:
    """True iff httpx can parse ``key`` as a mount pattern — this is the
    exact operation that raised ``InvalidURL: Invalid port: ':1]'``."""
    if "*[" in key:  # the bug shape: wildcard glued to a bracketed IPv6
        return False
    try:
        url = httpx.URL(key)
        _ = url.port  # accessing .port forces port validation
        return bool(url.host)
    except Exception:
        return False


def sanitise_proxy_url(raw: str | None) -> str | None:
    """Validate one proxy URL from the environment.

    Returns the URL string to hand to httpx, or ``None`` when the value
    is missing/malformed — a bad env value must never crash client
    construction. Only http/https proxies are honoured (httpx's built-in
    transports)."""
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    try:
        url = httpx.URL(candidate)
        _ = url.port
        if url.scheme not in ("http", "https") or not url.host:
            return None
        httpx.Proxy(url=candidate)  # validates auth/host shape fully
    except Exception:
        return None
    return candidate


def sanitise_no_proxy_entry(entry: str) -> str | None:
    """Map ONE ``NO_PROXY`` entry to an httpx mount key, or ``None`` to
    skip it. Mirrors ``httpx._utils.get_environment_proxies`` semantics
    with the bracketed-IPv6 bug fixed:

    >>> sanitise_no_proxy_entry("::1")
    'all://[::1]'
    >>> sanitise_no_proxy_entry("[::1]")   # the entry that crashed httpx
    'all://[::1]'
    >>> sanitise_no_proxy_entry("example.com")
    'all://*example.com'
    >>> sanitise_no_proxy_entry(".google.com")
    'all://*.google.com'
    >>> sanitise_no_proxy_entry("*")
    None
    """
    e = (entry or "").strip()
    if not e or e == "*":  # "*" = bypass everything; handled by caller
        return None
    if "://" in e:  # full-URL form: httpx uses it verbatim as the key
        return e if _valid_mount_key(e) else None

    host, port = e, None
    if e.startswith("["):
        end = e.find("]")
        if end == -1:
            return None
        host = e[1:end]
        rest = e[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                return None
            port = rest[1:]
        suffix = f":{port}" if port else ""
        if _is_ipv6(host):
            key = f"all://[{host}]{suffix}"
        elif _is_ipv4(host) or host.lower() == "localhost":
            key = f"all://{host}{suffix}"
        else:  # bracketed domain — nonsense, but keep it literal/safe
            key = f"all://{host}{suffix}"
        return key if _valid_mount_key(key) else None

    if _is_ipv6(e):  # bare IPv6, e.g. ::1 or fd8b:…::1 (no port possible)
        key = f"all://[{e}]"
    elif _is_ipv4(e):  # IPv4 or CIDR, optionally with :port — kept whole
        key = f"all://{e}"
    elif e.lower() == "localhost" or e.lower().startswith("localhost:"):
        key = f"all://{e}"
    elif e.startswith("*."):
        key = f"all://{e}"  # already the subdomain-wildcard form
    elif e.startswith("."):
        key = f"all://*{e}"  # .google.com -> *.google.com (httpx parity)
    else:
        key = f"all://*{e}"  # domain (+ optional :port) — httpx parity
    return key if _valid_mount_key(key) else None


def environment_proxy_mounts(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Sanitised equivalent of ``httpx.get_environment_proxies()``.

    Returns ``{mount_key: proxy_url}`` for proxied schemes and
    ``{mount_key: None}`` for ``NO_PROXY`` bypasses. Returns ``{}`` when
    no usable proxy is configured, when ``NO_PROXY`` contains ``*``
    (bypass all), or when every proxy value was malformed. Every key is
    guaranteed parseable by httpx — this function never raises."""
    env: Mapping[str, str] = os.environ if environ is None else environ

    proxies: dict[str, str] = {}
    for scheme in _PROXY_SCHEMES:
        url = sanitise_proxy_url(_env_get(env, f"{scheme}_proxy"))
        if url:
            proxies[f"{scheme}://"] = url
    if not proxies:
        return {}  # bypass entries are meaningless without a proxy

    no_proxy_raw = _env_get(env, "no_proxy") or ""
    entries = [part.strip() for part in no_proxy_raw.split(",")]
    if any(part == "*" for part in entries):
        return {}  # NO_PROXY=* disables proxying entirely (httpx parity)

    mounts: dict[str, str | None] = dict(proxies)
    for entry in entries:
        key = sanitise_no_proxy_entry(entry)
        if key:
            mounts[key] = None
    return mounts


def resolve_verify(
    environ: Mapping[str, str] | None = None,
) -> ssl.SSLContext | bool:
    """TLS verification for provider clients.

    When ``SSL_CERT_FILE`` is set, build the context from the OS/default
    trust store (which honours that variable) so CAs installed only
    system-wide — e.g. a TLS-intercepting egress proxy's CA — verify.
    This is the same pattern as ``tts.adapters._connector``. Otherwise
    return ``True`` (httpx's certifi default): a no-op on normal
    machines."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    if env.get("SSL_CERT_FILE"):
        return ssl.create_default_context()
    return True


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------
def _proxy_mounts(
    *,
    environ: Mapping[str, str] | None,
    explicit_proxy: str | None,
    verify: ssl.SSLContext | str | bool,
    http2: bool,
    async_: bool,
) -> dict[str, object] | None:
    """Build the httpx ``mounts`` mapping, or ``None`` for a direct
    client. Proxy mounts carry real transports; bypass mounts are
    ``None`` (httpx: fall back to the client's direct transport)."""
    if explicit_proxy is not None:
        url = sanitise_proxy_url(explicit_proxy)
        if url is None:
            raise ValueError("explicit proxy is not a valid http(s) proxy URL")
        spec: dict[str, str | None] = {"all://": url}
        # An explicit proxy still honours env NO_PROXY bypasses (read
        # directly: environment_proxy_mounts() is empty when the env
        # itself has no proxy configured).
        env: Mapping[str, str] = os.environ if environ is None else environ
        no_proxy_raw = _env_get(env, "no_proxy") or ""
        for part in (p.strip() for p in no_proxy_raw.split(",")):
            if part == "*":
                return None  # bypass everything -> direct client
            key = sanitise_no_proxy_entry(part)
            if key:
                spec.setdefault(key, None)
    else:
        spec = environment_proxy_mounts(environ)
    if not spec:
        return None
    transport_cls = httpx.AsyncHTTPTransport if async_ else httpx.HTTPTransport
    mounts: dict[str, object] = {}
    for key, url in spec.items():
        mounts[key] = (
            None
            if url is None
            else transport_cls(
                proxy=httpx.Proxy(url=url),
                verify=verify,
                trust_env=False,
                http2=http2,
            )
        )
    return mounts


def create_async_client(
    *,
    timeout: float | httpx.Timeout | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    verify: ssl.SSLContext | str | bool | None = None,
    proxy: str | None = None,
    environ: Mapping[str, str] | None = None,
    **kwargs,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` that survives hostile proxy envs.

    Drop-in replacement for ``httpx.AsyncClient(...)`` in provider code:
    same ``timeout``/``transport``/``base_url``/… semantics, but
    ``trust_env`` is forced off and the environment's proxy config is
    applied in sanitised form (see module docstring). ``trust_env``,
    ``mounts`` and ``proxy`` passed via ``kwargs`` are overridden —
    proxying is owned by this factory (pass ``proxy=`` explicitly to
    override the environment)."""
    kwargs.pop("trust_env", None)
    kwargs.pop("mounts", None)
    kwargs.pop("proxy", None)
    kwargs.pop("proxies", None)
    resolved_verify = verify if verify is not None else resolve_verify(environ)
    if timeout is not None:
        kwargs["timeout"] = timeout
    if transport is not None:
        # Injected transport (tests, MockTransport) wins outright; httpx
        # itself likewise ignores env proxies when a transport is given.
        return httpx.AsyncClient(
            transport=transport, verify=resolved_verify,
            trust_env=False, **kwargs,
        )
    http2 = bool(kwargs.pop("http2", False))
    mounts = _proxy_mounts(
        environ=environ, explicit_proxy=proxy, verify=resolved_verify,
        http2=http2, async_=True,
    )
    return httpx.AsyncClient(
        verify=resolved_verify, trust_env=False, http2=http2,
        mounts=mounts, **kwargs,
    )


def create_client(
    *,
    timeout: float | httpx.Timeout | None = None,
    transport: httpx.BaseTransport | None = None,
    verify: ssl.SSLContext | str | bool | None = None,
    proxy: str | None = None,
    environ: Mapping[str, str] | None = None,
    **kwargs,
) -> httpx.Client:
    """Sync twin of :func:`create_async_client`."""
    kwargs.pop("trust_env", None)
    kwargs.pop("mounts", None)
    kwargs.pop("proxy", None)
    kwargs.pop("proxies", None)
    resolved_verify = verify if verify is not None else resolve_verify(environ)
    if timeout is not None:
        kwargs["timeout"] = timeout
    if transport is not None:
        return httpx.Client(
            transport=transport, verify=resolved_verify,
            trust_env=False, **kwargs,
        )
    http2 = bool(kwargs.pop("http2", False))
    mounts = _proxy_mounts(
        environ=environ, explicit_proxy=proxy, verify=resolved_verify,
        http2=http2, async_=False,
    )
    return httpx.Client(
        verify=resolved_verify, trust_env=False, http2=http2,
        mounts=mounts, **kwargs,
    )
