"""Contract tests for the Round 2 ``app.execution`` package.

These tests pin the *shared surface* the execution workers agreed on —
module names, constant values, function/class presence — so any worker's
module can land (or be refactored) without silently breaking the others.
They intentionally assert contracts, not behaviour: behavioural tests
live with each module.

Every sibling-module check uses :func:`pytest.importorskip` on that
specific module, so this file stays green while the package is still
landing piece by piece; a skipped test here means "module not built
yet", never "contract failed".  The fixture smoke tests at the bottom
have no skip: ``tests/execution_fixtures.py`` is part of this deliverable
and must always work.

Run: ``.venv/bin/python -m pytest tests/test_execution_contracts.py``
"""

from __future__ import annotations

import pytest

# The one canonical DAG order (app/execution/dag.py, STAGES).  Jobs are
# chained through a single ``depends_on`` link in exactly this order;
# gate.a sits after the script stage, gate.b is the terminal gate.
EXPECTED_STAGES: list[str] = [
    "stage.research",
    "stage.script",
    "gate.a",
    "stage.voice",
    "stage.visuals",
    "stage.render",
    "stage.thumbnail",
    "stage.qc",
    "stage.package",
    "gate.b",
]

# The five mandated pool limits (app/execution/pools.py, POOL_LIMITS).
# Extra pools may exist; these five values are pinned.
EXPECTED_POOL_LIMITS: dict[str, int] = {
    "render": 1,
    "tts": 4,
    "llm": 8,
    "downloads": 6,
    "upload": 2,
}

# The event catalogue (app/execution/events.py -> app.ws_hub.WSHub).
EXPECTED_EVENT_TYPES: list[str] = [
    "agent.status_changed",
    "pipeline.phase_changed",
    "cost.ticker",
    "gate.pending",
    "budget.warning",
]


# ---------------------------------------------------------------------------
# Normalisation helpers (tolerant about *representation*, strict about values)
# ---------------------------------------------------------------------------


def _stage_kinds(stages) -> list[str]:
    """Extract kind strings from STAGES whatever shape entries take:
    plain strings, dicts with a ``kind``/``name`` key, objects with a
    ``kind``/``name`` attribute, or ``(kind, ...)`` tuples."""
    if isinstance(stages, dict):
        return [str(k) for k in stages.keys()]
    kinds: list[str] = []
    for entry in stages:
        if isinstance(entry, str):
            kinds.append(entry)
        elif isinstance(entry, dict):
            kinds.append(str(entry.get("kind") or entry.get("name")))
        elif isinstance(entry, (tuple, list)) and entry:
            kinds.append(str(entry[0]))
        else:
            kinds.append(str(getattr(entry, "kind", getattr(entry, "name", entry))))
    return kinds


def _callable_handler(handler) -> bool:
    if callable(handler):
        return True
    return any(
        callable(getattr(handler, attr, None))
        for attr in ("handle", "run", "execute", "__call__")
    )


# ---------------------------------------------------------------------------
# (a) DAG chain
# ---------------------------------------------------------------------------


def test_dag_stages_exact_order():
    dag = pytest.importorskip("app.execution.dag")
    stages = getattr(dag, "STAGES", None)
    assert stages is not None, "app.execution.dag must expose STAGES"
    assert _stage_kinds(stages) == EXPECTED_STAGES


# ---------------------------------------------------------------------------
# (b) Pools
# ---------------------------------------------------------------------------


def test_pool_limits_mandated_values():
    pools = pytest.importorskip("app.execution.pools")
    limits = getattr(pools, "POOL_LIMITS", None)
    assert isinstance(limits, dict), "pools.POOL_LIMITS must be a dict"
    for pool, limit in EXPECTED_POOL_LIMITS.items():
        assert limits.get(pool) == limit, (
            f"POOL_LIMITS[{pool!r}] must be {limit}, got {limits.get(pool)!r}"
        )


