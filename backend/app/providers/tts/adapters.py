"""Provider adapters.

* :class:`EdgeTTSAdapter` — DEFAULT, free, no key. Drives Microsoft Edge's
  online Read-Aloud service via the ``edge_tts`` library. NOTE: this is an
  UNOFFICIAL, reverse-engineered consumer endpoint — no SLA, no commercial
  licence grant, no published quota; Microsoft can change the protocol at
  any time. It sits behind the provider interface precisely so it can be
  swapped in one line (Azure exposes the identical voices).
* :class:`AzureSpeechAdapter` — SAME voice IDs as edge-tts (inaudible
  switch), $15 / 1M chars reference price, full SSML phoneme + custom
  lexicon support. Key via :class:`SecretProvider`.
* :class:`ElevenLabsAdapter` — API PAYG only. WARNING: Urdu is NOT in the
  Multilingual v2 language list; the Urdu fallback chain may include
  ElevenLabs ONLY when the voice profile's ``urdu_audition_passed`` flag is
  set (enforced in :mod:`fallback`, not here).
* :class:`GoogleTTSAdapter` / :class:`OpenAITTSAdapter` — interface stubs
  for the English/Hindi/Urdu chains. They raise :class:`ProviderUnavailable`
  cleanly; they never silently succeed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Any

from ..http_client import create_async_client
from .base import (ProviderError, ProviderUnavailable, SecretProvider,
                   TTSProvider, TTSRequest, TTSResult, VoiceInfo,
                   expected_duration_s)

# Default voices per locale. edge-tts and Azure share these IDs exactly.
DEFAULT_VOICES: dict[str, dict[str, str]] = {
    "ur-PK": {"male": "ur-PK-AsadNeural", "female": "ur-PK-UzmaNeural"},
    "hi-IN": {"male": "hi-IN-MadhurNeural", "female": "hi-IN-SwaraNeural"},
    "en-US": {"male": "en-US-GuyNeural", "female": "en-US-JennyNeural"},
    "en-GB": {"male": "en-GB-RyanNeural", "female": "en-GB-SoniaNeural"},
    "en-IN": {"male": "en-IN-PrabhatNeural", "female": "en-IN-NeerjaNeural"},
}


def _default_voice_for(locale: str | None) -> str | None:
    if not locale:
        return "en-US-GuyNeural"
    loc = locale if locale in DEFAULT_VOICES else None
    if loc is None:  # language-only like 'ur' -> first matching locale
        for key, voices in DEFAULT_VOICES.items():
            if key.split("-")[0] == locale.split("-")[0]:
                loc = key
                break
    if loc is None:
        loc = "en-US"
    return DEFAULT_VOICES[loc]["male"]


def _rate_str(rate_percent: float) -> str:
    return f"{rate_percent:+.0f}%"


def _pitch_str(pitch_hz: float) -> str:
    return f"{pitch_hz:+.0f}Hz"


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ProviderUnavailable("httpx is not installed") from exc
    return httpx


def _raise_for_status(resp, provider: str) -> None:
    if resp.status_code == 429 or resp.status_code >= 500:
        retry_after = resp.headers.get("Retry-After")
        raise ProviderError(
            f"{provider}: HTTP {resp.status_code}",
            retry_after_s=float(retry_after) if retry_after else None,
            status_code=resp.status_code)
    if resp.status_code in (401, 403, 404):
        raise ProviderUnavailable(
            f"{provider}: HTTP {resp.status_code} (auth/quota/voice) — escalating")
    if resp.status_code >= 400:
        raise ProviderError(f"{provider}: HTTP {resp.status_code}",
                            status_code=resp.status_code)


# ---------------------------------------------------------------------------
# edge-tts
# ---------------------------------------------------------------------------
_EDGE_WSS_HOST = "speech.platform.bing.com"


def _is_ws_handshake_error(exc: BaseException) -> bool:
    try:
        from aiohttp.client_exceptions import WSServerHandshakeError
        return isinstance(exc, WSServerHandshakeError)
    except Exception:
        return False


def _ws_send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Send one masked WebSocket frame (client frames must be masked)."""
    mask = os.urandom(4)
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


