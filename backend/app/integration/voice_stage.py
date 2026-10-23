"""ROUND 2 integration — VOICE stage (Worker 3).

Real edge-tts synthesis per chapter with a per-chapter silence/tone stub
fallback, chapter-WAV caching, narration assembly, a locally generated
music bed, and a −14 LUFS mix/master.

Contract (shared with the sibling integration workers)::

    synthesize_voice(bundle, profile, ctx) -> VoiceResult

* ``bundle`` — a ``content.ContentBundle``-shaped object: ``.chapters[i].text``
  and ``.narration_text``.  Duck-typed (getattr / mapping access) so dicts,
  dataclasses and Pydantic models all work.
* ``profile`` — a niche/voice profile: read duck-typed for ``voice_id`` /
  ``voice_style`` (hint), ``language``, ``target_wpm`` and the niche id
  (``profile_id`` / ``niche``).  Works with ``NicheProfile``, the TTS
  layer's ``VoiceProfile``, plain objects and dicts.
* ``ctx`` — the integration ``StageContext``; only ``.audio_dir`` is used.

Synthesis strategy per chapter (chapter = one continuity unit):

1. Cache: a chapter WAV named with the SHA-256 of (text, voice, rate,
   pitch) plus a sidecar JSON is reused verbatim when present.
2. REAL: the providers layer first — ``EdgeTTSAdapter``
   (``app.providers.tts``) with a short timeout.  The adapter's aiohttp
   transport hard-verifies the WebSocket ``Connection: Upgrade`` response
   header; some egress proxies / TLS middleboxes rewrite that header to
   ``close`` on an otherwise valid ``101`` handshake, which makes the
   stock transport unusable behind them.  For exactly that case this
   module carries a small raw-socket WebSocket transport speaking the
   same edge-tts protocol (same DRM token, SSML and message parsing,
   reusing ``edge_tts``' own helpers) which validates the 101 status and
   Sec-WebSocket-Accept semantics itself.  It honours the standard
   ``https_proxy`` / ``SSL_CERT_FILE`` environment when present and is a
   no-op difference on a direct connection (e.g. the user's Windows PC).
   Either transport succeeding counts as the real ``edge-tts`` path.
3. STUB: estimated-duration soft-tone WAV (words / WPM).  A literal
   0-volume file would measure −inf LUFS and break loudnorm downstream,
   so the placeholder tone is very soft (≈ −30 dBFS, 196 Hz) rather than
   digital silence — still unmistakably a placeholder, but the render /
   subtitle / loudnorm stages all work against realistic durations.

``path_used`` is ``"edge-tts"`` when every chapter is real, ``"mixed"``
when some chapters fell back, and ``"stub-silence"`` when none did.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from app.data_dir import data_dir_layout
from app.ffmpeg_resolution import ffmpeg_executable, ffprobe_executable

__all__ = ["VoiceResult", "synthesize_voice"]


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------
class VoiceResult(BaseModel):
    """Voice-stage output (integration contract, Worker 3)."""

    narration_wav: str            # final assembled narration (voice stem)
    duration_s: float             # ffprobe-measured narration duration
    path_used: str                # "edge-tts" | "mixed" | "stub-silence"
    voice_id: str
    #: Provenance (Round 4): script the narration was handed to TTS in,
    #: "roman" (default/legacy) or "urdu_script" (transliterated).
    tts_input_script: str = "roman"
    #: Latin tokens left untransliterated (unknown English), in order.
    transliteration_unknown_tokens: list[str] = Field(default_factory=list)
    #: Dictionary-covered tokens/phrases replaced during transliteration.
    transliteration_dictionary_hits: list[str] = Field(default_factory=list)
    chapter_files: list[str] = Field(default_factory=list)
    music_wav: str | None = None
    mixed_master_wav: str | None = None
    ledger_rows: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Small duck-typing helpers (bundle / profile / ctx may be models, dataclasses
# or dicts depending on which sibling worker produced them)
# ---------------------------------------------------------------------------
def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict):
            if name in obj and obj[name] not in (None, ""):
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value not in (None, ""):
                return value
    return default


def _ctx_less_tmp_dir(name: str) -> Path:
    """Fallback work dir when no StageContext supplies one.

    Lives under the canonical data dir's tmp area
    (``<data dir>/tmp/<name>``, via :mod:`app.data_dir`) — never a
    hard-coded OS ``/tmp`` path, which the packaged Windows app
    cannot rely on. Pure path arithmetic; creates nothing.
    """
    return data_dir_layout().tmp_dir / name


def _run_coro(coro_factory) -> Any:
    """Run an async factory from sync code.

    Loop ownership (Round 4): delegates to
    ``app.integration.run_loop.run_coroutine`` — inside a produce run
    the coroutine runs on the run's single owned loop; this stage
    never creates a loop or a thread-bridge of its own.
    """
    from .run_loop import run_coroutine

    return run_coroutine(coro_factory)


# ---------------------------------------------------------------------------
# Voice / language / preset resolution
# ---------------------------------------------------------------------------
_URDU_SCRIPT_RE = re.compile(r"[\u0600-\u06FF]")

#: locale -> (male, female) edge-tts voices (same IDs as the providers layer).
_VOICES: dict[str, tuple[str, str]] = {
    "ur-PK": ("ur-PK-AsadNeural", "ur-PK-UzmaNeural"),
    "hi-IN": ("hi-IN-MadhurNeural", "hi-IN-SwaraNeural"),
    "en-US": ("en-US-GuyNeural", "en-US-JennyNeural"),
    "en-GB": ("en-GB-RyanNeural", "en-GB-SoniaNeural"),
    "en-IN": ("en-IN-PrabhatNeural", "en-IN-NeerjaNeural"),
}

_NICHE_KEY_MAP = {
    "horror_stories": "horror",
    "crime_stories": "crime",
    "islamic": "islamic_stories",
    "islamic stories": "islamic_stories",
    "custom": "other",
}


def _language_of(profile: Any, bundle: Any, texts: list[str],
                 spec: Any = None) -> str:
    """Normalised language tag like 'ur', 'hi', 'en' (+ optional region)."""
    # An explicit English request on the spec wins (mirrors the content
    # stage's language-mode rule); otherwise the profile's mix label rules.
    spec_lang = str(_get(spec, "language", "lang", "locale") or "").strip().lower()
    if spec_lang in ("en", "english", "en-us", "en-gb"):
        return "en"
    raw = _get(profile, "language", "lang", "locale") or _get(
        bundle, "language", "lang", "locale") or _get(
        spec, "language", "lang", "locale")
    if raw:
        tag = str(raw).strip().lower().replace("_", "-")
        # project language names -> codes
        names = {
            "roman-urdu": "ur", "roman urdu": "ur", "urdu": "ur",
            "english": "en", "hindi": "hi",
        }
        base = tag.split("+")[0].strip()
        if base in names:
            return names[base]
        return base
    if texts and _URDU_SCRIPT_RE.search(texts[0]):
        return "ur"
    # Project default: Roman-Urdu narration channels.
    return "ur"


def _locale_for(lang: str) -> str:
    if lang in _VOICES:
        return lang
    base = lang.split("-")[0]
    for locale in _VOICES:
        if locale.split("-")[0] == base:
            # preserve an explicit region when it matches the voice family
            if "-" in lang:
                candidate = f"{base}-{lang.split('-', 1)[1].upper()}"
                if candidate in _VOICES:
                    return candidate
            return locale
    return "en-US"


def _resolve_voice(profile: Any, lang: str) -> str:
    """Explicit profile voice if it is a real voice id, else by hint+locale.

    Defaults (contract): ur / roman_urdu -> ur-PK-AsadNeural (male) with
    ur-PK-UzmaNeural as the female alternate.
    """
    explicit = _get(profile, "voice_id", "tts_voice_id", "edge_voice_id", "voice")
    if explicit and "Neural" in str(explicit):
        return str(explicit)
    provider_voices = _get(profile, "provider_voices")
    if isinstance(provider_voices, dict):
        for key in ("edge", "edge-tts", "azure"):
            if provider_voices.get(key):
                return str(provider_voices[key])

    hint = " ".join(
        str(_get(profile, name) or "")
        for name in ("voice_style", "voice_hint", "voice_gender", "gender")
    ).lower()
    locale = _locale_for(lang)
    male, female = _VOICES[locale]
    if "uzma" in hint:
        return female if locale == "ur-PK" else female
    if "asad" in hint:
        return male
    if any(w in hint for w in ("female", "woman", "lady", "aurat")):
        return female
    return male


def _preset_for(profile: Any):
    """Providers-layer niche preset (rate / pitch / pauses / WPM)."""
    try:
        from app.providers.tts.presets import get_preset

        niche = _get(profile, "profile_id", "niche", "preset", "niche_id")
        key = str(niche or "other").strip().lower()
        return get_preset(_NICHE_KEY_MAP.get(key, key))
    except Exception:
        return None


def _wpm_of(profile: Any, preset: Any) -> float:
    wpm = _get(profile, "target_wpm", "wpm", "wpm_target")
    if wpm:
        try:
            return float(wpm)
        except (TypeError, ValueError):
            pass
    if preset is not None:
        return float(preset.wpm_target)
    return 140.0


# ---------------------------------------------------------------------------
# Pre-TTS transliteration (Round 4, Worker 3)
# ---------------------------------------------------------------------------
def _resolve_tts_input_script(profile: Any, spec: Any = None,
                              bundle: Any = None) -> str:
    """Channel/profile config surface for the TTS input script.

    Resolution order: ProjectSpec (per-channel request) -> niche/voice
    profile -> bundle.  Values are normalised to ``"roman"`` /
    ``"urdu_script"``; anything unrecognised falls back to ``"roman"``
    (the legacy default — behaviour is unchanged unless a channel
    explicitly opts into ``urdu_script``).
    """
    for obj in (spec, profile, bundle):
        raw = _get(obj, "tts_input_script", "tts_script", "input_script")
        if raw:
            cleaned = str(raw).strip().lower().replace("-", "_")
            if cleaned in ("urdu_script", "urdu"):
                return "urdu_script"
            if cleaned == "roman":
                return "roman"
    return "roman"


def _prepare_tts_texts(texts: list[str], profile: Any, spec: Any,
                       bundle: Any, lang: str, voice_id: str) -> tuple[
                           list[str], str, list[str], list[str]]:
    """Apply the ``tts_input_script`` option to chapter texts.

    Returns ``(texts_for_tts, effective_script, unknown_tokens,
    dictionary_hits)``.  Transliteration applies ONLY for Urdu voices
    (language base ``ur`` or an ``ur-*`` voice id) when the channel
    selected ``urdu_script``; otherwise the original texts pass
    through untouched.  The bundle/script itself is never mutated —
    subtitles keep building from the ORIGINAL narration text (the
    pronunciation-dictionary contract), only the TTS hand-off changes.
    The chapter cache keys hash the (possibly transliterated) text,
    so the two input scripts never share a cache entry.
    """
    requested = _resolve_tts_input_script(profile, spec, bundle)
    is_urdu_voice = (
        str(lang or "").split("-")[0].lower() == "ur"
        or str(voice_id or "").lower().startswith("ur-")
    )
    if requested != "urdu_script" or not is_urdu_voice:
        return list(texts), "roman", [], []
    from app.providers.tts.transliteration import transliterate

    niche = _get(profile, "profile_id", "niche", "niche_id")
    out: list[str] = []
    unknown: list[str] = []
    hits: list[str] = []
    for text in texts:
        result = transliterate(str(text), niche=(
            str(niche).strip().lower() if niche else None))
        out.append(result.text)
        unknown.extend(result.unknown_tokens)
        hits.extend(result.dictionary_hits)
    return out, "urdu_script", unknown, hits


# ---------------------------------------------------------------------------
# FFmpeg helpers (convert / stub / beds) — the pipeline package owns the
# shared runner; local fallbacks keep this stage self-sufficient.
# ---------------------------------------------------------------------------
def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        [ffmpeg_executable(), "-y", "-hide_banner", *args],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg failed: {tail}")


def _probe_duration(path: Path) -> float:
    try:
        from app.pipeline.audio._ffmpeg import probe_duration

        return float(probe_duration(Path(path)))
    except Exception:
        out = subprocess.run(
            [ffprobe_executable(), "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        return float(out.stdout.strip().splitlines()[0])


def _to_wav_48k_mono(src: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(["-i", str(src), "-vn", "-ar", "48000", "-ac", "1",
             "-c:a", "pcm_s24le", str(out)])
    return out


def _stub_chapter_wav(text: str, wpm: float, out: Path) -> Path:
    """Placeholder chapter: very soft 196 Hz tone of realistic duration.

    Duration = words / WPM.  The tone is ≈ −30 dBFS (not digital silence)
    so loudnorm measurement downstream stays finite; see module docstring.
    """
    words = max(1, len(text.split()))
    duration = max(1.5, words / max(60.0, wpm) * 60.0)
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=196:sample_rate=48000",
        "-t", f"{duration:.3f}",
        "-af", "volume=0.03",
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s24le", str(out),
    ])
    return out


# ---------------------------------------------------------------------------
# REAL path 1 — providers layer (EdgeTTSAdapter)
# ---------------------------------------------------------------------------
def _patch_edge_ssl_context() -> None:
    """Point edge-tts' pinned certifi context at the OS/env CA bundle when
    one is configured (egress MITM proxies set SSL_CERT_FILE); no-op on a
    normal direct connection."""
    try:
        if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
            import edge_tts.communicate as _comm

            _comm._SSL_CTX = ssl.create_default_context()
    except Exception:
        pass


def _adapter_synthesize(text: str, voice_id: str, lang: str,
                        rate_percent: float, pitch_hz: float,
                        timeout_s: float) -> bytes:
    from app.providers.tts.adapters import EdgeTTSAdapter
    from app.providers.tts.base import TTSRequest

    _patch_edge_ssl_context()
    adapter = EdgeTTSAdapter()
    req = TTSRequest(
        text=text, voice_id=voice_id, language=lang,
        rate_percent=rate_percent, pitch_hz=pitch_hz,
    )

    async def _run() -> bytes:
        result = await asyncio.wait_for(adapter.synthesize(req), timeout=timeout_s)
        if result.is_empty or not result.audio_bytes:
            raise RuntimeError("edge-tts adapter returned empty audio")
        return result.audio_bytes

    return _run_coro(_run)


# ---------------------------------------------------------------------------
# REAL path 2 — raw WebSocket transport for the edge-tts protocol.
#
# Identical protocol to the edge_tts library (DRM Sec-MS-GEC token, speech
# .config + SSML messages, header-prefixed binary audio frames); only the
# transport differs: a plain socket + manual RFC6455 framing that accepts
# a standards-valid 101 whose Connection header a middlebox rewrote, which
# aiohttp's strict handshake check rejects.  Proxy (CONNECT + optional
# Basic auth) and CA configuration come from the standard environment and
# are never logged.
# ---------------------------------------------------------------------------
def _proxy_url() -> str | None:
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _open_edge_socket(host: str, port: int, timeout: float) -> ssl.SSLSocket:
    proxy = _proxy_url()
    if proxy:
        parsed = urlparse(proxy)
        sock = socket.create_connection(
            (parsed.hostname, parsed.port or 3128), timeout=timeout)
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}",
                 "Proxy-Connection: keep-alive"]
        if parsed.username:
            creds = base64.b64encode(
                f"{parsed.username}:{parsed.password or ''}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {creds}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("proxy closed during CONNECT")
            buf.extend(chunk)
        status = bytes(buf).split(b"\r\n", 1)[0].decode("latin1")
        if " 200" not in status:
            raise ConnectionError(f"proxy CONNECT failed: {status}")
    else:
        sock = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(sock, server_hostname=host)


class _RawWS:
    """Minimal masked-client / unmasked-server RFC6455 framing."""

    def __init__(self, sock: ssl.SSLSocket):
        self._s = sock
        self._buf = bytearray()

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._s.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed by peer")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", len(data))
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", len(data))
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._s.sendall(bytes(header) + masked)

    def _send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x8A, 0x80 | len(payload)]) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._s.sendall(bytes(header) + masked)

    def recv_message(self) -> tuple[int, bytes]:
        frags = bytearray()
        first_op: int | None = None
        while True:
            head = self.read_exact(2)
            fin, op = head[0] & 0x80, head[0] & 0x0F
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self.read_exact(8))[0]
            if head[1] & 0x80:
                self.read_exact(4)  # server frames are unmasked; be safe
            payload = self.read_exact(length) if length else b""
            if op == 9:
                self._send_pong(payload)
                continue
            if op == 8:
                raise ConnectionError("websocket close frame received")
            if op in (1, 2):
                first_op, frags = op, bytearray(payload)
            elif op == 0:
                frags.extend(payload)
            if fin:
                return (first_op or 2), bytes(frags)


def _raw_edge_synthesize(text: str, voice_id: str,
                         rate_percent: float, pitch_hz: float,
                         timeout_s: float) -> bytes:
    """Synthesize one chunk of text via the raw edge-tts WS transport."""
    import edge_tts
    from edge_tts.communicate import (
        connect_id, date_to_string, get_headers_and_data, mkssml,
        ssml_headers_plus_data,
    )
    from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
    from edge_tts.drm import DRM

    rate = f"{rate_percent:+.0f}%"
    pitch = f"{pitch_hz:+.0f}Hz"
    comm = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch,
                                boundary="WordBoundary")

    parsed = urlparse(WSS_URL)
    host = parsed.hostname or "speech.platform.bing.com"
    port = parsed.port or 443
    tls = _open_edge_socket(host, port, timeout=min(15.0, timeout_s))
    try:
        tls.settimeout(timeout_s)
        ws = _RawWS(tls)
        path = (
            f"{parsed.path}?{parsed.query}"
            f"&ConnectionId={connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
        )
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1", f"Host: {host}",
            "Upgrade: websocket", "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
        ]
        skip = {"host", "connection", "upgrade",
                "sec-websocket-key", "sec-websocket-version"}
        for hname, hvalue in WSS_HEADERS.items():
            if hname.lower() not in skip:
                lines.append(f"{hname}: {hvalue}")
        tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            raw.extend(ws.read_exact(1))
        status = bytes(raw).split(b"\r\n", 1)[0].decode("latin1")
        if "101" not in status:
            raise ConnectionError(f"edge WS handshake failed: {status}")

        audio = bytearray()
        for partial in comm.texts:
            ws.send_text(
                f"X-Timestamp:{date_to_string()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\n"
                "Path:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"'
                '},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}'
            )
            ws.send_text(ssml_headers_plus_data(
                connect_id(), date_to_string(),
                mkssml(comm.tts_config, partial)))
            got_audio = False
            while True:
                op, payload = ws.recv_message()
                if op == 1:
                    params, _data = get_headers_and_data(
                        payload, payload.find(b"\r\n\r\n"))
                    path_hdr = params.get(b"Path")
                    if path_hdr == b"turn.end":
                        break
                    if path_hdr in (b"response", b"turn.start", b"audio.metadata"):
                        continue
                    raise ValueError(f"edge WS: unknown path {path_hdr!r}")
                if len(payload) < 2:
                    continue
                header_len = int.from_bytes(payload[:2], "big")
                params, data = get_headers_and_data(payload, header_len)
                if params.get(b"Path") != b"audio":
                    raise ValueError("edge WS: binary frame path is not audio")
                if params.get(b"Content-Type") is None and len(data) == 0:
                    continue
                if data:
                    audio.extend(data)
                    got_audio = True
            if not got_audio:
                raise ConnectionError("edge WS: no audio received for chunk")
        if not audio:
            raise ConnectionError("edge WS: empty synthesis result")
        return bytes(audio)
    finally:
        try:
            tls.close()
        except Exception:
            pass


def _edge_synthesize_chunk(text: str, voice_id: str, lang: str,
                           rate_percent: float, pitch_hz: float) -> bytes:
    """One chunk via edge-tts: providers adapter, then raw transport —
    one retry each, short timeouts.  Raises after all attempts fail."""
    last_exc: Exception | None = None
    for attempt_fn in (
        lambda: _adapter_synthesize(text, voice_id, lang,
                                    rate_percent, pitch_hz, timeout_s=45.0),
        lambda: _raw_edge_synthesize(text, voice_id,
                                     rate_percent, pitch_hz, timeout_s=40.0),
    ):
        for _try in range(2):  # initial + 1 retry
            try:
                return attempt_fn()
            except Exception as exc:  # any failure -> next transport / stub
                last_exc = exc
    raise RuntimeError(f"edge-tts synthesis failed: {last_exc}")


# ---------------------------------------------------------------------------
# Chapter rendering (with cache)
# ---------------------------------------------------------------------------
def _chapter_cache_paths(audio_dir: Path, index: int,
                         text: str, voice_id: str,
                         rate_percent: float, pitch_hz: float) -> tuple[Path, Path, str]:
    digest = hashlib.sha256(
        f"{voice_id}|{rate_percent:+.1f}|{pitch_hz:+.1f}|{text}".encode("utf-8")
    ).hexdigest()
    stem = f"chapter_{index:02d}_{digest[:12]}"
    return (audio_dir / f"{stem}.wav", audio_dir / f"{stem}.json", digest)


def _render_chapter(index: int, text: str, *, audio_dir: Path, voice_id: str,
                    lang: str, rate_percent: float, pitch_hz: float,
                    chunk_gap_ms: int, wpm: float) -> tuple[Path, str]:
    """Return (chapter_wav, source) where source is 'edge-tts' or 'stub'."""
    wav_path, sidecar_path, digest = _chapter_cache_paths(
        audio_dir, index, text, voice_id, rate_percent, pitch_hz)
    if wav_path.is_file() and sidecar_path.is_file():
        try:
            meta = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if meta.get("sha256") == digest and _probe_duration(wav_path) > 0:
                return wav_path, str(meta.get("source", "edge-tts"))
        except Exception:
            pass  # corrupt cache entry -> re-render below

    source = "edge-tts"
    try:
        from app.providers.tts.chunking import chunk_chapter_text

        chunks = chunk_chapter_text(text) or [text]
    except Exception:
        chunks = [text]

    segment_wavs: list[Path] = []
    seg_dir = audio_dir / ".segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    try:
        for j, chunk_text in enumerate(chunks):
            mp3_path = seg_dir / f"ch{index:02d}_seg{j:02d}.mp3"
            audio_bytes = _edge_synthesize_chunk(
                chunk_text, voice_id, lang, rate_percent, pitch_hz)
            mp3_path.write_bytes(audio_bytes)
            seg_wav = seg_dir / f"ch{index:02d}_seg{j:02d}.wav"
            _to_wav_48k_mono(mp3_path, seg_wav)
            segment_wavs.append(seg_wav)
    except Exception:
        source = "stub"
        segment_wavs = []

    if source == "edge-tts" and segment_wavs:
        if len(segment_wavs) == 1:
            segment_wavs[0].replace(wav_path)
        else:
            from app.pipeline.audio.voice import assemble_voice_stem

            assemble_voice_stem(segment_wavs, gaps_ms=chunk_gap_ms, out=wav_path)
    else:
        _stub_chapter_wav(text, wpm, wav_path)

    sidecar_path.write_text(json.dumps({
        "sha256": digest, "source": source, "voice_id": voice_id,
        "rate_percent": rate_percent, "pitch_hz": pitch_hz,
        "chars": len(text), "words": len(text.split()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return wav_path, source


# ---------------------------------------------------------------------------
# Music bed (locally generated — own/generated, no external asset)
# ---------------------------------------------------------------------------
def _generate_music_bed_raw(duration_s: float, out: Path) -> Path:
    """Two detuned sines + pink noise: a neutral generated tone bed."""
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=110:sample_rate=48000",
        "-f", "lavfi", "-i", "sine=frequency=164.81:sample_rate=48000",
        "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000:amplitude=0.6",
        "-filter_complex",
        "[0:a]volume=0.45[a];[1:a]volume=0.30[b];[2:a]volume=0.12[c];"
        "[a][b][c]amix=inputs=3:duration=shortest:normalize=0,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[m]",
        "-map", "[m]", "-t", f"{duration_s:.3f}",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(out),
    ])
    return out


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------
def _chapter_texts(bundle: Any) -> list[str]:
    texts: list[str] = []
    chapters = _get(bundle, "chapters", default=None)
    if chapters:
        for chapter in chapters:  # type: ignore[union-attr]
            text = _get(chapter, "text", "narration_text", "content", "script")
            if text and str(text).strip():
                texts.append(str(text))
    if not texts:
        narration = _get(bundle, "narration_text", "text", "script")
        if narration and str(narration).strip():
            texts = [str(narration)]
    return texts


def synthesize_voice(bundle: Any, profile: Any, ctx: Any) -> VoiceResult:
    """Integration voice stage: real edge-tts per chapter (stub fallback),
    narration assembly, generated music bed, −14 LUFS mix/master."""
    _audio_dir = _get(ctx, "audio_dir", "out_dir", default=None)
    audio_dir = (
        Path(str(_audio_dir))
        if _audio_dir is not None
        else _ctx_less_tmp_dir("voice")
    )
    audio_dir.mkdir(parents=True, exist_ok=True)

    texts = _chapter_texts(bundle)
    if not texts:
        raise ValueError("voice_stage: bundle carries no chapter/narration text")

    spec = _get(ctx, "spec", default=None)
    lang = _language_of(profile, bundle, texts, spec)
    voice_id = _resolve_voice(profile, lang)
    # The spec's explicit voice choice (ProjectSpec.voice_id) wins overall.
    spec_voice = _get(spec, "voice_id", "tts_voice_id")
    if spec_voice and "Neural" in str(spec_voice):
        voice_id = str(spec_voice)
    preset = _preset_for(profile)
    # Round 4: pre-TTS transliteration option (per-channel).  Only the
    # texts handed to synthesis change; `bundle` / subtitles are untouched.
    texts, tts_input_script, translit_unknown, translit_hits = \
        _prepare_tts_texts(texts, profile, spec, bundle, lang, voice_id)
    rate_percent = float(preset.rate_percent) if preset else 0.0
    # Round 4 (Worker 2): an explicit user rate override on the spec
    # (ProjectSpec.tts_rate_percent) wins over the niche preset.  It is
    # UNUSED by default — script-length planning now uses the voice's
    # calibrated WPM (app.integration.planning), so no compensating
    # override is needed to hit the duration target; when a user does
    # set one it is honoured here and logged loudly, and the planner
    # plans at the calibrated WPM for that same rate.
    _rate_override = _get(spec, "tts_rate_percent", "rate_percent")
    if _rate_override is not None:
        try:
            rate_percent = float(_rate_override)
            logging.getLogger(__name__).warning(
                "voice_stage: explicit user TTS rate override %+.1f%% "
                "applied (overrides niche preset rate)", rate_percent)
        except (TypeError, ValueError):
            logging.getLogger(__name__).warning(
                "voice_stage: ignoring non-numeric TTS rate override %r",
                _rate_override)
    pitch_hz = float(preset.pitch_hz) if preset else 0.0
    chapter_pause_ms = int(preset.chapter_pause_ms) if preset else 650
    chunk_gap_ms = int(preset.chunk_gap_ms) if preset else 300
    wpm = _wpm_of(profile, preset)

    # 1-3. Per-chapter synthesis (real -> stub), cached by text hash.
    chapter_files: list[str] = []
    sources: list[str] = []
    for i, text in enumerate(texts, start=1):
        wav_path, source = _render_chapter(
            i, text, audio_dir=audio_dir, voice_id=voice_id, lang=lang,
            rate_percent=rate_percent, pitch_hz=pitch_hz,
            chunk_gap_ms=chunk_gap_ms, wpm=wpm)
        chapter_files.append(str(wav_path))
        sources.append(source)

    if all(s == "edge-tts" for s in sources):
        path_used = "edge-tts"
    elif any(s == "edge-tts" for s in sources):
        path_used = "mixed"
    else:
        path_used = "stub-silence"

    # 4. Assemble the narration stem (locked voice chain, chapter pauses).
    from app.pipeline.audio.voice import assemble_voice_stem

    narration_wav = audio_dir / "narration.wav"
    assemble_voice_stem(
        [Path(p) for p in chapter_files],
        gaps_ms=float(chapter_pause_ms), out=narration_wav)
    duration_s = _probe_duration(narration_wav)

    # 5. Generated music bed + mix/master to −14 LUFS.
    music_wav: str | None = None
    mixed_master_wav: str | None = None
    mix_note = ""
    try:
        from app.pipeline.audio.music import prepare_music
        from app.pipeline.audio.mix import mix_and_master

        raw_bed = _generate_music_bed_raw(duration_s, audio_dir / "music_bed_raw.wav")
        prepared = audio_dir / "music_bed.wav"
        prepare_music(raw_bed, prepared, duration_s=duration_s)
        music_wav = str(prepared)
        master = audio_dir / "mixed_master.wav"
        mix_and_master(narration_wav, prepared, master)
        mixed_master_wav = str(master)
    except Exception as exc:
        mix_note = f"music/mix step failed: {exc}"

    ledger_rows: list[dict] = [
        {
            "asset_id": "narration_voice",
            "scene_id": "all",
            "kind": "voice",
            "path": str(narration_wav),
            "local_path": str(narration_wav),
            "source": "own/generated (edge-tts synthesis)",
            "licence": "n/a — generated in-app",
            "voice_id": voice_id,
            "path_used": path_used,
            "chapter_sources": sources,
            "chapters": len(chapter_files),
            "duration_s": round(duration_s, 3),
            "notes": (
                f"edge-tts voice {voice_id}; per-chapter sources: "
                f"{', '.join(sources)}"
            ),
            "tts_input_script": tts_input_script,
            "transliteration_unknown_token_count": len(translit_unknown),
            "transliteration_unknown_tokens": translit_unknown,
            "transliteration_dictionary_hits": translit_hits,
        },
    ]
    if music_wav:
        ledger_rows.append({
            "asset_id": "music_bed",
            "scene_id": "all",
            "kind": "music",
            "path": music_wav,
            "local_path": music_wav,
            "source": "own/generated (local tone bed)",
            "licence": "n/a — generated in-app",
            "mixed_master_wav": mixed_master_wav,
            "duration_s": round(duration_s, 3),
            "notes": "generated in-app (sine + pink-noise bed); no external asset",
        })
    elif mix_note:
        ledger_rows.append({
            "asset_id": "music_bed",
            "scene_id": "all",
            "kind": "music",
            "path": None,
            "local_path": "",
            "source": "own/generated (local tone bed)",
            "licence": "n/a — generated in-app",
            "error": mix_note,
            "notes": mix_note,
        })

    return VoiceResult(
        narration_wav=str(narration_wav),
        duration_s=round(float(duration_s), 3),
        path_used=path_used,
        voice_id=voice_id,
        tts_input_script=tts_input_script,
        transliteration_unknown_tokens=translit_unknown,
        transliteration_dictionary_hits=translit_hits,
        chapter_files=chapter_files,
        music_wav=music_wav,
        mixed_master_wav=mixed_master_wav,
        ledger_rows=ledger_rows,
    )
