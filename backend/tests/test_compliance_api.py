"""Compliance Gate API tests (Round 6).

The Settings surface: ``GET /api/v1/settings/compliance`` returns the
checklist with per-item status + who/when + the overall effect, and
``POST /api/v1/settings/compliance/transitions`` is the ONLY write
path — a deliberate, logged, evidenced action.  There is no
free-form edit (PUT/PATCH answer 405) and no unevidenced pass.
"""

from __future__ import annotations

BASE = "/api/v1/settings/compliance"
ACTOR = "api-test-officer"


def test_compliance_api_full_flow(client):
    # Fresh state: honest not_started, public publishing blocked.
    resp = client.get(BASE)
    assert resp.status_code == 200
    state = resp.json()
    keys = [item["key"] for item in state["items"]]
    assert keys == [
        "api_audit_status",
        "terms_branding_review",
        "privacy_policy_live",
        "quota_project_verified",
    ]
    assert state["overall"] in ("not_started", "in_progress")
    assert state["public_publishing_allowed"] is False
    assert "forced to Private" in state["effect"]

    # A pass without evidence is refused, typed.
    resp = client.post(
        f"{BASE}/transitions",
        json={
            "item_key": "terms_branding_review",
            "to_status": "passed",
            "actor": ACTOR,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "compliance_evidence_required"

    # A transition without an actor is refused, typed.
    resp = client.post(
        f"{BASE}/transitions",
        json={
            "item_key": "terms_branding_review",
            "to_status": "in_progress",
            "actor": "  ",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "compliance_actor_required"

    # Unknown item -> 404 typed.
    resp = client.post(
        f"{BASE}/transitions",
        json={
            "item_key": "not_a_real_item",
            "to_status": "passed",
            "actor": ACTOR,
            "evidence": "x",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "compliance_item_not_found"

    # An evidenced pass lands, with who/when visible in the state.
    resp = client.post(
        f"{BASE}/transitions",
        json={
            "item_key": "terms_branding_review",
            "to_status": "passed",
            "actor": ACTOR,
            "evidence": "ToS + branding review signed off 2026-10-23",
            "note": "reviewed against the current API terms",
        },
    )
    assert resp.status_code == 200
    state = resp.json()
    item = next(
        i for i in state["items"] if i["key"] == "terms_branding_review"
    )
    assert item["status"] == "passed"
    assert item["updated_by"] == ACTOR
    assert item["updated_at"] > 0
    assert item["evidence"].startswith("ToS + branding review")
    history = [
        h for h in state["history"] if h["item_key"] == "terms_branding_review"
    ]
    assert history and history[0]["actor"] == ACTOR
    assert history[0]["to_status"] == "passed"
    assert history[0]["created_at"] > 0
    # One item passed is not an overall pass.
    assert state["overall"] == "in_progress"
    assert state["public_publishing_allowed"] is False


def test_compliance_has_no_freeform_edit_endpoint(client):
    # No PUT/PATCH state flip exists — transitions are the only write.
    for method in ("put", "patch"):
        resp = getattr(client, method)(
            BASE, json={"overall": "passed", "items": []}
        )
        assert resp.status_code == 405, method
    # And the state was not flipped by the attempt.
    state = client.get(BASE).json()
    assert state["overall"] != "passed" or all(
        item["status"] == "passed" for item in state["items"]
    )
