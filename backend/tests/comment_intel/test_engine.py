"""Comment Intelligence engine tests (Agent 28, Round 5).

Labelled fixtures (see fixtures.py): enthusiastic fans, genuine
questions in English + Roman Urdu, complaints, obvious spam.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.comment_intel import (
    CommentAnalysisEngine,
    CommentAnalysisError,
    CommentAnalysisStore,
    persist_rollup_to_db,
    top_questions_for_channel,
)
from app.comment_intel.deterministic import classify_comment, flag_spam
from app.comment_intel.schema import (
    CommentInput,
    LLMCommentAnalysis,
    LLMQuestion,
    LLMTheme,
    LLMTopic,
    comment_from_row,
)
from app.providers.llm.models import LLMResult, Tier, UsageBuckets

from .fixtures import MIXED_VIDEO, SECOND_VIDEO, VIDEO_ID


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Deterministic mode
# ---------------------------------------------------------------------------


def test_deterministic_rollup_bands_are_sane():
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO, video_id=VIDEO_ID))
    assert rollup.scope == "video"
    assert rollup.comment_count == 11
    assert rollup.spam_count == 2
    assert rollup.analysed_count == 9
    bands = rollup.sentiment
    assert bands.positive == pytest.approx(3 / 9, abs=0.01)
    assert bands.question == pytest.approx(4 / 9, abs=0.01)
    assert bands.negative == pytest.approx(2 / 9, abs=0.01)
    total = bands.positive + bands.neutral + bands.negative + bands.question
    assert total == pytest.approx(1.0, abs=0.01)


def test_deterministic_provenance_is_labelled():
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO))
    prov = rollup.provenance
    assert prov.engine == "deterministic"
    assert prov.model is None
    assert prov.fallback_reason == "llm_not_configured"
    assert prov.sentiment_method == "lexicon_v1"
    assert prov.spam_method == "heuristics_v1"
    assert prov.generated_at


def test_top_questions_are_the_actual_fixture_questions():
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO))
    texts = [q.question_text for q in rollup.top_questions]
    # ranked by likes: q_1 (120) first, q_4 (40) last
    assert texts[0] == "What is the deepest point of the ocean?"
    assert "Kya aap next video mein Mariana Trench par banayenge?" in texts
    assert "How do giant squid survive at that depth?" in texts
    assert "Agli video kab aayegi?" in texts
    assert texts[-1] == "Agli video kab aayegi?"
    by_text = {q.question_text: q for q in rollup.top_questions}
    assert by_text["What is the deepest point of the ocean?"].comment_id == "q_1"
    assert by_text["What is the deepest point of the ocean?"].like_count == 120
    assert by_text["What is the deepest point of the ocean?"].video_id == VIDEO_ID


def test_themes_and_topics_nonempty():
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO))
    assert rollup.themes, "themes must be non-empty on the mixed fixture"
    for theme in rollup.themes:
        assert theme.comment_ids
        assert 0 < theme.share <= 1.0
        assert set(theme.comment_ids) <= {c["id"] for c in MIXED_VIDEO}
    assert rollup.praise_topics
    complaint_labels = {t.label for t in rollup.complaint_topics}
    assert complaint_labels & {"slow", "boring", "audio", "voice", "editing"}


def test_spam_flags_have_reasons_and_ids():
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO))
    flagged = {f.comment_id for f in rollup.spam_flags}
    assert flagged == {"spam_1", "spam_2"}
    for flag in rollup.spam_flags:
        assert flag.reason
        assert flag.video_id == VIDEO_ID
        assert flag.channel_id == "chan-facts"
    reasons = {f.reason for f in rollup.spam_flags}
    assert any(r.startswith("contains_link") or r.startswith("spam_phrase") for r in reasons)


def test_deterministic_mode_fully_reproducible():
    engine = CommentAnalysisEngine()
    a = run(engine.analyse_video(MIXED_VIDEO))
    b = run(engine.analyse_video(MIXED_VIDEO))
    da, db = a.model_dump(), b.model_dump()
    da.pop("provenance"), db.pop("provenance")
    assert da == db


def test_classify_roman_urdu_and_english():
    assert classify_comment(CommentInput(id="1", text="Kya ye sach hai?")) == "question"
    assert classify_comment(CommentInput(id="2", text="Ye video bakwas hai")) == "negative"
    assert classify_comment(CommentInput(id="3", text="Bohat zabardast video")) == "positive"
    assert classify_comment(CommentInput(id="4", text="Video uploaded on Tuesday")) == "neutral"


def test_spam_heuristics_caps_duplicate_repetition():
    dup = CommentInput(id="d1", text="hello friends welcome back to another episode today")
    dup2 = CommentInput(id="d2", text="hello friends welcome back to another episode today")
    caps = CommentInput(id="c1", text="THIS IS THE BEST THING EVER MADE WOW")
    rep = CommentInput(id="r1", text="win win win win win win win prizes")
    flags, spam_ids = flag_spam([dup, dup2, caps, rep])
    reasons = {(f.comment_id, f.reason) for f in flags}
    assert ("d2", "duplicate_text") in reasons
    assert ("c1", "excessive_caps") in reasons
    assert ("r1", "repetitive_words") in reasons
    assert spam_ids == {"d2", "c1", "r1"}


def test_empty_input_rollup():
    rollup = run(CommentAnalysisEngine().analyse_video([]))
    assert rollup.comment_count == 0
    assert rollup.sentiment.positive == 0.0
    assert rollup.top_questions == []
    assert rollup.provenance.engine == "deterministic"


# ---------------------------------------------------------------------------
# LLM mode (mocked router)
# ---------------------------------------------------------------------------


class FakeLLM:
    """Mock router: returns a canned structured analysis."""

    def __init__(self):
        self.requests = []

    async def generate(self, req):
        self.requests.append(req)
        parsed = LLMCommentAnalysis(
            themes=[
                LLMTheme(label="ocean depth", comment_ids=["fan_1", "q_1", "ghost-id"]),
                LLMTheme(label="pacing", comment_ids=["cmp_1", "cmp_2"]),
            ],
            questions=[
                LLMQuestion(question_text="What is the deepest point of the ocean?", comment_id="q_1"),
                LLMQuestion(question_text="Kya aap next video mein Mariana Trench par banayenge?", comment_id="q_2"),
                # paraphrased fake quote — must be dropped, never stored
                LLMQuestion(question_text="When will you upload the next one please?", comment_id="q_4"),
            ],
            praise_topics=[LLMTopic(label="narration voice", comment_ids=["fan_1", "fan_2"])],
            complaint_topics=[LLMTopic(label="slow pacing", comment_ids=["cmp_1", "cmp_2"])],
        )
        return LLMResult(
            text="", parsed=parsed, provider="fake", model="fake-model-1",
            tier=Tier.T1, usage=UsageBuckets(), cost_usd=0.0,
        )


def test_llm_mode_uses_router_and_labels_provenance():
    fake = FakeLLM()
    rollup = run(CommentAnalysisEngine(llm=fake).analyse_video(MIXED_VIDEO))
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.tier == Tier.T1
    assert req.agent_id == "28"
    assert req.output_schema is LLMCommentAnalysis
    prov = rollup.provenance
    assert prov.engine == "llm"
    assert prov.model == "fake-model-1"
    assert prov.fallback_reason is None
    # sentiment/spam stay deterministic + labelled even in LLM mode
    assert prov.sentiment_method == "lexicon_v1"
    assert rollup.sentiment.question == pytest.approx(4 / 9, abs=0.01)
    assert rollup.spam_count == 2


def test_llm_output_is_validated_no_fake_quotes_no_ghost_ids():
    rollup = run(CommentAnalysisEngine(llm=FakeLLM()).analyse_video(MIXED_VIDEO))
    theme_ids = {cid for t in rollup.themes for cid in t.comment_ids}
    assert "ghost-id" not in theme_ids
    assert {t.label for t in rollup.themes} == {"ocean depth", "pacing"}
    qtexts = [q.question_text for q in rollup.top_questions]
    assert "What is the deepest point of the ocean?" in qtexts
    assert "When will you upload the next one please?" not in qtexts
    notes = " ".join(rollup.notes)
    assert "llm_unknown_comment_ids_dropped" in notes
    assert "llm_non_verbatim_questions_dropped" in notes
    assert rollup.praise_topics[0].label == "narration voice"
    assert rollup.complaint_topics[0].label == "slow pacing"


class RaisingLLM:
    async def generate(self, req):
        raise RuntimeError("provider exploded")


def test_llm_failure_is_labelled_deterministic_fallback():
    rollup = run(CommentAnalysisEngine(llm=RaisingLLM()).analyse_video(MIXED_VIDEO))
    prov = rollup.provenance
    assert prov.engine == "deterministic"
    assert prov.model is None
    assert prov.fallback_reason is not None
    assert "RuntimeError" in prov.fallback_reason
    assert "provider exploded" in prov.fallback_reason
    # fallback still produces a full, sane rollup
    assert rollup.top_questions
    assert rollup.themes


def test_llm_failure_strict_mode_raises_typed_error():
    engine = CommentAnalysisEngine(llm=RaisingLLM(), strict=True)
    with pytest.raises(CommentAnalysisError):
        run(engine.analyse_video(MIXED_VIDEO))


# ---------------------------------------------------------------------------
# Channel rollup + TOP-QUESTIONS SEAM
# ---------------------------------------------------------------------------


def test_channel_rollup_aggregates_videos():
    engine = CommentAnalysisEngine()
    channel = run(engine.analyse_channel(MIXED_VIDEO + SECOND_VIDEO))
    assert channel.scope == "channel"
    assert channel.channel_id == "chan-facts"
    assert channel.comment_count == 14
    assert channel.spam_count == 2
    assert channel.analysed_count == 12
    total = (channel.sentiment.positive + channel.sentiment.neutral
             + channel.sentiment.negative + channel.sentiment.question)
    assert total == pytest.approx(1.0, abs=0.01)
    assert channel.themes
    assert {f.comment_id for f in channel.spam_flags} == {"spam_1", "spam_2"}


def test_top_questions_seam_shape_and_ranking():
    engine = CommentAnalysisEngine()
    channel = run(engine.analyse_channel(MIXED_VIDEO + SECOND_VIDEO))
    questions = top_questions_for_channel(channel)
    assert questions, "seam must return questions"
    for q in questions:
        assert set(q.keys()) == {"question_text", "video_id", "comment_id", "like_count"}
    likes = [q["like_count"] for q in questions]
    assert likes == sorted(likes, reverse=True)
    assert questions[0]["question_text"] == "What is the deepest point of the ocean?"
    assert questions[0]["comment_id"] == "q_1"
    texts = {q["question_text"] for q in questions}
    assert "Why did the city disappear so suddenly?" in texts
    assert "Kya aap next video mein Mariana Trench par banayenge?" in texts


def test_top_questions_seam_accepts_video_rollup_list():
    engine = CommentAnalysisEngine()
    v1 = run(engine.analyse_video(MIXED_VIDEO))
    v2 = run(engine.analyse_video(SECOND_VIDEO, video_id="vid-history"))
    questions = top_questions_for_channel([v1, v2])
    assert questions[0]["comment_id"] == "q_1"
    assert any(q["comment_id"] == "q_5" for q in questions)


def test_mixed_engine_channel_rollup_is_not_silently_upgraded():
    det = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO))
    llm = run(CommentAnalysisEngine(llm=FakeLLM()).analyse_video(SECOND_VIDEO, video_id="vid-history"))
    from app.comment_intel import aggregate_channel_rollup

    channel = aggregate_channel_rollup([det, llm], channel_id="chan-facts")
    assert channel.provenance.engine == "deterministic"
    assert channel.provenance.fallback_reason is not None
    assert "mixes engines" in channel.provenance.fallback_reason


# ---------------------------------------------------------------------------
# Persistence seams
# ---------------------------------------------------------------------------


def test_json_store_roundtrip(tmp_path):
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO, video_id=VIDEO_ID))
    store = CommentAnalysisStore(tmp_path)
    path = store.save(rollup)
    assert path.exists()
    loaded = store.load(scope="video", video_id=VIDEO_ID)
    assert loaded is not None
    assert loaded.comment_count == 11
    assert loaded.provenance.engine == "deterministic"
    entries = store.list_entries()
    assert len(entries) == 1 and entries[0]["engine"] == "deterministic"


def test_db_seam_writes_when_tables_exist(tmp_path):
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO, video_id=VIDEO_ID))
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "CREATE TABLE comment_analyses (id TEXT PRIMARY KEY, video_id TEXT, "
        "channel_id TEXT, scope TEXT, engine TEXT, model TEXT, "
        "analysis_json TEXT, created_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE comment_moderation_flags (id TEXT PRIMARY KEY, "
        "comment_id TEXT, video_id TEXT, channel_id TEXT, reason TEXT, "
        "status TEXT, created_at INTEGER)"
    )
    report = persist_rollup_to_db(conn, rollup)
    assert report["db_comment_analyses"] is True
    assert report["db_moderation_flags"] == len(rollup.spam_flags)
    assert set(report["tables_present"]) == {"comment_analyses", "comment_moderation_flags"}
    row = conn.execute("SELECT engine, video_id FROM comment_analyses").fetchone()
    assert row == ("deterministic", VIDEO_ID)
    n_flags = conn.execute("SELECT COUNT(*) FROM comment_moderation_flags").fetchone()[0]
    assert n_flags == len(rollup.spam_flags)


def test_db_seam_no_tables_is_honest_noop(tmp_path):
    rollup = run(CommentAnalysisEngine().analyse_video(MIXED_VIDEO))
    conn = sqlite3.connect(tmp_path / "empty.db")
    report = persist_rollup_to_db(conn, rollup)
    assert report == {"tables_present": [], "db_comment_analyses": False,
                      "db_moderation_flags": 0}


def test_real_comments_store_roundtrip(tmp_path):
    """Adaptation to the landed schema worker store (app.comments):
    comments are read FROM CommentsStore, the rollup and spam flags
    are written back THROUGH it (comment_analyses +
    comment_moderation_flags rows, owner-scoped)."""
    from app.comments import CommentsStore, ensure_comments_schema

    conn = sqlite3.connect(tmp_path / "real.db")
    conn.execute("CREATE TABLE channels (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO channels (id) VALUES ('chan-facts')")
    ensure_comments_schema(conn)
    conn.commit()
    store = CommentsStore(conn)
    for comment in MIXED_VIDEO:
        store.upsert_comment("chan-facts", comment)

    rollup, report = run(
        CommentAnalysisEngine().analyse_stored_video(store, "chan-facts", VIDEO_ID)
    )
    assert rollup.comment_count == 11
    assert rollup.spam_count == 2
    assert report["store_analysis"] is True
    assert report["error"] is None
    assert report["store_moderation_flags"] == len(rollup.spam_flags)
    assert report["flags_skipped"] == 0

    saved = store.latest_analysis("chan-facts", scope="video", video_id=VIDEO_ID)
    assert saved is not None
    assert saved["engine"] == "deterministic"
    assert saved["model_id"] is None
    assert saved["comment_count"] == 11
    assert saved["rollup"]["comment_count"] == 11
    assert saved["provenance"]["engine"] == "deterministic"
    assert saved["provenance"]["fallback_reason"] == "llm_not_configured"

    flags = store.list_moderation_flags("chan-facts", status="pending")
    assert {f["comment_id"] for f in flags} == {"spam_1", "spam_2"}
    assert all(f["flag_kind"] == "spam" for f in flags)
    assert all(f["suggested_action"] == "review" for f in flags)
    # owner scoping: another channel sees nothing
    assert store.list_moderation_flags("chan-other") == []
    assert store.list_analyses("chan-other") == []


def test_comment_from_row_accepts_sqlite_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE comments (id TEXT, video_id TEXT, channel_id TEXT, "
        "author_display_name TEXT, text TEXT, like_count INTEGER, "
        "published_at TEXT, reply_count INTEGER)"
    )
    conn.execute(
        "INSERT INTO comments VALUES ('x1','v1','ch1','Areeb','Great video, very helpful',5,'2026-10-20',2)"
    )
    row = conn.execute("SELECT * FROM comments").fetchone()
    comment = comment_from_row(row)
    assert comment.id == "x1" and comment.like_count == 5
    rollup = run(CommentAnalysisEngine().analyse_video([row]))
    assert rollup.comment_count == 1
    assert rollup.sentiment.positive == pytest.approx(1.0)
