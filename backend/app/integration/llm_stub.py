"""Deterministic offline LLM stub for integration testing.

``DeterministicStubLLM`` is an OFFLINE STUB for integration testing —
it is NOT a real model.  It makes no network calls, holds no API keys,
and never generates genuinely new language-model output.  It exists so
the Round-2 pipeline (agents, router-shaped call sites, orchestration)
can run end-to-end on a machine with no provider credentials, with
fully reproducible results: the same request always yields the same
response text.

It implements the existing provider protocol from
``app.providers.llm.models`` — ``LLMProvider`` with
``async generate(LLMRequest) -> LLMResult`` — so it can be dropped in
anywhere a real adapter / the router is expected.  Responses are
rule-based: the stub extracts the topic / keywords from the request
prompt (``Topic: ...`` lines, quoted phrases, capitalised phrases and
frequent content words) and echoes them back inside fixed sentence
templates chosen deterministically from the request's prompt hash.
Usage buckets are always zeroed and the model name is always
``"deterministic-stub"`` (cost is therefore always $0).
"""

from __future__ import annotations

import hashlib
import re

from app.providers.llm.models import LLMRequest, LLMResult, UsageBuckets

__all__ = ["DeterministicStubLLM", "MODEL_NAME", "PROVIDER_NAME"]

MODEL_NAME = "deterministic-stub"
PROVIDER_NAME = "deterministic-stub"

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "and", "or", "is", "are",
    "was", "were", "with", "for", "you", "your", "write", "script",
    "video", "topic", "niche", "language", "tone", "rules", "only",
    "ke", "ki", "ka", "aur", "mein", "hai", "hain", "ye", "woh", "kya",
}

_TEMPLATES = [
    "Stub draft about {topic}: the key points are {keywords}. "
    "This offline placeholder covers {topic} in a clear, structured way, "
    "moving from a strong opening hook through the main points to a short conclusion.",
    "Offline stub response for {topic}. Main keywords: {keywords}. "
    "The full version would explain {topic} step by step, with one new point per section "
    "and the most important insight saved for the climax near the end.",
    "Deterministic placeholder text on {topic} (keywords: {keywords}). "
    "Section one introduces {topic}; section two develops the main points; "
    "the final section summarises and closes with a call to action.",
]


def _prompt_text(req: LLMRequest) -> str:
    return "\n".join(m.content for m in req.normalised_messages())


def _extract_topic(text: str) -> str:
    """Best-effort topic extraction: 'Topic:' line, quotes, else keywords."""
    match = re.search(r"(?im)^\s*topic\s*[:\-]\s*(.+)$", text)
    if match:
        return match.group(1).strip().strip('"').strip()[:120]
    quoted = re.findall(r'"([^"\n]{3,80})"', text)
    if quoted:
        return quoted[0].strip()
    kws = _extract_keywords(text, limit=3)
    return " ".join(kws) if kws else "the requested topic"


def _extract_keywords(text: str, limit: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    order: list[str] = []
    for word in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text):
        low = word.lower()
        if low in _STOPWORDS:
            continue
        if low not in counts:
            order.append(low)
        counts[low] = counts.get(low, 0) + 1
    ranked = sorted(order, key=lambda w: (-counts[w], order.index(w)))
    return ranked[:limit]


class DeterministicStubLLM:
    """Deterministic offline stub implementing the ``LLMProvider`` protocol.

    This is an offline stub for integration testing, NOT a real model.
    ``generate()`` is pure and reproducible: it derives its text only
    from the request (topic/keywords echoed from the prompt, template
    chosen by the request's prompt hash), reports zeroed usage buckets
    and model name ``"deterministic-stub"``, and costs $0.
    """

    provider_name = PROVIDER_NAME
    model = MODEL_NAME

    async def generate(self, req: LLMRequest) -> LLMResult:
        """Return a deterministic, rule-based ``LLMResult`` for ``req``."""
        text = self._render(req)
        parsed = None
        if req.output_schema is not None:
            parsed = self._try_parse(req, text)
        return LLMResult(
            text=text,
            parsed=parsed,
            provider=PROVIDER_NAME,
            model=MODEL_NAME,
            tier=req.tier,
            usage=UsageBuckets(),  # zeroed by design — the stub calls nothing
            cost_usd=0.0,
            status="ok",
            attempts=1,
            latency_ms=0.0,
            prompt_version=req.prompt_version,
        )

    # -- internals ---------------------------------------------------------

    def _render(self, req: LLMRequest) -> str:
        prompt = _prompt_text(req)
        topic = _extract_topic(prompt)
        keywords = _extract_keywords(prompt)
        digest = hashlib.sha256(
            f"{req.prompt_hash()}|{req.task or ''}".encode("utf-8")
        ).hexdigest()
        template = _TEMPLATES[int(digest[:8], 16) % len(_TEMPLATES)]
        rendered = template.format(
            topic=topic,
            keywords=", ".join(keywords) if keywords else topic,
        )
        # Respect the caller's output budget approximately
        # (~1 token ≈ 1 word for this stub's simple prose).
        max_words = max(8, int(req.max_output_tokens or 1024))
        words = rendered.split()
        if len(words) > max_words:
            rendered = " ".join(words[:max_words]).rstrip(",;:") + "."
        return rendered

    def _try_parse(self, req: LLMRequest, text: str):
        """Best-effort structured output: fill the schema's str fields.

        Only fields with defaults / Optional / str type are populated;
        on any failure ``parsed`` stays ``None`` rather than raising —
        a stub must never break the pipeline it is stubbing.
        """
        schema = req.output_schema
        if schema is None:
            return None
        try:
            values: dict[str, object] = {}
            for name, field in schema.model_fields.items():  # type: ignore[attr-defined]
                annotation = str(field.annotation)
                if annotation in ("str", "<class 'str'>") or "str" in annotation:
                    values[name] = text
            return schema(**values)  # type: ignore[operator]
        except Exception:
            return None

    # -- convenience for synchronous call sites / tests ---------------------

    def generate_text(self, prompt: str, *, task: str | None = None) -> str:
        """Synchronous one-shot helper: prompt in, stub text out."""
        from app.providers.llm.models import Tier

        req = LLMRequest(tier=Tier.T1, prompt=prompt, task=task)
        return self._render(req)
