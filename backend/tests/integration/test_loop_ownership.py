"""Regression tests for Round-4 event-loop ownership.

The Round-3 failure shape: stock fetching inside a produce run used a
fresh ``asyncio.run()`` per call.  The shared provider adapter keeps an
httpx connection pool bound to the loop it first ran on, so the second
fetch in the same run (and any later produce run in the same process)
died with ``RuntimeError: Event loop is closed`` and silently fell
back to plates.  The fetcher's later workaround — a private persistent
loop per fetcher — is also gone.

Rule under test (``app.integration.run_loop``): ONE loop per produce
run, owned by ``produce_video`` / ``produce_loop()``; lower layers
(``StockFetcher.fetch_for_scene`` here) run on that loop and own none.
These tests simulate the loop-bound pool directly: the fake adapter
remembers the first loop it ran on and raises ``Event loop is closed``
if a later call arrives on a different or closed loop — exactly what
the real httpx pool does.
"""

from __future__ import annotations

import asyncio

from app.integration import produce
from app.integration.models import ProjectSpec
from app.integration.run_loop import current_produce_loop, produce_loop
from app.integration.stock_fetch import StockFetcher
from app.providers.stock.models import Candidate, MediaType


# ---------------------------------------------------------------------------
# Loop-bound fake adapter (mirrors PixabayProvider's pooled httpx client)
# ---------------------------------------------------------------------------


def _video(asset_id: str) -> Candidate:
    return Candidate(
        provider="pixabay",
        provider_asset_id=str(asset_id),
        media_type=MediaType.VIDEO,
        download_url=f"https://cdn.example.test/video/{asset_id}.mp4",
        page_url=f"https://pixabay.com/videos/id-{asset_id}/",
        duration_s=30.0,
        width=1920,
        height=1080,
        fps=30.0,
        tags=["deep", "ocean"],
        downloads=100,
    )


class LoopBoundAdapter:
    """Async stock adapter whose "pool" is bound to its first loop.

    ``results`` maps query -> list[Candidate].  Every search/download
    records the running loop; a call on any other loop, or after the
    bound loop closed, raises RuntimeError("Event loop is closed") —
    the exact failure the old per-call ``asyncio.run()`` produced.
    Deliberately has NO ``cache`` attribute (the fetcher's own file
    search cache is exercised instead).
    """

    name = "pixabay"

    def __init__(self, results):
        self.results = dict(results or {})
        self.pool_loop = None
        self.loops_seen: list = []
        self.download_calls: list[str] = []

    def _bind(self):
        loop = asyncio.get_running_loop()
        self.loops_seen.append(loop)
        if self.pool_loop is None:
            self.pool_loop = loop
        if self.pool_loop.is_closed() or loop is not self.pool_loop:
            raise RuntimeError("Event loop is closed")
        return loop

    async def search(self, query, media_type, filters=None):
        self._bind()
        kind = "video" if media_type == MediaType.VIDEO else "photo"
        return list(self.results.get((query, kind), []))

    async def download(self, candidate):
        self._bind()
        self.download_calls.append(str(candidate.provider_asset_id))
        return f"fake-bytes-for-{candidate.provider_asset_id}".encode()


def _spec() -> ProjectSpec:
    return ProjectSpec(niche="facts", topic="Ocean depth secrets", minutes=1.0)


# ---------------------------------------------------------------------------
# (a) The regression shape at fetcher level: two sequential flows in one
#     process, several fetches each, all on the run's single loop.
# ---------------------------------------------------------------------------