class _WSFrameReader:
    """Minimal WebSocket frame reader: assembles fragments, answers pings."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self.buf = bytearray()

    def _need(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ProviderError("edge raw fallback: connection closed")
            self.buf += chunk

    def read_message(self) -> tuple[int, bytes]:
        payload = bytearray()
        opcode: int | None = None
        while True:
            self._need(2)
            b0, b1 = self.buf[0], self.buf[1]
            del self.buf[:2]
            fin, op = b0 & 0x80, b0 & 0x0F
            masked, length = b1 & 0x80, b1 & 0x7F
            if length == 126:
                self._need(2)
                length = struct.unpack(">H", self.buf[:2])[0]
                del self.buf[:2]
            elif length == 127:
                self._need(8)
                length = struct.unpack(">Q", self.buf[:8])[0]
                del self.buf[:8]
            mask = b""
            if masked:
                self._need(4)
                mask = bytes(self.buf[:4])
                del self.buf[:4]
            self._need(length)
            data = bytes(self.buf[:length])
            del self.buf[:length]
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 9:                       # ping -> pong, keep reading
                _ws_send_frame(self._sock, 10, data)
                continue
            if op == 8:                       # close
                return 8, bytes(payload)
            if opcode is None:
                opcode = op
            payload += data
            if fin:
                return opcode, bytes(payload)


def _raw_edge_synthesize(req: TTSRequest, proxy_url: str | None,
                         ) -> tuple[bytes, list[dict[str, Any]], float]:
    """Blocking raw-socket synthesis against the edge-tts endpoint.

    Fallback for TLS-intercepting proxies that re-originate the WebSocket
    handshake and relay Microsoft's 101 with ``Connection: close`` (and
    their own Sec-WebSocket-Accept), which aiohttp's strict handshake
    validation rejects even though frames forward fine. Speaks the same
    protocol as the ``edge_tts`` library, reusing its DRM token and SSML
    builders; only the transport is hand-rolled and tolerant of the
    relayed handshake headers. Returns (audio_bytes, boundaries, duration_s).
    """
    import html

    import edge_tts
    import edge_tts.communicate as comm_mod
    from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
    from edge_tts.drm import DRM

    raw = None
    if proxy_url:
        proxy = urllib.parse.urlparse(proxy_url)
        raw = socket.create_connection((proxy.hostname, proxy.port), timeout=30)
        auth = ""
        if proxy.username:
            cred = f"{urllib.parse.unquote(proxy.username)}:" \
                   f"{urllib.parse.unquote(proxy.password or '')}"
            auth = "Proxy-Authorization: Basic " + base64.b64encode(
                cred.encode()).decode() + "\r\n"
        raw.sendall((f"CONNECT {_EDGE_WSS_HOST}:443 HTTP/1.1\r\n"
                     f"Host: {_EDGE_WSS_HOST}:443\r\n{auth}"
                     "Proxy-Connection: Keep-Alive\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk:
                raise ProviderError("edge raw fallback: proxy closed CONNECT")
            resp += chunk
        if b" 200 " not in resp.split(b"\r\n", 1)[0]:
            raise ProviderError(
                f"edge raw fallback: proxy CONNECT failed: "
                f"{resp.split(chr(13).encode(), 1)[0]!r}")
    else:
        raw = socket.create_connection((_EDGE_WSS_HOST, 443), timeout=30)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=_EDGE_WSS_HOST)
    sock.settimeout(120)
    try:
        text = req.ssml_text or req.text
        communicate = edge_tts.Communicate(
            text, req.voice_id, rate=_rate_str(req.rate_percent),
            pitch=_pitch_str(req.pitch_hz))
        cid = comm_mod.connect_id()
        url = (f"{WSS_URL}&ConnectionId={cid}"
               f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
               f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
        parts_url = urllib.parse.urlparse(url)
        key = base64.b64encode(os.urandom(16)).decode()
        headers = DRM.headers_with_muid(dict(WSS_HEADERS))
        lines = [f"GET {parts_url.path}?{parts_url.query} HTTP/1.1",
                 f"Host: {_EDGE_WSS_HOST}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ProviderError("edge raw fallback: closed at handshake")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status_line:
            raise ProviderError(
                f"edge raw fallback: handshake failed: {status_line}")
        reader = _WSFrameReader(sock)
        reader.buf += rest

        config = (
            f"X-Timestamp:{comm_mod.date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')
        _ws_send_frame(sock, 1, config.encode())

        audio = bytearray()
        boundaries: list[dict[str, Any]] = []
        duration_s = 0.0
        compensation_ticks = 0
        text_parts = list(comm_mod.split_text_by_byte_length(
            text.encode("utf-8"), 4096)) or [text.encode("utf-8")]
        for part in text_parts:
            part_audio_start = len(audio)
            ssml = comm_mod.ssml_headers_plus_data(
                cid, comm_mod.date_to_string(),
                comm_mod.mkssml(communicate.tts_config, part))
            _ws_send_frame(sock, 1, ssml.encode())
            while True:
                opcode, payload = reader.read_message()
                if opcode == 8:
                    raise ProviderError(
                        "edge raw fallback: server closed the stream")
                if opcode == 1:
                    message = payload.decode("utf-8", errors="replace")
                    if "Path:turn.end" in message:
                        break
                    if "Path:audio.metadata" in message:
                        try:
                            meta = json.loads(message.split("\r\n\r\n", 1)[1])
                            for entry in meta.get("Metadata", []):
                                if entry.get("Type") != "WordBoundary":
                                    continue
                                d = entry["Data"]
                                offset = (d["Offset"] + compensation_ticks) / 10_000_000
                                dur = d["Duration"] / 10_000_000
                                duration_s = max(duration_s, offset + dur)
                                boundaries.append({
                                    "text": html.unescape(
                                        d.get("text", {}).get("Text", "")),
                                    "start_s": offset, "end_s": offset + dur})
                        except Exception:
                            pass
                elif opcode == 2 and len(payload) >= 2:
                    header_len = int.from_bytes(payload[:2], "big")
                    audio += payload[2 + header_len:]
            # Mirror edge_tts: inter-part offset from CBR bytes (48 kbps).
            part_bytes = len(audio) - part_audio_start
            compensation_ticks += part_bytes * 8 * 10_000_000 // 48_000
        try:
            _ws_send_frame(sock, 8, b"")
        except Exception:
            pass
        return bytes(audio), boundaries, duration_s
    finally:
        try:
            sock.close()
        except Exception:
            pass


class EdgeTTSAdapter(TTSProvider):
    """Free default provider (unofficial endpoint, no SLA — see module
    docstring). Uses the ``edge_tts`` library API, wrapped async."""

    name = "edge"
    model = "edge-tts"
    cost_per_1k_chars = 0.0
    max_concurrency = 4

    def __init__(self, proxy: str | None = None):
        # Explicit proxy wins; otherwise honour the standard *_proxy env
        # vars (a proxy URL is not a secret and is never logged). Direct
        # connection when neither is set.
        self._proxy = proxy

    def _proxy_url(self) -> str | None:
        if self._proxy:
            return self._proxy
        for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
            value = os.environ.get(var)
            if value:
                return value
        return None

    @staticmethod
    def _patch_edge_tts_ssl_context() -> None:
        """edge_tts pins a certifi-only SSL context (``_SSL_CTX`` in its
        communicate/voices modules) and passes it explicitly, overriding
        any connector context — so CAs that exist only in the OS store /
        ``SSL_CERT_FILE`` (e.g. a corporate egress proxy's CA) fail
        verification. When ``SSL_CERT_FILE`` is set, repoint edge_tts at a
        context built from the default trust store (OS + env). No-op
        otherwise, keeping stock certifi behaviour."""
        if not os.environ.get("SSL_CERT_FILE"):
            return
        try:
            import edge_tts.communicate as _comm
            _comm._SSL_CTX = ssl.create_default_context()
        except Exception:
            pass
        try:
            import edge_tts.voices as _voices
            _voices._SSL_CTX = ssl.create_default_context()
        except Exception:
            pass

    @staticmethod
    def _connector():
        """aiohttp connector trusting the OS CA store (``SSL_CERT_FILE`` /
        system bundle) instead of certifi alone, so TLS-intercepting
        corporate egress proxies whose CA is installed system-wide still
        verify. A fresh connector per call; edge_tts closes it with its
        session. Returns None (aiohttp default) if aiohttp is missing."""
        try:
            import aiohttp
            return aiohttp.TCPConnector(ssl=ssl.create_default_context())
        except Exception:
            return None

    def default_voice(self, locale: str | None) -> str | None:
        return _default_voice_for(locale)

    async def canary(self, locale: str = "en-US",
                     voice_id: str | None = None) -> bool:
        """REAL one-line synthesis check against Microsoft's speech
        endpoint (not a stub): True only if a live ``synthesize`` returns
        non-empty audio with a positive duration."""
        voice = voice_id or self.default_voice(locale)
        if not voice:
            return False
        try:
            result = await self.synthesize(TTSRequest(
                text="Hello.", voice_id=voice,
                language=(locale or "en").split("-")[0]))
        except Exception:
            return False
        return bool(result.audio_bytes) and result.duration_s > 0

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        try:
            import edge_tts
        except ImportError as exc:
            raise ProviderUnavailable("edge_tts library is not installed") from exc
        self._patch_edge_tts_ssl_context()
        communicate = edge_tts.Communicate(
            req.ssml_text or req.text, req.voice_id,
            rate=_rate_str(req.rate_percent), pitch=_pitch_str(req.pitch_hz),
            proxy=self._proxy_url(), connector=self._connector())
        audio = bytearray()
        boundaries: list[dict[str, Any]] = []
        duration_s = 0.0
        try:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    offset = chunk["offset"] / 10_000_000
                    dur = chunk["duration"] / 10_000_000
                    duration_s = max(duration_s, offset + dur)
                    boundaries.append({"text": chunk["text"],
                                       "start_s": offset, "end_s": offset + dur})
        except Exception as exc:
            if not _is_ws_handshake_error(exc):
                raise ProviderError(f"edge: synthesis failed: {exc}") from exc
            # aiohttp rejected the (proxy-relayed) 101 handshake — retry
            # over the tolerant raw-socket transport (see _raw_edge_synthesize).
            try:
                raw_audio, boundaries, duration_s = await asyncio.to_thread(
                    _raw_edge_synthesize, req, self._proxy_url())
            except Exception as raw_exc:
                raise ProviderError(
                    f"edge: synthesis failed (raw fallback): {raw_exc}"
                ) from raw_exc
            audio = bytearray(raw_audio)
        estimated = duration_s == 0.0
        if estimated:
            duration_s = expected_duration_s(req.text, req.rate_percent)
        return TTSResult(
            provider=self.name, model=self.model, voice_id=req.voice_id,
            billed_chars=len(req.text), duration_s=duration_s,
            audio_bytes=bytes(audio), cost_usd=0.0,
            duration_estimated=estimated, word_timestamps=boundaries)

    async def list_voices(self, locale: str | None = None) -> list[VoiceInfo]:
        try:
            import edge_tts
        except ImportError as exc:
            raise ProviderUnavailable("edge_tts library is not installed") from exc
        self._patch_edge_tts_ssl_context()
        voices = await edge_tts.list_voices(
            proxy=self._proxy_url(), connector=self._connector())
        out = []
        for v in voices:
            if locale:
                loc = v["Locale"].lower()
                want = locale.lower()
                if loc != want and not loc.startswith(want.split("-")[0] + "-"):
                    continue
            out.append(VoiceInfo(voice_id=v["ShortName"], locale=v["Locale"],
                                 gender=v.get("Gender"), name=v.get("FriendlyName"),
                                 provider=self.name))
        return out


# ---------------------------------------------------------------------------
# Azure AI Speech
# ---------------------------------------------------------------------------
class AzureSpeechAdapter(TTSProvider):
    """Azure AI Speech. Same voice IDs as edge-tts; $15/1M chars reference;
    full SSML <phoneme> + custom lexicon support."""

    name = "azure"
    model = "azure-neural"
    cost_per_1k_chars = 0.015          # $15 / 1M chars
    max_concurrency = 8

    def __init__(self, secrets: SecretProvider, key_ref: str,
                 region: str = "eastus", *, client=None):
        self._secrets = secrets
        self._key_ref = key_ref
        self.region = region
        self._client = client          # injectable httpx.AsyncClient (tests)

    def default_voice(self, locale: str | None) -> str | None:
        return _default_voice_for(locale)

    @property
    def _endpoint(self) -> str:
        return f"https://{self.region}.tts.speech.microsoft.com"

    async def _key(self) -> str:
        key = await self._secrets.get_secret(self._key_ref)
        if not key:
            raise ProviderUnavailable("azure: API key not available")
        return key

    def _ssml(self, req: TTSRequest) -> str:
        # voice ids like ur-PK-AsadNeural -> xml:lang ur-PK
        parts = req.voice_id.split("-")
        locale = "-".join(parts[:2]) if len(parts) >= 2 else "en-US"
        body = req.ssml_text
        if body is None:
            from xml.sax.saxutils import escape
            body = escape(req.text)
        return (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="{locale}"><voice name="{req.voice_id}">'
            f'<prosody rate="{_rate_str(req.rate_percent)}" '
            f'pitch="{_pitch_str(req.pitch_hz)}">{body}</prosody>'
            "</voice></speak>")

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        key = await self._key()
        httpx = _httpx()
        headers = {"Ocp-Apim-Subscription-Key": key,
                   "Content-Type": "application/ssml+xml",
                   "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3"}
        url = f"{self._endpoint}/cognitiveservices/v1"
        client = self._client or create_async_client(timeout=120)
        close = self._client is None
        try:
            resp = await client.post(url, content=self._ssml(req).encode("utf-8"),
                                     headers=headers)
        except Exception as exc:
            raise ProviderError(f"azure: request failed: {exc}") from exc
        finally:
            if close:
                await client.aclose()
        _raise_for_status(resp, "azure")
        # SSML markup chars are billable on Azure; billed = spoken text len
        # (layer strips markup before estimating — see roadmap section 30).
        billed = len(req.text)
        return TTSResult(
            provider=self.name, model=req.model or self.model,
            voice_id=req.voice_id, billed_chars=billed,
            duration_s=expected_duration_s(req.text, req.rate_percent),
            audio_bytes=resp.content, cost_usd=self.estimate_cost(billed),
            duration_estimated=True)

    async def list_voices(self, locale: str | None = None) -> list[VoiceInfo]:
        key = await self._key()
        httpx = _httpx()
        url = f"{self._endpoint}/cognitiveservices/voices/list"
        client = self._client or create_async_client(timeout=60)
        close = self._client is None
        try:
            resp = await client.get(
                url, headers={"Ocp-Apim-Subscription-Key": key})
        finally:
            if close:
                await client.aclose()
        _raise_for_status(resp, "azure")
        out = []
        for v in resp.json():
            if locale and v.get("Locale", "").lower() != locale.lower():
                continue
            out.append(VoiceInfo(voice_id=v["ShortName"], locale=v["Locale"],
                                 gender=v.get("Gender"), name=v.get("DisplayName"),
                                 provider=self.name))
        return out


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------
class ElevenLabsAdapter(TTSProvider):
    """ElevenLabs, API PAYG only.

    Pricing (API rates): Flash/Turbo $0.04/1k chars; Multilingual v2/v4
    $0.08/1k chars. Pronunciation dictionaries (alias + IPA) are attached per
    request; the with-timestamps endpoint supplies character alignment used
    for subtitle timestamps.

    WARNING: Urdu is NOT in the Multilingual v2 language list. Urdu fallback
    chains include this adapter ONLY when the voice profile carries
    ``urdu_audition_passed=True`` (see fallback.resolve_chain).
    """

    name = "elevenlabs"
    max_concurrency = 4
    MODEL_COSTS = {                     # USD per 1k chars, API PAYG
        "eleven_flash_v2_5": 0.04,
        "eleven_turbo_v2_5": 0.04,
        "eleven_multilingual_v2": 0.08,
        "eleven_v4": 0.08,
    }

    def __init__(self, secrets: SecretProvider, key_ref: str,
                 model: str = "eleven_flash_v2_5", *, client=None):
        self._secrets = secrets
        self._key_ref = key_ref
        self.model = model
        self.cost_per_1k_chars = self.MODEL_COSTS.get(model, 0.08)
        self._client = client

    def default_voice(self, locale: str | None) -> str | None:
        return None  # ElevenLabs voices are account-specific IDs.

    async def _key(self) -> str:
        key = await self._secrets.get_secret(self._key_ref)
        if not key:
            raise ProviderUnavailable("elevenlabs: API key not available")
        return key

    async def subscription_info(self, *, timeout_s: float = 10.0) -> dict:
        """Cheapest authenticated call: GET /v1/user/subscription (§28).

        Doubles as the character-balance fetch for the usage meter, so
        it returns the plan tier plus characters used / allowed. Unlike
        :meth:`synthesize`, HTTP failures raise ``httpx.HTTPStatusError``
        (via ``raise_for_status``) and transport failures propagate as
        ``httpx`` exceptions — the test-connection caller maps them onto
        the §28 error taxonomy and needs the real status code.
        """
        key = await self._key()
        httpx = _httpx()
        client = self._client or create_async_client(timeout=timeout_s)
        close = self._client is None
        try:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": key})
        finally:
            if close:
                await client.aclose()
        resp.raise_for_status()
        data = resp.json() or {}
        return {
            "tier": data.get("tier"),
            "character_count": data.get("character_count"),
            "character_limit": data.get("character_limit"),
            "next_character_count_reset_unix": data.get(
                "next_character_count_reset_unix"),
        }

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        key = await self._key()
        httpx = _httpx()
        model = req.model or self.model
        body: dict[str, Any] = {
            "text": req.text,
            "model_id": model,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        # Speed is the only rate control (0.7-1.2); no pitch control.
        speed = min(1.2, max(0.7, 1.0 + req.rate_percent / 100.0))
        body["voice_settings"]["speed"] = speed
        if req.pronunciation_dictionary:
            body["pronunciation_dictionary_locators"] = [
                req.pronunciation_dictionary]
        url = (f"https://api.elevenlabs.io/v1/text-to-speech/"
               f"{req.voice_id}/with-timestamps")
        client = self._client or create_async_client(timeout=180)
        close = self._client is None
        try:
            resp = await client.post(url, json=body,
                                     headers={"xi-api-key": key})
        except Exception as exc:
            raise ProviderError(f"elevenlabs: request failed: {exc}") from exc
        finally:
            if close:
                await client.aclose()
        _raise_for_status(resp, "elevenlabs")
        data = resp.json()
        audio = base64.b64decode(data.get("audio_base64", ""))
        alignment = data.get("alignment") or {}
        starts = alignment.get("character_start_times_seconds") or []
        ends = alignment.get("character_end_times_seconds") or []
        duration = ends[-1] if ends else expected_duration_s(req.text, req.rate_percent)
        timestamps = []
        chars = alignment.get("characters") or []
        for i, ch in enumerate(chars):
            if i < len(starts):
                timestamps.append({"text": ch, "start_s": starts[i],
                                   "end_s": ends[i] if i < len(ends) else starts[i]})
        billed = len(req.text)
        cost_rate = self.MODEL_COSTS.get(model, self.cost_per_1k_chars)
        return TTSResult(
            provider=self.name, model=model, voice_id=req.voice_id,
            billed_chars=billed, duration_s=duration, audio_bytes=audio,
            cost_usd=billed / 1000.0 * cost_rate,
            duration_estimated=not bool(ends), word_timestamps=timestamps)

    async def list_voices(self, locale: str | None = None) -> list[VoiceInfo]:
        key = await self._key()
        httpx = _httpx()
        client = self._client or create_async_client(timeout=60)
        close = self._client is None
        try:
            resp = await client.get("https://api.elevenlabs.io/v1/voices",
                                    headers={"xi-api-key": key})
        finally:
            if close:
                await client.aclose()
        _raise_for_status(resp, "elevenlabs")
        out = []
        for v in resp.json().get("voices", []):
            labels = v.get("labels") or {}
            out.append(VoiceInfo(voice_id=v["voice_id"], locale=labels.get("language", ""),
                                 gender=labels.get("gender"), name=v.get("name"),
                                 provider=self.name))
        return out


# ---------------------------------------------------------------------------
# Stubs — clean ProviderUnavailable, never silent success
# ---------------------------------------------------------------------------
class _StubAdapter(TTSProvider):
    implemented = False
    stub_reason = "adapter not implemented yet"

    async def synthesize(self, req: TTSRequest) -> TTSResult:
        raise ProviderUnavailable(f"{self.name}: {self.stub_reason}")

    async def list_voices(self, locale: str | None = None) -> list[VoiceInfo]:
        raise ProviderUnavailable(f"{self.name}: {self.stub_reason}")

    async def canary(self, locale: str = "en-US", voice_id: str | None = None) -> bool:
        return False


class GoogleTTSAdapter(_StubAdapter):
    """Google Cloud TTS stub. Urdu is catalogued as ur-IN (Standard tier);
    Hindi/English have premium tiers. Full adapter lands in Phase 2 —
    until then it is in the chains as data but always unavailable."""

    name = "google"
    model = "google-neural2"
    cost_per_1k_chars = 0.016          # Neural2 $16/1M reference
    max_concurrency = 8
    stub_reason = "Google TTS adapter is a Phase-2 stub"


class OpenAITTSAdapter(_StubAdapter):
    """OpenAI gpt-4o-mini-tts stub (English steerable option). Model-churn
    watch in roadmap section 30: verify model status before Phase 1 locks."""

    name = "openai"
    model = "gpt-4o-mini-tts"
    cost_per_1k_chars = 0.015
    max_concurrency = 8
    stub_reason = "OpenAI TTS adapter is a stub pending model-status check"
