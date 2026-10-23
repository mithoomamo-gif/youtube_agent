"""Pronunciation dictionary — 3 versioned layers (Global / Per-niche /
Per-channel), roadmap section 30.

An entry stores BOTH a respelt ``alias`` (works on every provider) and an
``ipa`` form (used where phoneme control exists), plus a per-provider
mechanism map. Compilation is per provider:

* edge / openai -> plain-text alias substitution
* azure         -> inline SSML ``<phoneme>`` (or lexicon for high-freq terms)
* google        -> request-level ``custom_pronunciations``
* elevenlabs    -> pronunciation-dictionary rules object (alias + IPA)

Subtitles are NEVER built from compiled text: the original script text is
restored for subtitles (timestamps + ORIGINAL script). Dictionary version is
part of the chunk cache key, so fixing one word re-renders exactly the
chunks containing it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as xml_escape

DEFAULT_MECHANISMS = {
    "azure": "phoneme",
    "google": "custom_pronunciations",
    "elevenlabs": "dictionary",
    "openai": "alias",
    "edge": "alias",
}

#: Providers whose adapter key matches a mechanism-family name.
PROVIDER_FAMILY = {
    "edge": "edge", "azure": "azure", "elevenlabs": "elevenlabs",
    "google": "google", "openai": "openai",
}


@dataclass
class PronunciationEntry:
    surface: str
    alias: str | None = None
    ipa: str | None = None
    lang: str | None = None
    mechanisms: dict[str, str] = field(default_factory=dict)
    layer: str = "global"

    def mechanism_for(self, provider: str) -> str:
        family = PROVIDER_FAMILY.get(provider, provider)
        return self.mechanisms.get(family) or self.mechanisms.get(provider) \
            or DEFAULT_MECHANISMS.get(family, "alias")


@dataclass
class CompiledText:
    """Result of compiling a dictionary against a text for one provider."""
    text: str                       # text actually sent (alias-compiled)
    original_text: str              # untouched text — the subtitle source
    ssml: str | None = None         # set for SSML providers (azure)
    custom_pronunciations: list[dict] = field(default_factory=list)  # google
    elevenlabs_dictionary: dict | None = None
    applied_surfaces: list[str] = field(default_factory=list)

    def subtitle_text(self) -> str:
        """Subtitles always come from the ORIGINAL script, never respelt."""
        return self.original_text


class PronunciationDictionary:
    """Merged 3-layer dictionary. Later layers override earlier ones."""

    def __init__(self, entries: list[PronunciationEntry] | None = None,
                 version: str | None = None):
        self._entries: dict[tuple[str, str | None], PronunciationEntry] = {}
        for entry in entries or []:
            self.add(entry)
        self._explicit_version = version

    # -- construction ------------------------------------------------------
    def add(self, entry: PronunciationEntry) -> None:
        self._entries[(entry.surface.lower(), entry.lang)] = entry

    @property
    def entries(self) -> list[PronunciationEntry]:
        return list(self._entries.values())

    @property
    def version(self) -> str:
        """Version is content-derived unless explicitly pinned: any entry
        change changes the version, hence the chunk cache keys."""
        if self._explicit_version:
            return self._explicit_version
        payload = json.dumps(
            [vars(e) for e in sorted(self.entries, key=lambda e: e.surface)],
            ensure_ascii=False, sort_keys=True)
        return "dict-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def from_layers(cls, layers: list[list[PronunciationEntry]],
                    version: str | None = None) -> "PronunciationDictionary":
        merged: list[PronunciationEntry] = []
        for layer in layers:            # global, then niche, then channel
            merged.extend(layer)
        return cls(merged, version=version)

    @classmethod
    def load_yaml_files(cls, paths: list[str],
                        version: str | None = None) -> "PronunciationDictionary":
        layers = [load_yaml_entries(p) for p in paths if os.path.exists(p)]
        explicit = version
        return cls.from_layers(layers, version=explicit)

    @classmethod
    def load_bundled(cls, niche: str | None = None,
                     channel_path: str | None = None) -> "PronunciationDictionary":
        """Load bundled global (+ optional per-niche) YAML, then channel file."""
        base = os.path.join(os.path.dirname(__file__), "dictionaries")
        paths = [os.path.join(base, "global.yaml")]
        if niche:
            paths.append(os.path.join(base, f"{niche}.yaml"))
        if channel_path:
            paths.append(channel_path)
        return cls.load_yaml_files(paths)

    # -- compilation -------------------------------------------------------
    def _matching(self, text: str, language: str | None) -> list[PronunciationEntry]:
        found = []
        for entry in self.entries:
            if entry.lang and language and entry.lang != language.split("-")[0]:
                continue
            if entry.surface and entry.surface in text:
                found.append(entry)
        # Longest surface first so multi-word terms win over their parts.
        return sorted(found, key=lambda e: -len(e.surface))

    def compile(self, text: str, provider: str,
                language: str | None = None) -> CompiledText:
        family = PROVIDER_FAMILY.get(provider, provider)
        if family == "azure":
            return self._compile_azure(text, language)
        if family == "google":
            return self._compile_google(text, language)
        if family == "elevenlabs":
            return self._compile_elevenlabs(text, language)
        return self._compile_alias(text, provider, language)

    def _compile_alias(self, text: str, provider: str,
                       language: str | None) -> CompiledText:
        out, applied = text, []
        for entry in self._matching(text, language):
            if entry.alias and entry.surface in out:
                out = out.replace(entry.surface, entry.alias)
                applied.append(entry.surface)
        return CompiledText(text=out, original_text=text, applied_surfaces=applied)

    def _compile_azure(self, text: str, language: str | None) -> CompiledText:
        # Escape first, then inject markup over the escaped surface forms so
        # the returned SSML body is safe to embed in a <voice> document.
        out, applied = xml_escape(text), []
        for entry in self._matching(text, language):
            mech = entry.mechanism_for("azure")
            esc_surface = xml_escape(entry.surface)
            if esc_surface not in out:
                continue
            if mech == "phoneme" and entry.ipa:
                repl = (f'<phoneme alphabet="ipa" '
                        f'ph="{xml_escape(entry.ipa, {chr(34): "&quot;"})}">'
                        f'{esc_surface}</phoneme>')
                out = out.replace(esc_surface, repl)
            elif entry.alias:  # lexicon/alias fallback inside SSML path
                out = out.replace(esc_surface, xml_escape(entry.alias))
            applied.append(entry.surface)
        return CompiledText(text=text, original_text=text, ssml=out,
                            applied_surfaces=applied)

    def _compile_google(self, text: str, language: str | None) -> CompiledText:
        customs, applied = [], []
        for entry in self._matching(text, language):
            if entry.ipa:
                customs.append({
                    "phrase": entry.surface,
                    "phoneme": entry.ipa,
                    "pronunciation": {"type": "PHONEME", "alphabet": "IPA",
                                      "phoneme": entry.ipa},
                })
                applied.append(entry.surface)
        return CompiledText(text=text, original_text=text,
                            custom_pronunciations=customs,
                            applied_surfaces=applied)

    def _compile_elevenlabs(self, text: str, language: str | None) -> CompiledText:
        rules, applied = [], []
        for entry in self._matching(text, language):
            if entry.ipa:
                rules.append({"string_to_replace": entry.surface,
                              "type": "phoneme", "phoneme": entry.ipa})
            elif entry.alias:
                rules.append({"string_to_replace": entry.surface,
                              "type": "alias", "alias": entry.alias})
            else:
                continue
            applied.append(entry.surface)
        dictionary = {"name": "pronunciation", "rules": rules} if rules else None
        return CompiledText(text=text, original_text=text,
                            elevenlabs_dictionary=dictionary,
                            applied_surfaces=applied)


def load_yaml_entries(path: str) -> list[PronunciationEntry]:
    """Load one YAML layer file. Format::

        version: "3"
        entries:
          - surface: "حضرت یوسف"
            lang: ur
            alias: "Hazrat Yousuf"
            ipa: "həzˈɾət̪ ˈjuːsʊf"
            mechanisms: {azure: phoneme, google: custom_pronunciations,
                         elevenlabs: dictionary, openai: alias, edge: alias}
    """
    try:
        import yaml  # PyYAML
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML is required to load dictionary YAML") from exc
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    layer = os.path.splitext(os.path.basename(path))[0]
    entries: list[PronunciationEntry] = []
    raw = data.get("entries", [])
    if isinstance(raw, dict):  # mapping style: surface -> fields
        raw = [dict(surface=k, **(v or {})) for k, v in raw.items()]
    for item in raw:
        entries.append(PronunciationEntry(
            surface=str(item["surface"]),
            alias=item.get("alias"),
            ipa=item.get("ipa"),
            lang=item.get("lang"),
            mechanisms=dict(item.get("mechanisms") or {}),
            layer=layer,
        ))
    return entries


def contains_surface(text: str, entry: PronunciationEntry) -> bool:
    return bool(re.search(re.escape(entry.surface), text))
