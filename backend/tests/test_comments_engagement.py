"""Comment Engagement service (Round 5 — Agent 28, ENGAGEMENT half).

Covers, against the real schema + the schema worker's CommentsStore:

* draft lifecycle: generate -> proposed -> approve | reject, with
  guarded transitions (terminal drafts cannot be re-decided);
* generation provenance in BOTH engine modes — LLM success labelled
  ``engine='llm'`` with the model recorded; LLM missing / failing /
  unusable output labelled ``engine='deterministic'`` WITH a
  ``fallback_reason`` (R4-003 honesty law, never a silent swap);
* language inference: Urdu script / Roman Urdu / Hindi / English
  comments get drafts in the comment's own language;
* the NOT-WIRED posting boundary: every draft view carries
  ``posted is False`` / ``posting == 'not_wired'``, approving
  performs NO external call (provider layer + HTTP layer mocked and
  asserted untouched), and the engagement source contains zero
  YouTube write-endpoint strings;
* moderation queue: pending flags listed with comment context +
  suggested-action LABELS, human decisions (dismiss / mark_reviewed)
  recorded locally, terminal flags cannot be re-decided, and no
  decision ever reports an executed action;
* owner-channel isolation: cross-channel generate / decide reads
  and writes are refused.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx
import pytest

from app.comments.engagement import (
    CommentEngagementService,
    DraftGenerationError,
    IllegalTransitionError,
    detect_language,
)
from app.comments.store import (
    CommentsNotFoundError,
    CommentsScopeError,
    CommentsStore,
)
from app.providers.llm.models import LLMResult, Tier
from tests.execution_fixtures import make_db

VIDEO_A = "videoAAA111"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _seed_channel(factory, label: str) -> str:
    channel_id = str(uuid.uuid4())
    ts = int(time.time() * 1000)
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO channels (id, youtube_channel_id, title, language,"
            " status, created_at, updated_at) VALUES (?, ?, ?, 'en-US',"
            " 'active', ?, ?)",
            (channel_id, f"UC-owner-{label}-{uuid.uuid4().hex[:8]}",
             f"Owner {label}", ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return channel_id


@pytest.fixture()
def env(tmp_path):
    _db_path, factory = make_db(tmp_path)
    store = CommentsStore(factory)
    return {
        "factory": factory,
        "store": store,
        "channel_a": _seed_channel(factory, "A"),
        "channel_b": _seed_channel(factory, "B"),
    }


def _comment(env, cid="UgxENG001", text="Great video, loved it!", **over):
    base = {
        "id": cid,
        "video_id": VIDEO_A,
        "author_display_name": "Viewer One",
        "author_channel_id": "UCviewer1",
        "text": text,
        "like_count": 5,
        "reply_count": 0,
        "published_at": "2026-10-20T10:00:00Z",
    }
    base.update(over)
    return env["store"].upsert_comment(env["channel_a"], base)


class FakeLLM:
    """Stand-in for the LLM router: records requests, returns canned
    text (or raises), and proves the approval path never calls it."""

    def __init__(self, text="Thanks a lot for watching!", exc=None):
        self.text = text
        self.exc = exc
        self.requests = []

    async def generate(self, req):
        self.requests.append(req)
        if self.exc is not None:
            raise self.exc
        return LLMResult(
            text=self.text, provider="fake", model="fake-model-1",
            tier=Tier.T1,
        )


@pytest.fixture()
def http_guard(monkeypatch):
    """Fail loudly on ANY httpx request; records attempts."""
    calls = []

    async def _blocked(self, *args, **kwargs):  # pragma: no cover
        calls.append((args, kwargs))
        raise AssertionError("external HTTP attempted from engagement code")

    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked)
    monkeypatch.setattr(httpx.Client, "send", _blocked)
    return calls


# ---------------------------------------------------------------------------
# Language + deterministic generation
# ---------------------------------------------------------------------------


def test_detect_language_modes():
    assert detect_language("بہت اچھی ویڈیو ہے") == ("ur", "arabic")
    assert detect_language("बहुत अच्छी वीडियो") == ("hi", "devanagari")
    assert detect_language("Bohat achi video hai, maza agaya") == ("ur", "roman")
    assert detect_language("Great video, loved it!") == ("en", "latin")


@pytest.mark.asyncio
async def test_generate_deterministic_when_no_llm(env):
    _comment(env)
    svc = CommentEngagementService(env["store"], llm=None)
    draft = await svc.generate_draft("UgxENG001", channel_id=env["channel_a"])
    assert draft["status"] == "proposed"
    assert draft["comment_id"] == "UgxENG001"
    assert draft["channel_id"] == env["channel_a"]
    assert draft["text"] and draft["text"] == draft["draft_text"]
    prov = draft["provenance"]
    assert prov["engine"] == "deterministic"
    assert prov["fallback_reason"] == "llm_not_configured"
    assert prov["model"] is None
    assert prov["language"] == "en"
    assert prov["tone"] == "helpful_simple_v1"
    # Not-wired posting boundary, on the very first view.
    assert draft["posted"] is False
    assert draft["posting"] == "not_wired"
    # Persisted, not just returned.
    stored = env["store"].get_draft(draft["id"], channel_id=env["channel_a"])
    assert stored is not None and stored["status"] == "proposed"


@pytest.mark.asyncio
async def test_generate_urdu_script_comment_gets_urdu_draft(env):
    _comment(env, text="یہ ویڈیو کیسے بنائی؟ بہت اچھی ہے")
    svc = CommentEngagementService(env["store"], llm=None)
    draft = await svc.generate_draft("UgxENG001")
    assert draft["provenance"]["language"] == "ur"
    assert draft["provenance"]["script"] == "arabic"
    assert any("؀" <= ch <= "ۿ" for ch in draft["text"])


@pytest.mark.asyncio
async def test_generate_roman_urdu_comment_gets_roman_draft(env):
    _comment(env, text="Bohat achi video hai, aap kese banate hain?")
    svc = CommentEngagementService(env["store"], llm=None)
    draft = await svc.generate_draft("UgxENG001")
    assert draft["provenance"]["language"] == "ur"
    assert draft["provenance"]["script"] == "roman"
    assert "shukriya" in draft["text"].lower()


@pytest.mark.asyncio
async def test_generate_unknown_comment_raises(env):
    svc = CommentEngagementService(env["store"], llm=None)
    with pytest.raises(CommentsNotFoundError):
        await svc.generate_draft("UgxMISSING")


# ---------------------------------------------------------------------------
# LLM mode + labelled fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_llm_mode_records_provenance(env):
    _comment(env, text="How did you record this audio?")
    llm = FakeLLM(text="Thanks for asking — we record with a USB mic!")
    svc = CommentEngagementService(env["store"], llm=llm)
    draft = await svc.generate_draft("UgxENG001")
    assert draft["text"] == "Thanks for asking — we record with a USB mic!"
    prov = draft["provenance"]
    assert prov["engine"] == "llm"
    assert prov["model"] == "fake-model-1"
    assert prov["fallback_reason"] is None
    # The router was addressed by TIER only, as Agent 28.
    (req,) = llm.requests
    assert req.tier == Tier.T1
    assert req.agent_id == "28"
    assert req.task == "comments.reply_draft"
    assert req.model_id is None


@pytest.mark.asyncio
async def test_generate_llm_failure_falls_back_labelled(env):
    _comment(env)
    llm = FakeLLM(exc=RuntimeError("provider down"))
    svc = CommentEngagementService(env["store"], llm=llm)
    draft = await svc.generate_draft("UgxENG001")
    prov = draft["provenance"]
    assert prov["engine"] == "deterministic"
    assert "RuntimeError" in prov["fallback_reason"]
    assert "provider down" in prov["fallback_reason"]
    assert draft["text"]  # a usable template draft, honestly labelled


@pytest.mark.asyncio
async def test_generate_llm_unusable_output_falls_back_labelled(env):
    _comment(env)
    llm = FakeLLM(text="Check https://spam.example/win now!!!")
    svc = CommentEngagementService(env["store"], llm=llm)
    draft = await svc.generate_draft("UgxENG001")
    assert draft["provenance"]["engine"] == "deterministic"
    assert draft["provenance"]["fallback_reason"] == "llm_output_unusable"
    assert "http" not in draft["text"]


@pytest.mark.asyncio
async def test_generate_strict_mode_raises_on_llm_failure(env):
    _comment(env)
    llm = FakeLLM(exc=RuntimeError("provider down"))
    svc = CommentEngagementService(env["store"], llm=llm, strict=True)
    with pytest.raises(DraftGenerationError):
        await svc.generate_draft("UgxENG001")
    assert env["store"].list_drafts(env["channel_a"]) == []


# ---------------------------------------------------------------------------
# Approval state machine — LOCAL ONLY, never posts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_lifecycle_records_decision(env):
    _comment(env)
    svc = CommentEngagementService(env["store"], llm=None)
    draft = await svc.generate_draft("UgxENG001")
    decided = svc.approve_draft(draft["id"], decided_by="owner")
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "owner"
    assert decided["decided_at"] is not None
    # Approval is a local mark only — the boundary fields prove it.
    assert decided["posted"] is False
    assert decided["posting"] == "not_wired"


@pytest.mark.asyncio
async def test_reject_lifecycle(env):
    _comment(env)
    svc = CommentEngagementService(env["store"], llm=None)
    draft = await svc.generate_draft("UgxENG001")
    decided = svc.reject_draft(draft["id"])
    assert decided["status"] == "rejected"
    assert decided["decided_at"] is not None
    assert decided["posted"] is False


@pytest.mark.asyncio
async def test_terminal_draft_cannot_be_redecided(env):
    _comment(env)
    svc = CommentEngagementService(env["store"], llm=None)
    draft = await svc.generate_draft("UgxENG001")
    svc.approve_draft(draft["id"])
    with pytest.raises(IllegalTransitionError):
        svc.approve_draft(draft["id"])
    with pytest.raises(IllegalTransitionError):
        svc.reject_draft(draft["id"])

    draft2 = await svc.generate_draft("UgxENG001")
    svc.reject_draft(draft2["id"])
    with pytest.raises(IllegalTransitionError):
        svc.approve_draft(draft2["id"])


@pytest.mark.asyncio
async def test_decide_unknown_draft_and_bad_decision(env):
    svc = CommentEngagementService(env["store"], llm=None)
    with pytest.raises(CommentsNotFoundError):
        svc.approve_draft("no-such-draft")
    _comment(env)
    draft = await svc.generate_draft("UgxENG001")
    with pytest.raises(ValueError):
        svc.decide_draft(draft["id"], "maybe")


@pytest.mark.asyncio
async def test_approval_performs_no_external_call(env, http_guard):
    """THE no-post guarantee: with the provider layer rigged to raise
    and httpx rigged to raise, generate + approve must still succeed
    purely locally — zero HTTP, zero further LLM calls at decision
    time."""
    _comment(env)

    class ExplodingLLM:
        async def generate(self, req):  # pragma: no cover
            raise AssertionError("LLM must not be called by approval")

    svc = CommentEngagementService(env["store"], llm=None)
    draft = await svc.generate_draft("UgxENG001")
    svc_llm = CommentEngagementService(env["store"], llm=ExplodingLLM())
    decided = svc_llm.approve_draft(draft["id"], decided_by="owner")
    assert decided["status"] == "approved"
    assert decided["posted"] is False
    assert http_guard == []


def test_engagement_source_has_no_write_endpoint_strings():
    """Grep-audit seam for the verification worker: the engagement
    service + router sources must contain ZERO YouTube write
    endpoint strings (they are assembled here so this test file
    itself stays clean of them too)."""
    backend = Path(__file__).resolve().parent.parent
    forbidden = [
        "comments" + ".insert",
        "comments" + ".update",
        "comments" + ".delete",
        "comments" + ".set" + "Moderation" + "Status",
        "set" + "Moderation" + "Status",
    ]
    for rel in ("app/comments/engagement.py", "app/routers/comments.py"):
        source = (backend / rel).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in source, f"{needle} found in {rel}"


# ---------------------------------------------------------------------------
# Owner-channel isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_channel_generate_and_decide_refused(env):
    _comment(env)
    svc = CommentEngagementService(env["store"], llm=None)
    with pytest.raises(CommentsScopeError):
        await svc.generate_draft("UgxENG001", channel_id=env["channel_b"])
    draft = await svc.generate_draft("UgxENG001", channel_id=env["channel_a"])
    with pytest.raises(CommentsScopeError):
        svc.approve_draft(draft["id"], channel_id=env["channel_b"])
    # Channel B sees no drafts at all.
    assert svc.list_drafts(env["channel_b"]) == []
    with pytest.raises(CommentsNotFoundError):
        svc.get_draft(draft["id"], channel_id=env["channel_b"])


# ---------------------------------------------------------------------------
# Moderation queue — labels + LOCAL decisions only
# ---------------------------------------------------------------------------


def _flag(env, kind="spam", suggested=None, cid="UgxENG001"):
    return env["store"].create_moderation_flag(
        cid, kind,
        channel_id=env["channel_a"],
        reason="heuristic: repeated link-like text",
        suggested_action=suggested,
        provenance={"engine": "deterministic", "spam_method": "heuristics_v1"},
    )


def test_moderation_queue_lists_pending_with_labels_and_context(env):
    _comment(env, text="WIN MONEY FAST click now")
    _flag(env)
    svc = CommentEngagementService(env["store"], llm=None)
    queue = svc.list_moderation_queue(env["channel_a"])
    assert len(queue) == 1
    item = queue[0]
    assert item["status"] == "pending"
    assert item["flag_kind"] == "spam"
    assert item["suggested_action_label"] == "review_for_spam"
    assert item["action_is_label_only"] is True
    assert item["action_executed"] is False
    assert item["youtube_action"] is None
    assert item["comment_text"] == "WIN MONEY FAST click now"
    assert item["comment_author"] == "Viewer One"
    # Other channel's queue is empty.
    assert svc.list_moderation_queue(env["channel_b"]) == []


def test_moderation_default_labels_by_kind(env):
    _comment(env)
    _flag(env, kind="abuse")
    svc = CommentEngagementService(env["store"], llm=None)
    (item,) = svc.list_moderation_queue(env["channel_a"])
    assert item["suggested_action_label"] == "hide_recommended"


def test_moderation_dismiss_records_local_decision(env, http_guard):
    _comment(env)
    flag = _flag(env)
    svc = CommentEngagementService(env["store"], llm=None)
    decided = svc.decide_moderation_flag(
        flag["id"], "dismiss", decided_by="owner"
    )
    assert decided["status"] == "dismissed"
    assert decided["decision"] == "dismiss"
    assert decided["decided_by"] == "owner"
    assert decided["decided_at"] is not None
    assert decided["action_executed"] is False
    assert http_guard == []
    # No longer in the pending queue; still listed when asked for all.
    assert svc.list_moderation_queue(env["channel_a"]) == []
    assert len(svc.list_moderation_queue(env["channel_a"], status=None)) == 1


def test_moderation_mark_reviewed_records_local_decision(env):
    _comment(env)
    flag = _flag(env, kind="abuse")
    svc = CommentEngagementService(env["store"], llm=None)
    decided = svc.decide_moderation_flag(flag["id"], "mark_reviewed")
    # 'actioned' in the store records the HUMAN's review only.
    assert decided["status"] == "actioned"
    assert decided["decision"] == "mark_reviewed"
    assert decided["action_executed"] is False
    assert decided["youtube_action"] is None


def test_moderation_terminal_flag_cannot_be_redecided(env):
    _comment(env)
    flag = _flag(env)
    svc = CommentEngagementService(env["store"], llm=None)
    svc.decide_moderation_flag(flag["id"], "dismiss")
    with pytest.raises(IllegalTransitionError):
        svc.decide_moderation_flag(flag["id"], "mark_reviewed")
    with pytest.raises(ValueError):
        svc.decide_moderation_flag(flag["id"], "delete_comment")
    with pytest.raises(CommentsNotFoundError):
        svc.decide_moderation_flag("no-such-flag", "dismiss")
