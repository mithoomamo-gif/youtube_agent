"""Prompt loading and 5-layer prompt assembly for the YouTube Agent app.

Prompts are PRODUCT ASSETS, not code: every production prompt lives as a
versioned Markdown file with YAML front-matter under the repo-root ``prompts/``
directory (``prompts/<agent_slug>/<task>.md``). No prompt string is ever
inlined in agent code — every LLM call resolves its template through this
module (see ``prompts/README.md`` and roadmap section 23, "Prompt versioning").

The 5-layer prompt stack (roadmap section 33) is assembled, in order:

    1. Core System Layer  — the prompt file's own body (fixed, versioned,
       never per-niche). May itself contain a ``{{core_system}}`` placeholder
       for an extra shared core block supplied by the caller.
    2. Niche Profile Layer — ``{{niche_profile}}`` — injected from the Niche
       Profile Object (language, tone, WPM, structure, hook families, rules).
    3. Channel Layer       — ``{{channel}}`` — channel identity, audience,
       series continuity, pronunciation-dictionary excerpts.
    4. Topic Layer         — ``{{topic_layer}}`` (alias ``{{topic}}``) — topic,
       keyword pack, research brief, unique angle, competitor gaps.
    5. Run Layer           — ``{{run_params}}`` — target minutes/words, chapter
       budgets, interrupt cadence, publish mode.

Substitution is a plain, order-preserving placeholder replacement: layers are
substituted in stack order (core → niche → channel → topic → run) so a lower
layer can never accidentally rewrite an earlier layer's text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "PromptLayers",
    "DEFAULT_PROMPTS_ROOT",
    "default_prompts_root",
    "prompt_path",
    "load_prompt",
    "list_prompts",
    "assemble_prompt",
]

# ---------------------------------------------------------------------------
# PromptLayers — the five layers of the stack.
#
# Defined with pydantic when it is available (the backend's normal runtime,
# via FastAPI) and as a plain dataclass otherwise, so importing this module
# never fails on a missing optional dependency.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised depending on installed deps
    from pydantic import BaseModel

    class PromptLayers(BaseModel):  # type: ignore[no-redef]
        """The five prompt layers, in assembly order. All fields are text."""

        core_system: str = ""
        niche_profile: str = ""
        channel: str = ""
        topic: str = ""
        run_params: str = ""

except Exception:  # pydantic not installed — dataclass fallback

    @dataclass
    class PromptLayers:  # type: ignore[no-redef]
        """The five prompt layers, in assembly order. All fields are text."""

        core_system: str = ""
        niche_profile: str = ""
        channel: str = ""
        topic: str = ""
        run_params: str = ""


# ---------------------------------------------------------------------------
# Locating the prompts/ directory
# ---------------------------------------------------------------------------
def default_prompts_root() -> Path:
    """Repo-root ``prompts/`` directory, resolved relative to this file.

    This file lives at ``backend/app/agents/prompt_assembly.py``, so the repo
    root is three parents up from ``agents/``'s parent chain:
    agents -> app -> backend -> <repo root>.
    """
    return Path(__file__).resolve().parents[3] / "prompts"


DEFAULT_PROMPTS_ROOT = default_prompts_root()


def _resolve_root(prompts_root: str | Path | None) -> Path:
    return Path(prompts_root) if prompts_root is not None else DEFAULT_PROMPTS_ROOT


def prompt_path(
    agent_slug: str, task: str, prompts_root: str | Path | None = None
) -> Path:
    """Path of the prompt file for ``agent_slug`` / ``task``."""
    return _resolve_root(prompts_root) / agent_slug / f"{task}.md"


# ---------------------------------------------------------------------------
# Front-matter parsing (PyYAML if available, tiny fallback parser otherwise)
# ---------------------------------------------------------------------------
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _coerce_scalar(value: str) -> Any:
    """Best-effort scalar typing for the fallback parser."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "none", "~"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _parse_front_matter_minimal(block: str) -> dict[str, Any]:
    """Minimal YAML-subset parser: flat ``key: value`` lines only.

    Sufficient for this project's front-matter (name, version, agent,
    agent_id, task, tier, status, output_schema). Indented/nested structures
    are preserved as raw strings rather than guessed at.
    """
    meta: dict[str, Any] = {}
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = _coerce_scalar(value)
    return meta


def _parse_front_matter(block: str) -> dict[str, Any]:
    try:
        import yaml  # PyYAML — preferred when available
    except Exception:
        return _parse_front_matter_minimal(block)
    loaded = yaml.safe_load(block)
    return dict(loaded) if isinstance(loaded, dict) else {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_prompt(
    agent_slug: str,
    task: str,
    prompts_root: str | Path | None = None,
) -> tuple[dict, str]:
    """Load a prompt template.

    Returns ``(front_matter, body)`` where ``front_matter`` is the parsed YAML
    front-matter dict (``name``, ``version``, ``agent``, ``agent_id``,
    ``task``, ``tier``, ``status``, …) and ``body`` is the Markdown template
    body with the front-matter stripped.

    Raises:
        FileNotFoundError: if no prompt file exists for the agent/task.
    """
    path = prompt_path(agent_slug, task, prompts_root)
    if not path.is_file():
        raise FileNotFoundError(
            f"No prompt file for agent={agent_slug!r} task={task!r}: {path}"
        )
    text = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    meta = _parse_front_matter(match.group(1))
    body = text[match.end() :].strip()
    return meta, body


def list_prompts(
    prompts_root: str | Path | None = None,
) -> list[tuple[str, str]]:
    """List all available ``(agent_slug, task)`` pairs, sorted."""
    root = _resolve_root(prompts_root)
    if not root.is_dir():
        return []
    pairs: list[tuple[str, str]] = []
    for agent_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for md in sorted(agent_dir.glob("*.md")):
            pairs.append((agent_dir.name, md.stem))
    return pairs


# Placeholder -> layer value, in strict 5-layer assembly order. The Topic
# layer answers to both {{topic_layer}} (the name used in the prompt files)
# and {{topic}} (the PromptLayers field name).
def _substitutions(layers: PromptLayers) -> list[tuple[str, str]]:
    return [
        ("{{core_system}}", layers.core_system),
        ("{{niche_profile}}", layers.niche_profile),
        ("{{channel}}", layers.channel),
        ("{{topic_layer}}", layers.topic),
        ("{{topic}}", layers.topic),
        ("{{run_params}}", layers.run_params),
    ]


def assemble_prompt(
    agent_slug: str,
    task: str,
    layers: PromptLayers,
    prompts_root: str | Path | None = None,
) -> str:
    """Assemble the final prompt text for one LLM call.

    Loads the template body for ``agent_slug`` / ``task`` (Layer 1, the Core
    System Layer) and substitutes the five layer placeholders in stack order:
    ``{{core_system}}``, ``{{niche_profile}}``, ``{{channel}}``,
    ``{{topic_layer}}`` / ``{{topic}}``, ``{{run_params}}``.

    Layer values are inserted verbatim. A placeholder whose layer value is an
    empty string is replaced with an empty string (the template's surrounding
    headings remain, which keeps assembled prompts diffable in logs).

    Raises:
        FileNotFoundError: for an unknown agent slug or task.
        TypeError: if ``layers`` is not a :class:`PromptLayers`.
    """
    if not isinstance(layers, PromptLayers):
        raise TypeError(
            f"layers must be a PromptLayers instance, got {type(layers).__name__}"
        )
    _meta, body = load_prompt(agent_slug, task, prompts_root)
    assembled = body
    for placeholder, value in _substitutions(layers):
        assembled = assembled.replace(placeholder, value or "")
    return assembled