def test_every_stage_kind_maps_to_a_pool():
    pools = pytest.importorskip("app.execution.pools")
    dag = pytest.importorskip("app.execution.dag")
    mapping = getattr(pools, "KIND_TO_POOL", None)
    assert mapping is not None, "pools must expose KIND_TO_POOL"
    limits = getattr(pools, "POOL_LIMITS", {})
    kinds = _stage_kinds(getattr(dag, "STAGES", EXPECTED_STAGES))

    def pool_for(kind: str):
        if isinstance(mapping, dict):
            return mapping.get(kind)
        if callable(mapping):  # tolerate a resolver function
            return mapping(kind)
        getter = getattr(mapping, "get", None)
        return getter(kind) if callable(getter) else None

    for kind in kinds:
        pool = pool_for(kind)
        assert pool is not None, f"stage kind {kind!r} has no pool in KIND_TO_POOL"
        assert pool in limits, (
            f"stage kind {kind!r} maps to pool {pool!r} which is not in POOL_LIMITS"
        )


# ---------------------------------------------------------------------------
# (c) Handler registry
# ---------------------------------------------------------------------------


def _build_registry(tmp_path):
    """Call handlers.build_registry tolerating the plausible signatures:
    zero-arg, (conn_factory, hub), or keyword variants thereof."""
    handlers = pytest.importorskip("app.execution.handlers")
    build = getattr(handlers, "build_registry", None)
    assert callable(build), "handlers must expose a callable build_registry"

    try:
        return build()
    except TypeError:
        pass

    from tests.execution_fixtures import RecordingHub, make_db

    _, factory = make_db(tmp_path / "registry")
    hub = RecordingHub()
    attempts = [
        lambda: build(factory, hub),
        lambda: build(factory, hub=hub),
        lambda: build(conn_factory=factory, hub=hub),
        lambda: build(conn_factory=factory, emitter=hub),
        lambda: build(factory),
    ]
    last_exc: Exception | None = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_exc = exc
    raise AssertionError(
        f"build_registry rejected every known calling convention: {last_exc!r}"
    )


def _registry_mapping(registry) -> dict:
    if isinstance(registry, dict):
        return registry
    for attr in ("handlers", "registry", "kinds"):
        value = getattr(registry, attr, None)
        if isinstance(value, dict):
            return value
        if callable(value):
            try:
                result = value()
            except TypeError:
                continue
            if isinstance(result, dict):
                return result
    if callable(getattr(registry, "get", None)) and callable(
        getattr(registry, "keys", None)
    ):
        return {key: registry.get(key) for key in registry.keys()}
    raise AssertionError(
        f"cannot interpret build_registry() result of type {type(registry)!r} "
        "as a kind -> handler mapping"
    )


def test_handlers_registry_covers_all_stages(tmp_path):
    registry = _registry_mapping(_build_registry(tmp_path))
    for kind in EXPECTED_STAGES:
        assert kind in registry, f"registry is missing handler for {kind!r}"
        assert _callable_handler(registry[kind]), (
            f"handler for {kind!r} is not callable: {registry[kind]!r}"
        )


# ---------------------------------------------------------------------------
# (d) Event emitter surface
# ---------------------------------------------------------------------------


# EventEmitter's semantic helper methods, one per catalogue event type
# (app/execution/events.py module docstring is the mapping's source):
#   agent_status -> agent.status_changed, phase_changed ->
#   pipeline.phase_changed, cost_ticker -> cost.ticker,
#   gate_pending -> gate.pending, budget_warning -> budget.warning.
EXPECTED_EMITTER_METHODS: list[str] = [
    "agent_status",
    "phase_changed",
    "cost_ticker",
    "gate_pending",
    "budget_warning",
]


def test_event_emitter_surface(tmp_path):
    events = pytest.importorskip("app.execution.events")
    emitter_cls = getattr(events, "EventEmitter", None)
    assert emitter_cls is not None, "events must expose EventEmitter"

    for name in EXPECTED_EMITTER_METHODS:
        assert callable(getattr(emitter_cls, name, None)), (
            f"EventEmitter is missing the {name}() helper"
        )

    # Behavioural pin: driving the helpers through a RecordingHub must
    # produce exactly the five catalogue event types, in call order.
    from tests.execution_fixtures import RecordingHub, make_db, seed_channel_niche, seed_project

    _, factory = make_db(tmp_path / "events")
    channel_id, niche_id = seed_channel_niche(factory)
    project_id = seed_project(factory, channel_id, niche_id)

    hub = RecordingHub()
    emitter = emitter_cls(hub)
    emitter.agent_status({"id": "job-1", "project_id": project_id}, "running")
    emitter.phase_changed(project_id, "production")
    emitter.cost_ticker(project_id, 100, 100)
    emitter.gate_pending(project_id, "a", "approval-1")
    emitter.budget_warning(project_id)
    assert hub.types() == EXPECTED_EVENT_TYPES


