"""Labelled synthetic comment fixtures for Comment Intelligence tests.

Each comment's expected band is in its id prefix:
``fan_*`` positive, ``q_*`` question (English + Roman Urdu),
``cmp_*`` complaint (negative), ``spam_*`` spam/abuse.
"""

from __future__ import annotations

VIDEO_ID = "vid-facts-ocean"
CHANNEL_ID = "chan-facts"


def _c(cid, text, likes=0, replies=0, video_id=VIDEO_ID):
    return {
        "id": cid,
        "video_id": video_id,
        "channel_id": CHANNEL_ID,
        "author_display_name": f"viewer-{cid}",
        "text": text,
        "like_count": likes,
        "published_at": "2026-10-20T10:00:00Z",
        "reply_count": replies,
    }


FANS = [
    _c("fan_1", "Amazing video! The ocean story was beautiful and the voice narration is superb", likes=50, replies=4),
    _c("fan_2", "Great work, loved the deep sea facts, very informative and interesting", likes=30),
    _c("fan_3", "Zabardast video, kahani bohat achi thi, shukriya itni achi maloomat ke liye", likes=25),
]

QUESTIONS = [
    _c("q_1", "What is the deepest point of the ocean?", likes=120, replies=9),
    _c("q_2", "Kya aap next video mein Mariana Trench par banayenge?", likes=95, replies=3),
    _c("q_3", "How do giant squid survive at that depth?", likes=60),
    _c("q_4", "Agli video kab aayegi?", likes=40),
]

COMPLAINTS = [
    _c("cmp_1", "The audio was too slow and boring in the middle, very disappointing", likes=15),
    _c("cmp_2", "Voice bohat slow hai, video boring thi aur editing bakwas thi", likes=8),
]

SPAM = [
    _c("spam_1", "Subscribe to my channel and earn money online, visit www.spamxyz.com now", likes=0),
    _c("spam_2", "FREE FOLLOWERS FREE FOLLOWERS FREE FOLLOWERS check the link http://spam.example.com", likes=0),
]

MIXED_VIDEO = FANS + QUESTIONS + COMPLAINTS + SPAM

SECOND_VIDEO = [
    _c("fan_4", "Excellent history facts, wonderful narration, thanks for this", likes=44, video_id="vid-history"),
    _c("q_5", "Why did the city disappear so suddenly?", likes=77, replies=5, video_id="vid-history"),
    _c("cmp_3", "The background music was awful and the pacing was terrible", likes=5, video_id="vid-history"),
]
