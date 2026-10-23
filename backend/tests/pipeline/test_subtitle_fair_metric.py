"""Transliteration-aware ("fair") script-match metric (Round 4).

The raw metric (:func:`script_match_ratio`) compares text in its
original script, so a Roman Urdu reference against Urdu-script Whisper
captions scores ~0.0 purely as a script artefact.  The fair metric
normalises BOTH sides into a common Roman representation first, then
applies the same token-sequence scoring.  These tests pin down that:

* identical Roman text scores the same under raw and fair;
* a faithful Urdu-script rendering of a real Roman Urdu script line
  scores LOW under raw and HIGH under fair;
* unrelated text scores low under BOTH metrics (no inflation);
* empty/edge inputs behave exactly like the raw metric's edges.
"""

from __future__ import annotations

import pytest

from app.pipeline.subtitles.validation import (
    normalise_for_match,
    script_match_ratio,
    script_match_ratio_fair,
    transliterate_urdu_to_roman,
)

# A real line from the Round 3 Phase 0 facts pack
# (outputs/round3/phase0/facts_ocean/samundar-ki-gehrai-ke-raaz-r3/bundle.json,
# hook / chapter 1, narration_text) and its faithful Urdu-script
# rendering — the form Whisper captions for this narration take.
ROMAN_LINE = (
    "Kya aap jante hain ke hamari zameen ka sab se gehra raaz "
    "pani ki gehrayion mein chupa hai?"
)
URDU_LINE = (
    "کیا آپ جانتے ہیں کہ ہماری زمین کا سب سے گہرا راز "
    "پانی کی گہرائیوں میں چھپا ہے؟"
)


def test_identical_roman_text_scores_same_under_raw_and_fair():
    assert script_match_ratio(ROMAN_LINE, ROMAN_LINE) == pytest.approx(1.0)
    assert script_match_ratio_fair(ROMAN_LINE, ROMAN_LINE) == pytest.approx(1.0)
    # Case / punctuation differences are ignored by both metrics.
    variant = ROMAN_LINE.upper().replace("?", "!")
    assert script_match_ratio(ROMAN_LINE, variant) == pytest.approx(1.0)
    assert script_match_ratio_fair(ROMAN_LINE, variant) == pytest.approx(1.0)


def test_similar_roman_pair_scores_close_under_both_metrics():
    other = ROMAN_LINE.replace("gehra raaz", "gehra secret")
    raw = script_match_ratio(ROMAN_LINE, other)
    fair = script_match_ratio_fair(ROMAN_LINE, other)
    assert raw == pytest.approx(fair, abs=0.05)
    assert 0.8 < raw < 1.0


def test_urdu_script_rendering_low_raw_high_fair():
    raw = script_match_ratio(ROMAN_LINE, URDU_LINE)
    fair = script_match_ratio_fair(ROMAN_LINE, URDU_LINE)
    # The raw metric sees two different scripts and matching nothing.
    assert raw < 0.1
    # After transliteration-aware normalisation the same words agree.
    assert fair > 0.55
    assert fair > raw + 0.4


def test_unrelated_texts_score_low_under_both_metrics():
    unrelated_roman = (
        "The chef chopped fresh onions and fried them in olive oil "
        "for dinner tonight with rice"
    )
    assert script_match_ratio(ROMAN_LINE, unrelated_roman) < 0.3
    assert script_match_ratio_fair(ROMAN_LINE, unrelated_roman) < 0.3

    unrelated_urdu = (
        "آج ہم چکن بریانی بنائیں گے اور ساتھ رائتہ بھی تیار کریں گے"
    )
    assert script_match_ratio(ROMAN_LINE, unrelated_urdu) < 0.3
    assert script_match_ratio_fair(ROMAN_LINE, unrelated_urdu) < 0.3


def test_empty_edges_behave_like_raw_metric():
    for reference, candidate in [("", ""), ("kuch text", ""), ("", "kuch text")]:
        assert script_match_ratio_fair(reference, candidate) == (
            script_match_ratio(reference, candidate)
        )
    assert script_match_ratio_fair("", "") == 1.0
    assert script_match_ratio_fair("abc", "") == 0.0
    assert script_match_ratio_fair("", "abc") == 0.0


def test_transliterate_retroflexes_and_digraphs():
    # ٹ / ڈ / ڑ collapse to their plain Roman Urdu counterparts.
    assert transliterate_urdu_to_roman("ٹ").startswith("t")
    assert transliterate_urdu_to_roman("ڈ").startswith("d")
    assert transliterate_urdu_to_roman("ڑ").startswith("r")
    # ھ aspirates the preceding consonant: بھائی -> bhaa…
    assert transliterate_urdu_to_roman("بھائی").startswith("bh")
    # Bari ye ے is the e/ai sound: کے -> ke.
    assert transliterate_urdu_to_roman("کے") == "ke"


def test_transliterate_digits_diacritics_and_passthrough():
    # Urdu (extended Arabic-Indic) and Arabic-Indic digits.
    assert transliterate_urdu_to_roman("۱۲۳") == "123"
    assert transliterate_urdu_to_roman("٤٥٦") == "456"
    # Harakat / shadda are stripped: بَ -> b, مُحَمَّد -> mhmd skeleton.
    assert transliterate_urdu_to_roman("بَ") == "b"
    assert transliterate_urdu_to_roman("مُحَمَّد") == "mhmd"
    # Latin text passes through unchanged.
    assert transliterate_urdu_to_roman("Hello World 123") == "Hello World 123"
    assert transliterate_urdu_to_roman("") == ""


def test_normalise_for_match_common_representation():
    assert normalise_for_match("کیا") == "kya"
    assert normalise_for_match("KYA!") == "kya"
    assert normalise_for_match("میں") == normalise_for_match("mein") == "men"
    assert normalise_for_match("ہے") == normalise_for_match("hai") == "he"
    assert normalise_for_match("") == ""
