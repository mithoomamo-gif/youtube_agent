"""SL-001 (HIGH) — REST style-profile creation enforces the no-copy
boundary (schema + ``assert_no_copy``) — regression tests.

Before the fix, ``POST /api/v1/channels/{id}/style-profiles`` persisted
arbitrary JSON into ``style_profiles.profile_json`` with NEITHER
validation against the strict ``style_profile.v1`` schema
(``app.integration.style_profile.StyleProfile``) NOR the synthesis
layer's ``assert_no_copy`` gate (verbatim + >= 4-word shingle checks).

These tests prove, through the public API only (plus a direct DB row
count for the nothing-persisted assertions):

1. a payload carrying a verbatim competitor title is rejected (422
   ``no_copy_violation``) and NOTHING is persisted;
2. a payload carrying a verbatim competitor description chunk is
   rejected the same way;
3. a payload that is not verbatim but shares a >= 4-word shingle with
   the competitor description is rejected (proves ``assert_no_copy``
   itself — not a weaker local check — is what runs);
4. schema-invalid payloads (extra field / missing required field /
   wrong-typed field / ``contains_competitor_assets: true``) are
   rejected (422 ``invalid_style_profile``), nothing persisted;
5. a clean patterns-only payload in the exact proof-profile shape
   (``outputs/stylelab/proof/style_profile.json``) is accepted and
   persisted, even though the source competitor HAS titles /
   descriptions on file (the gate has no false positive on patterns);
6. competitor_ids owned by a DIFFERENT channel are rejected even
   when the profile payload itself is clean;
7. a profile whose inner ``channel_id`` names another channel is
   rejected;
8. activation re-checks stored content: a legacy row poisoned
   directly in the DB (pre-gate style) cannot be activated.

Uses the conftest ``app`` / ``client`` fixtures like
``tests/test_style_lab_api.py``.  No network, no API keys.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SUFFIX = uuid.uuid4().hex[:10]
CHAN_A = f"slb-a-{_SUFFIX}"
CHAN_B = f"slb-b-{_SUFFIX}"
YT_A = f"UCslbA{_SUFFIX}xxxx"
YT_B = f"UCslbB{_SUFFIX}xxxx"

# Competitor source material stored on the competitor's video row.
TITLE = "Secrets of the Deep Ocean Revealed Today"
DESCRIPTION = (
    "Dive into the mysterious deep ocean world where strange glowing "
    "creatures live beneath the waves every single night"
)

_PROOF_PATH = (
    Path(__file__).resolve().parents[2]
    / "outputs" / "stylelab" / "proof" / "style_profile.json"
)


# ---------------------------------------------------------------------------
# Seeding / helpers
# ---------------------------------------------------------------------------


def _seed_channels() -> None:
    from app import db as db_module

    now = int(time.time() * 1000)
    conn = db_module.get_connection()
    try:
        for chan_id, yt_id, title in (
            (CHAN_A, YT_A, "Boundary Owner A"),
            (CHAN_B, YT_B, "Boundary Owner B"),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO channels (id, youtube_channel_id,"
                " title, language, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'en', 'active', ?, ?)",
                (chan_id, yt_id, title, now, now),
            )
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _channels(app):
    _seed_channels()
    yield


def _clean_profile(channel_id: str = CHAN_A) -> dict:
    """The proof profile shape (patterns only), scoped to a channel."""
    payload = json.loads(_PROOF_PATH.read_text(encoding="utf-8"))
    payload = copy.deepcopy(payload)
    payload["channel_id"] = channel_id
    return payload


def _world(client: TestClient, handle: str, channel_id: str = CHAN_A) -> dict:
    """A competitor under ``channel_id`` with one pasted-metadata video
    carrying TITLE / DESCRIPTION as its stored source material."""
    resp = client.post(
        f"/api/v1/channels/{channel_id}/competitors",
        json={"input": f"@{handle}"},
    )
    assert resp.status_code == 201, resp.text
    competitor = resp.json()
    video = client.post(
        f"/api/v1/channels/{channel_id}/competitors/{competitor['id']}/videos",
        json={
            "source": "pasted_metadata",
            "title": TITLE,
            "description": DESCRIPTION,
            "tags": ["ocean"],
        },
    )
    assert video.status_code == 201, video.text
    return competitor


def _profiles(client: TestClient, channel_id: str) -> list[dict]:
    resp = client.get(f"/api/v1/channels/{channel_id}/style-profiles")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _db_profile_count(channel_id: str) -> int:
    from app import db as db_module

    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM style_profiles WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _create(client: TestClient, channel_id: str, name: str,
            competitor_ids: list[str], profile):
    return client.post(
        f"/api/v1/channels/{channel_id}/style-profiles",
        json={"name": name, "competitor_ids": competitor_ids,
              "profile": profile},
    )


def _assert_rejected_nothing_persisted(client: TestClient, channel_id: str,
                                       name: str, before_count: int,
                                       resp) -> None:
    assert resp.status_code == 422, resp.text
    assert all(p["name"] != name for p in _profiles(client, channel_id))
    assert _db_profile_count(channel_id) == before_count


# ---------------------------------------------------------------------------
# 1 + 2. Verbatim title / description -> 422 no_copy_violation, no row
# ---------------------------------------------------------------------------


def test_verbatim_title_rejected_nothing_persisted(client: TestClient):
    competitor = _world(client, f"VerbTitle{_SUFFIX[:6]}")
    before = _db_profile_count(CHAN_A)

    poisoned = _clean_profile()
    # Smuggle the competitor's exact title into a free-string pattern
    # field — schema-valid, but a verbatim copy.
    poisoned["hook"]["types"] = [TITLE]
    resp = _create(client, CHAN_A, "Poisoned Verbatim Title",
                   [competitor["id"]], poisoned)

    _assert_rejected_nothing_persisted(
        client, CHAN_A, "Poisoned Verbatim Title", before, resp)
    assert resp.json()["error"]["code"] == "no_copy_violation"


def test_verbatim_description_rejected_nothing_persisted(
    client: TestClient,
):
    competitor = _world(client, f"VerbDesc{_SUFFIX[:6]}")
    before = _db_profile_count(CHAN_A)

    poisoned = _clean_profile()
    poisoned["hook"]["types"] = [DESCRIPTION]
    resp = _create(client, CHAN_A, "Poisoned Verbatim Desc",
                   [competitor["id"]], poisoned)

    _assert_rejected_nothing_persisted(
        client, CHAN_A, "Poisoned Verbatim Desc", before, resp)
    assert resp.json()["error"]["code"] == "no_copy_violation"


# ---------------------------------------------------------------------------
# 3. >= 4-word shingle (reworded, not verbatim) -> 422, no row
# ---------------------------------------------------------------------------


def test_shingle_match_rejected_nothing_persisted(client: TestClient):
    competitor = _world(client, f"Shingle{_SUFFIX[:6]}")
    before = _db_profile_count(CHAN_A)

    poisoned = _clean_profile()
    # NOT the title, NOT the description — but it contains the 5-word
    # shingle "mysterious deep ocean world where" from the stored
    # description, so only the >= 4-word shingle rule can catch it.
    poisoned["hook"]["types"] = [
        "mysterious deep ocean world where wonders await"
    ]
    resp = _create(client, CHAN_A, "Poisoned Shingle",
                   [competitor["id"]], poisoned)

    _assert_rejected_nothing_persisted(
        client, CHAN_A, "Poisoned Shingle", before, resp)
    assert resp.json()["error"]["code"] == "no_copy_violation"


# ---------------------------------------------------------------------------
# 4. Schema-invalid payloads -> 422 invalid_style_profile, no row
# ---------------------------------------------------------------------------


def _schema_invalid_payload(case: str) -> dict:
    payload = _clean_profile()
    if case == "extra_field":
        payload["bogus_field"] = "not in style_profile.v1"
    elif case == "missing_channel_id":
        del payload["channel_id"]
    elif case == "wrong_type":
        payload["video_count"] = "many"
    elif case == "competitor_assets_true":
        payload["no_copy_manifest"]["contains_competitor_assets"] = True
    elif case == "bad_schema_version":
        payload["schema_version"] = "style_profile.v999"
    else:  # pragma: no cover - guard against typos in the parametrize
        raise AssertionError(case)
    return payload


@pytest.mark.parametrize(
    "case",
    [
        "extra_field",
        "missing_channel_id",
        "wrong_type",
        "competitor_assets_true",
        "bad_schema_version",
    ],
)
def test_schema_invalid_rejected_nothing_persisted(
    client: TestClient, case: str
):
    competitor = _world(client, f"Schema{case[:4]}{_SUFFIX[:5]}")
    before = _db_profile_count(CHAN_A)
    name = f"Schema Invalid {case}"

    resp = _create(client, CHAN_A, name, [competitor["id"]],
                   _schema_invalid_payload(case))

    _assert_rejected_nothing_persisted(client, CHAN_A, name, before, resp)
    assert resp.json()["error"]["code"] == "invalid_style_profile"


# ---------------------------------------------------------------------------
# 5. Clean patterns-only payload (proof shape) -> accepted + persisted
# ---------------------------------------------------------------------------


def test_clean_proof_shaped_profile_accepted_and_persisted(
    client: TestClient,
):
    # The competitor HAS source titles/descriptions on file, so the
    # no-copy gate genuinely runs — a patterns-only profile must pass.
    competitor = _world(client, f"Clean{_SUFFIX[:6]}")
    before = _db_profile_count(CHAN_A)
    payload = _clean_profile()

    resp = _create(client, CHAN_A, "Clean Proof Style",
                   [competitor["id"]], payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["channel_id"] == CHAN_A
    assert body["competitor_ids"] == [competitor["id"]]
    assert body["profile"] == payload  # canonical v1 JSON round-trips
    assert _db_profile_count(CHAN_A) == before + 1
    assert body["id"] in [p["id"] for p in _profiles(client, CHAN_A)]


def test_payloadless_create_still_accepted(client: TestClient):
    """Control: creating a profile with NO profile content persists
    no content (profile_json NULL) and stays allowed — the gate is
    about content, and the UI creates profiles exactly this way."""
    competitor = _world(client, f"NoPayload{_SUFFIX[:6]}")
    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "No Content Yet", "competitor_ids": [competitor["id"]]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["profile"] is None


# ---------------------------------------------------------------------------
# 6 + 7. Cross-channel competitor_ids / inner channel mismatch
# ---------------------------------------------------------------------------


def test_cross_channel_competitor_rejected_even_with_clean_payload(
    client: TestClient,
):
    foreign = _world(client, f"Foreign{_SUFFIX[:6]}", channel_id=CHAN_B)
    before = _db_profile_count(CHAN_A)

    # Payload itself is clean and scoped to A — the competitor is not.
    resp = _create(client, CHAN_A, "Cross Channel Theft",
                   [foreign["id"]], _clean_profile(CHAN_A))

    _assert_rejected_nothing_persisted(
        client, CHAN_A, "Cross Channel Theft", before, resp)
    assert resp.json()["error"]["code"] == "competitor_not_in_channel"


def test_inner_channel_id_mismatch_rejected(client: TestClient):
    competitor = _world(client, f"Mismatch{_SUFFIX[:6]}")
    before = _db_profile_count(CHAN_A)

    # Posted under A, but the profile claims to belong to B.
    resp = _create(client, CHAN_A, "Wrong Inner Channel",
                   [competitor["id"]], _clean_profile(CHAN_B))

    _assert_rejected_nothing_persisted(
        client, CHAN_A, "Wrong Inner Channel", before, resp)
    error = resp.json()["error"]
    assert error["code"] == "invalid_style_profile"
    assert error["details"]["reason"] == "channel_mismatch"


# ---------------------------------------------------------------------------
# 8. Activation re-checks stored content (legacy poisoned row)
# ---------------------------------------------------------------------------


def test_activation_of_legacy_poisoned_profile_rejected(
    client: TestClient,
):
    from app import db as db_module

    competitor = _world(client, f"Legacy{_SUFFIX[:6]}")
    created = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles",
        json={"name": "Legacy Poisoned",
              "competitor_ids": [competitor["id"]]},
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    # Simulate a pre-gate row: poison profile_json directly in the DB
    # (schema-valid v1 shape, but carrying the verbatim title).
    poisoned = _clean_profile()
    poisoned["hook"]["types"] = [TITLE]
    conn = db_module.get_connection()
    try:
        conn.execute(
            "UPDATE style_profiles SET profile_json = ? WHERE id = ?",
            (json.dumps(poisoned), profile_id),
        )
    finally:
        conn.close()

    resp = client.post(
        f"/api/v1/channels/{CHAN_A}/style-profiles/{profile_id}/activate"
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "no_copy_violation"
    by_id = {p["id"]: p for p in _profiles(client, CHAN_A)}
    assert by_id[profile_id]["status"] == "draft"  # never went active
