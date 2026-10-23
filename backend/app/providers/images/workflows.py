"""Versioned ComfyUI workflow templates shipped with the app.

Users never see a node graph: workflows ship as JSON in
``workflow_templates/`` and the adapter substitutes typed values into
``{{TOKEN}}`` placeholders. A whole-string token is replaced by the
typed value (so ``"{{SEED}}"`` becomes an int); a token embedded in a
longer string is string-substituted.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

WORKFLOW_VERSION = "flux-schnell-txt2img-v1"
_TEMPLATE_FILE = "flux_schnell_txt2img_v1.json"
_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def load_workflow_template(name: str = _TEMPLATE_FILE) -> dict[str, Any]:
    """Load a shipped workflow template (deep-copied per call by caller)."""
    path = Path(__file__).parent / "workflow_templates" / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _substitute(node: Any, values: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    if isinstance(node, str):
        whole = _TOKEN_RE.fullmatch(node)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]

        def _repl(m: re.Match[str]) -> str:
            key = m.group(1)
            return str(values[key]) if key in values else m.group(0)

        return _TOKEN_RE.sub(_repl, node)
    return node


def build_workflow(
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    width: int,
    height: int,
    steps: int,
    cfg: float,
    guidance: float,
    sampler_name: str,
    scheduler: str,
    unet_loader_class: str,
    model_file: str,
    weight_dtype: str,
    template_name: str = _TEMPLATE_FILE,
) -> dict[str, Any]:
    """Build a ready-to-submit workflow graph from the shipped template."""
    graph = copy.deepcopy(load_workflow_template(template_name))
    values: dict[str, Any] = {
        "PROMPT": prompt,
        "NEGATIVE_PROMPT": negative_prompt,
        "SEED": int(seed),
        "WIDTH": int(width),
        "HEIGHT": int(height),
        "STEPS": int(steps),
        "CFG": float(cfg),
        "GUIDANCE": float(guidance),
        "SAMPLER_NAME": sampler_name,
        "SCHEDULER": scheduler,
        "UNET_LOADER_CLASS": unet_loader_class,
        "MODEL_FILE": model_file,
        "WEIGHT_DTYPE": weight_dtype,
    }
    graph = _substitute(graph, values)
    # The GGUF loader node has no weight_dtype input — prune it there.
    for node in graph.values():
        if isinstance(node, dict) and node.get("class_type") == "UnetLoaderGGUF":
            node.get("inputs", {}).pop("weight_dtype", None)
    return graph