def test_two_produce_style_flows_sequentially_no_loop_errors(tmp_path):
    run_loops = []
    for run in (1, 2):
        adapter = LoopBoundAdapter(
            {
                (f"scene one run{run}", "video"): [_video(f"r{run}-a")],
                (f"scene two run{run}", "video"): [_video(f"r{run}-b")],
            }
        )
        # One fetcher per run, as the visuals stage constructs it.
        fetcher = StockFetcher(adapter=adapter, cache_dir=tmp_path / f"stock{run}")
        assert not hasattr(fetcher, "_sync_loop"), (
            "the fetcher's private persistent-loop workaround must be gone"
        )
        with produce_loop() as owner:
            first = fetcher.fetch_for_scene(5.0, [f"scene one run{run}"])
            second = fetcher.fetch_for_scene(5.0, [f"scene two run{run}"])
            assert current_produce_loop() is owner
        # Correct results for BOTH fetches of the flow: with per-call
        # asyncio.run the second fetch's pool access raised inside the
        # fetcher and came back as None (silent plate fallback).
        assert first is not None and first.provider_asset_id == f"r{run}-a"
        assert second is not None and second.provider_asset_id == f"r{run}-b"
        # Every adapter call in the flow ran on ONE loop: the owner's.
        assert adapter.loops_seen, "adapter was never called"
        assert all(loop is owner.loop for loop in adapter.loops_seen)
        # The owner closed its loop at the end of the flow...
        assert owner.loop.is_closed()
        run_loops.append(owner.loop)
    # ...and the second flow got a FRESH loop, not the closed first one.
    assert run_loops[0] is not run_loops[1]
    assert current_produce_loop() is None


# ---------------------------------------------------------------------------
# (b) Full produce level: two produce_video runs back-to-back in one
#     process, each fetching stock for two scenes through the real
#     StockFetcher, with the other stages faked (conftest fixtures).
# ---------------------------------------------------------------------------


def test_two_sequential_produce_runs_share_one_loop_per_run(
    monkeypatch, stage_fakes, tmp_path
):
    def rec(ret):
        return lambda *args: ret

    monkeypatch.setattr(produce, "build_content", rec(stage_fakes.bundle))
    monkeypatch.setattr(produce, "run_gate_a", rec((True, [])))
    monkeypatch.setattr(produce, "synthesize_voice", rec(stage_fakes.voice))
    monkeypatch.setattr(produce, "assemble_project", rec(stage_fakes.assemble))
    monkeypatch.setattr(produce, "finalize_publish", rec(stage_fakes.publish))

    owner_loops = []
    for run in (1, 2):
        adapter = LoopBoundAdapter(
            {
                (f"scene one run{run}", "video"): [_video(f"p{run}-a")],
                (f"scene two run{run}", "video"): [_video(f"p{run}-b")],
            }
        )
        fetcher = StockFetcher(adapter=adapter, cache_dir=tmp_path / f"pstock{run}")
        fetched: list[str] = []

        def fake_visuals(bundle, voice, profile, ctx, _fetcher=fetcher, _run=run):
            for query in (f"scene one run{_run}", f"scene two run{_run}"):
                asset = _fetcher.fetch_for_scene(5.0, [query])
                assert asset is not None, (
                    "stock fetch inside a produce run returned None — "
                    "the loop-bound adapter raised 'Event loop is closed'"
                )
                fetched.append(asset.provider_asset_id)
            owner = current_produce_loop()
            assert owner is not None, "produce run had no loop owner"
            owner_loops.append(owner.loop)
            return stage_fakes.visuals

        monkeypatch.setattr(produce, "plan_and_generate_visuals", fake_visuals)
        result = produce.produce_video(_spec(), tmp_path / f"out{run}")

        # Correct results, identical provenance shape, both runs.
        assert result.gate_a_passed is True
        assert result.gate_b_passed is True
        assert result.publish_state == "ready_for_review"
        assert result.title == stage_fakes.bundle.title
        assert result.paths_used["llm"] == "deterministic-stub"
        assert result.paths_used["publish"] == "fake-provider"
        assert fetched == [f"p{run}-a", f"p{run}-b"]
        # All adapter work in this run happened on the run's one loop.
        assert adapter.loops_seen
        assert all(loop is owner_loops[-1] for loop in adapter.loops_seen)

    # One loop per run: distinct objects, and run 1's loop was closed
    # before run 2 started (a reused closed loop is the old bug).
    assert owner_loops[0] is not owner_loops[1]
    assert owner_loops[0].is_closed()
    assert owner_loops[1].is_closed()
    assert current_produce_loop() is None
