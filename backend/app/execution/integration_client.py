"""Lazy bridge to the Round-2 integration package (``app.integration``).

The integration package (real end-to-end production: research -> script
-> voice -> visuals -> render -> pack) is built by a separate team,
concurrently with this execution layer.  It may not exist yet, and even
when it exists it grows stage functions incrementally.  This module is
therefore the *only* place the execution layer touches it, and it does
so defensively:

* ``app.integration`` is **never** imported at module top level — the
  import happens inside :func:`get_integration`, on every call, so a
  package that lands (or gains a stage) after this module was imported
  is picked up without a restart.
* :func:`call_stage` returns ``None`` whenever the integration path is
  unavailable (package missing, stage function missing, stage declined
  by returning ``None``).  Callers do **not** treat ``None`` as "use
  the stub path" by default (R4-003): in
  :mod:`app.execution.handlers` a ``None`` result is a stage failure
  — logged, recorded as a ``failed`` agent run, and raised as
  ``StageFailedError`` so the runner's failure policy applies
  (retry exactly once, then park the project ``failed``) — *unless*
  stub / offline mode was explicitly selected for that job
  (``handlers._stub_mode_selected``), in which case the caller falls
  through to its deterministic stub path.
* Exceptions raised *by* a stage function itself propagate to the
  caller — :mod:`app.execution.handlers` logs them (with traceback),
  and likewise turns them into a ``StageFailedError`` failure unless
  stub / offline mode was explicitly selected for the job, in which
  case it falls back to the stub path.
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

__all__ = ["get_integration", "call_stage"]


def get_integration():
    """Return the ``app.integration`` module, or ``None`` if unavailable.

    The import is deliberately performed on every call (cheap once the
    module is in ``sys.modules``) so tests can substitute a fake module
    via ``sys.modules`` and late-landing code is seen immediately.
    """
    try:
        return importlib.import_module("app.integration")
    except Exception:
        return None


def call_stage(stage: str, project: dict, payload: dict) -> Optional[dict]:
    """Run the real integration stage ``run_<stage>`` if it exists.

    ``stage`` is the short stage name (``"research"``, ``"script"``,
    ``"voice"``, ... — see :data:`app.execution.dag.STAGE_BY_KIND`).

    Returns the stage's dict result, or ``None`` when there is no real
    implementation to call.  ``None`` means "no result" — the caller
    (:mod:`app.execution.handlers`) treats it as a stage failure and
    raises ``StageFailedError`` unless stub / offline mode was
    explicitly selected for that job, in which case it falls back to
    its stub.  A result that is not a dict but exposes
    ``model_dump()`` (pydantic) is converted; any other non-dict
    result is treated as "no result".  Exceptions from the stage
    function propagate to the caller.
    """
    module = get_integration()
    if module is None:
        return None
    fn: Any = getattr(module, f"run_{stage}", None)
    if not callable(fn):
        return None
    result = fn(project, payload)
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else None
    return None
