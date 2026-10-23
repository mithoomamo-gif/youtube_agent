"""Event-loop ownership for integration produce runs (Round 4).

OWNERSHIP RULE — the whole integration package follows this:

* **One produce run = one event loop, owned at the top.**
  :func:`app.integration.produce.produce_video` (the top-level run
  entry point) enters :func:`produce_loop` for the whole run.  That
  context creates exactly one loop (via :class:`asyncio.Runner`),
  keeps it open across every stage of the run, and closes it when the
  run ends.  A second, sequential produce run gets a *fresh* loop —
  loops are never reused across runs.

* **Lower layers never own a loop.**  Content
  (:func:`app.integration.content.build_content_auto`), voice
  (:mod:`app.integration.voice_stage`), visuals
  (:mod:`app.integration.visuals_stage`) and stock
  (:class:`app.integration.stock_fetch.StockFetcher`) code must NOT
  call ``asyncio.run()``, ``asyncio.new_event_loop()`` or keep a
  private/persistent loop of their own.  When sync stage code needs a
  coroutine finished, it calls :func:`run_coroutine` from this module,
  which runs the coroutine **on the produce run's loop** when one is
  active.  This is what keeps objects that bind to a loop — notably
  the stock adapter's shared httpx connection pool — valid for every
  call in the run: the Round 3 failure mode was a fresh
  ``asyncio.run()`` per stock fetch closing the loop under the pool,
  so later fetches died with ``RuntimeError: Event loop is closed``
  (and the fetcher's later persistent-private-loop workaround is
  removed in favour of this single owner).

* **Sync facades live only at the outer boundary.**  When
  :func:`run_coroutine` is called with *no* produce run active (a
  standalone CLI / test / REPL caller of e.g.
  ``StockFetcher.fetch_for_scene``), it opens a short-lived
  :func:`produce_loop` for that one top-level call — callers that
  need several calls on one loop wrap them in ``produce_loop()``
  themselves (or simply run a produce run).  When a foreign loop is
  already running in the calling thread (an async host embedding the
  sync stages without a produce run), the coroutine is driven on a
  worker thread that enters its own ``produce_loop()`` — the single
  shared thread-bridge, kept here so no stage module grows its own.
  Async callers should prefer awaiting the async cores directly
  (``build_content_live`` / ``fetch_for_scene_async``).
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

__all__ = [
    "current_produce_loop",
    "produce_loop",
    "run_coroutine",
]


class _ProduceLoop:
    """The one loop of one produce run (backed by ``asyncio.Runner``).

    ``Runner`` is what makes repeated ``run()`` calls on the same loop
    safe: the loop stays open (and its default executor / asyncgen
    bookkeeping stays valid) between calls, and ``close()`` performs
    the full shutdown sequence exactly once, at the end of the run.
    """

    def __init__(self) -> None:
        self._runner = asyncio.Runner()
        self.loop = self._runner.get_loop()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self.loop.is_closed()

    def run(self, coro: Any) -> Any:
        if self.closed:
            raise RuntimeError("produce loop is already closed")
        return self._runner.run(coro)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._runner.close()


#: The active produce run's loop, per context.  Set only by
#: :func:`produce_loop`; worker threads do not inherit it, which is
#: deliberate — a thread that needs the loop enters its own
#: ``produce_loop()`` (see the bridge in :func:`run_coroutine`).
_active_loop: contextvars.ContextVar[_ProduceLoop | None] = contextvars.ContextVar(
    "yta_produce_loop", default=None
)


def current_produce_loop() -> _ProduceLoop | None:
    """The active produce run's loop owner, or None outside a run."""
    owner = _active_loop.get()
    if owner is not None and owner.closed:
        return None
    return owner


@contextmanager
def produce_loop() -> Iterator[_ProduceLoop]:
    """Own the event loop for one produce run (re-entrant).

    Nested entry (a stage re-entering, or a run started inside a run)
    yields the *existing* owner and does not close it — only the
    outermost entry creates and closes the loop.
    """
    existing = current_produce_loop()
    if existing is not None:
        yield existing
        return
    owner = _ProduceLoop()
    token = _active_loop.set(owner)
    try:
        yield owner
    finally:
        _active_loop.reset(token)
        owner.close()


def _as_factory(coro_or_factory: Any) -> Callable[[], Any]:
    if asyncio.iscoroutine(coro_or_factory):
        coro = coro_or_factory
        return lambda: coro
    if callable(coro_or_factory):
        return coro_or_factory  # type: ignore[return-value]
    raise TypeError(
        "run_coroutine() expects a coroutine or a zero-arg coroutine "
        f"factory, got {type(coro_or_factory).__name__}"
    )


def run_coroutine(coro_or_factory: Any) -> Any:
    """Run a coroutine to completion from sync integration code.

    Dispatch, in order (see the module docstring for the full rule):

    1. A produce loop is active -> run on **that** loop.  This is the
       normal in-run path for every stage of a produce run.
    2. No produce loop and no running loop -> this call is the outer
       boundary: open a one-call :func:`produce_loop` for it.
    3. A foreign loop is running in this thread -> drive the
       coroutine on a worker thread that owns its own produce loop
       (the shared thread-bridge for embedded sync callers).
    """
    factory = _as_factory(coro_or_factory)

    owner = current_produce_loop()
    if owner is not None:
        if owner.loop.is_running():
            raise RuntimeError(
                "run_coroutine() cannot block the produce run's loop "
                "from inside that loop; await the coroutine directly "
                "instead"
            )
        return owner.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with produce_loop() as boundary_owner:
            return boundary_owner.run(factory())

    box: dict[str, Any] = {}

    def _thread_main() -> None:
        try:
            with produce_loop() as thread_owner:
                box["value"] = thread_owner.run(factory())
        except BaseException as exc:  # propagate faithfully to caller
            box["error"] = exc

    thread = threading.Thread(target=_thread_main, daemon=True, name="yta-run-coroutine")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