# ---------------------------------------------------------------------------
# (e) Gates bridge
# ---------------------------------------------------------------------------


def test_gates_bridge_surface():
    bridge = pytest.importorskip("app.execution.gates_bridge")
    for name in ("ensure_approval", "apply_decision", "resume_graph"):
        fn = getattr(bridge, name, None)
        assert callable(fn), f"gates_bridge must expose a callable {name}"


# ---------------------------------------------------------------------------
# (f) Runner
# ---------------------------------------------------------------------------


def test_runner_surface():
    runner = pytest.importorskip("app.execution.runner")
    assert getattr(runner, "JobRunner", None) is not None, (
        "runner must expose JobRunner"
    )
    gate_pending = getattr(runner, "GatePending", None)
    assert gate_pending is not None, "runner must expose GatePending"
    assert isinstance(gate_pending, type) and issubclass(gate_pending, Exception), (
        "GatePending must be an Exception subclass (handlers raise it as "
        "control flow when a gate needs a human decision)"
    )


# ---------------------------------------------------------------------------
# (g) Scheduler
# ---------------------------------------------------------------------------


def test_scheduler_tick_surface():
    scheduler = pytest.importorskip("app.execution.scheduler")
    tick_cls = getattr(scheduler, "SchedulerTick", None)
    assert tick_cls is not None, "scheduler must expose SchedulerTick"
    assert callable(getattr(tick_cls, "tick", None)), (
        "SchedulerTick must have a callable tick method"
    )
    # NOTE: catch-up is a *policy inside tick* (missed slots collapse
    # into one materialised run), not a separate method — see the
    # scheduler module docstring.  There is deliberately no
    # catch_up_once assertion here.


# ---------------------------------------------------------------------------
# Fixture smoke tests (no importorskip — these helpers ship with this file's
# deliverable and must always work)
# ---------------------------------------------------------------------------


def test_fixtures_make_db_and_seed_roundtrip(tmp_path):
    from tests.execution_fixtures import make_db, seed_channel_niche, seed_project

    db_path, factory = make_db(tmp_path)
    assert db_path.exists()

    channel_id, niche_id = seed_channel_niche(factory)
    project_id = seed_project(factory, channel_id, niche_id)
    assert project_id

    conn = factory()
    try:
        row = conn.execute(
            "SELECT channel_id, niche_profile_id, publish_mode, topic "
            "FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == channel_id and row[1] == niche_id
        assert row[2] == "review_first" and row[3] == "Test topic"
        channel = conn.execute(
            "SELECT default_niche_profile_id FROM channels WHERE id = ?",
            (channel_id,),
        ).fetchone()
        assert channel[0] == niche_id
    finally:
        conn.close()


def test_fixtures_seed_uniqueness_defaults(tmp_path):
    from tests.execution_fixtures import make_db, seed_channel_niche

    _, factory = make_db(tmp_path)
    first = seed_channel_niche(factory)
    second = seed_channel_niche(factory)  # must not collide on UNIQUE cols
    assert first[0] != second[0] and first[1] != second[1]


def test_fixtures_recording_hub_records_envelopes():
    from tests.execution_fixtures import RecordingHub

    hub = RecordingHub()
    env = hub.publish(
        "gate.pending", {"gate": "A"}, project_id="p1", job_id="j1"
    )
    hub.publish("cost.ticker", {"cost_micros": 5}, project_id="p1")

    assert env["type"] == "gate.pending"
    assert env["payload"] == {"gate": "A"}
    assert env["project_id"] == "p1" and env["job_id"] == "j1"
    assert hub.types() == ["gate.pending", "cost.ticker"]
    assert len(hub.by_type("cost.ticker")) == 1
    assert hub.last()["type"] == "cost.ticker"
    assert [e["seq"] for e in hub.events] == [1, 2]
    hub.clear()
    assert len(hub) == 0
