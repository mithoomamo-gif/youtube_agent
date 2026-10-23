"""Live event emission for the execution layer (Round 2, Worker 4).

Thin, *never-raising* wrapper around the WebSocket hub
(:class:`app.ws_hub.WSHub`).  Execution code (runner / gate bridge /
pipeline) calls the semantic helpers on :class:`EventEmitter` instead of
formatting WS event names itself:

==========================  ==========================
method                      WS event type
==========================  ==========================
``agent_status``            ``agent.status_changed``
``phase_changed``           ``pipeline.phase_changed``
``cost_ticker``             ``cost.ticker``
``gate_pending``            ``gate.pending``
``budget_warning``          ``budget.warning``
==========================  ==========================

Every payload is a plain ``dict`` carrying the method's fields plus
``ts`` (epoch **milliseconds**).  Every publish is wrapped in
``try/except``: events are observability, never control flow — a broken
or absent hub must never fail a job, a gate, or a render.
"""

from __future__ import annotations

import time
from typing import Any, Optional

__all__ = ["EventEmitter", "emit"]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _field(obj: Any, *names: str) -> Any:
    """Best-effort field lookup on a dict / Row / object / plain string.

    A bare string is treated as an id (for the first name ending in
    ``id`` / equal to ``id``), which lets callers pass ``job="j1"``.
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        for name in names:
            if name == "id" or name.endswith("_id"):
                return obj
        return None
    if isinstance(obj, dict):
        for name in names:
            if obj.get(name) is not None:
                return obj.get(name)
        return None
    # sqlite3.Row and other mapping-like objects expose .keys().
    keys = getattr(obj, "keys", None)
    if callable(keys):
        try:
            available = set(keys())
            for name in names:
                if name in available and obj[name] is not None:
                    return obj[name]
            return None
        except Exception:
            pass
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def emit(hub: Any, type: str, payload: Optional[dict] = None, **kw: Any) -> Optional[dict]:
    """Publish one event through ``hub``; never raises.

    ``type`` is the WS event type (e.g. ``"gate.pending"``), ``payload``
    a plain dict (``ts`` in epoch ms is added when missing) and ``**kw``
    is forwarded verbatim to ``hub.publish`` (typically ``project_id=``
    / ``job_id=`` scope).  Returns the hub's envelope, or ``None`` when
    there is no hub or publishing failed.
    """
    if hub is None:
        return None
    try:
        body: dict[str, Any] = dict(payload or {})
        body.setdefault("ts", _now_ms())
        return hub.publish(type, body, **kw)
    except Exception:
        return None


class EventEmitter:
    """Semantic event helpers bound to an optional hub.

    ``hub`` may be a real :class:`app.ws_hub.WSHub`, any object with a
    compatible ``publish(event_type, payload, *, project_id, job_id,
    **scope)`` method (e.g. a recording fake in tests), or ``None``
    (all methods become silent no-ops).
    """

    def __init__(self, hub: Any = None) -> None:
        self.hub = hub

    # -- internal ------------------------------------------------------

    def _publish(
        self,
        event_type: str,
        payload: dict,
        *,
        project_id: Any = None,
        job_id: Any = None,
    ) -> Optional[dict]:
        if self.hub is None:
            return None
        try:
            return self.hub.publish(
                event_type, payload, project_id=project_id, job_id=job_id
            )
        except Exception:
            return None

    # -- semantic helpers ----------------------------------------------

    def agent_status(self, job: Any, status: str, **extra: Any) -> Optional[dict]:
        """``agent.status_changed`` for a job/agent transition."""
        try:
            extra = dict(extra)
            job_id = extra.pop("job_id", None) or _field(job, "id", "job_id")
            project_id = extra.pop("project_id", None) or _field(
                job, "project_id"
            )
            payload: dict[str, Any] = {}
            if job_id is not None:
                payload["job_id"] = job_id
            if project_id is not None:
                payload["project_id"] = project_id
            agent_key = extra.pop(
                "agent_key", None
            ) or _field(job, "agent_key", "agent", "kind")
            if agent_key is not None:
                payload["agent_key"] = agent_key
            kind = _field(job, "kind")
            if kind is not None:
                payload.setdefault("kind", kind)
            payload.update(extra)
            # Core fields win over extras.
            payload["status"] = status
            if job_id is not None:
                payload["job_id"] = job_id
            if project_id is not None:
                payload["project_id"] = project_id
            payload["ts"] = _now_ms()
            return self._publish(
                "agent.status_changed",
                payload,
                project_id=project_id,
                job_id=job_id,
            )
        except Exception:
            return None

    def phase_changed(
        self, project_id: Any, phase: Any, **extra: Any
    ) -> Optional[dict]:
        """``pipeline.phase_changed`` when a project enters a new phase."""
        try:
            extra = dict(extra)
            job_id = extra.pop("job_id", None)
            payload: dict[str, Any] = dict(extra)
            payload["project_id"] = project_id
            payload["phase"] = phase
            payload["ts"] = _now_ms()
            return self._publish(
                "pipeline.phase_changed",
                payload,
                project_id=project_id,
                job_id=job_id,
            )
        except Exception:
            return None

    def cost_ticker(
        self,
        project_id: Any,
        cost_micros: Any,
        total_cost_micros: Any,
        **extra: Any,
    ) -> Optional[dict]:
        """``cost.ticker`` — incremental + cumulative cost (micros)."""
        try:
            extra = dict(extra)
            job_id = extra.pop("job_id", None)
            payload: dict[str, Any] = dict(extra)
            payload["project_id"] = project_id
            payload["cost_micros"] = cost_micros
            payload["total_cost_micros"] = total_cost_micros
            payload["ts"] = _now_ms()
            return self._publish(
                "cost.ticker", payload, project_id=project_id, job_id=job_id
            )
        except Exception:
            return None

    def gate_pending(
        self, project_id: Any, gate: Any, approval_id: Any, **extra: Any
    ) -> Optional[dict]:
        """``gate.pending`` — a human gate is waiting for a decision."""
        try:
            extra = dict(extra)
            job_id = extra.pop("job_id", None)
            payload: dict[str, Any] = dict(extra)
            payload["project_id"] = project_id
            payload["gate"] = gate
            payload["approval_id"] = approval_id
            payload["ts"] = _now_ms()
            return self._publish(
                "gate.pending", payload, project_id=project_id, job_id=job_id
            )
        except Exception:
            return None

    def budget_warning(
        self, project_id: Any, **extra: Any
    ) -> Optional[dict]:
        """``budget.warning`` — a budget cap is near / reached."""
        try:
            extra = dict(extra)
            job_id = extra.pop("job_id", None)
            payload: dict[str, Any] = dict(extra)
            payload["project_id"] = project_id
            payload["ts"] = _now_ms()
            return self._publish(
                "budget.warning", payload, project_id=project_id, job_id=job_id
            )
        except Exception:
            return None
