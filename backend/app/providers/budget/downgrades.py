"""The downgrade ladder — what the 90% SOFT STOP engages, in exact order.

Every step is recorded in the project log and the user's report; degraded
output is NEVER silent (roadmap Part 10.4.4 / 10.5).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Downgrade:
    id: str
    order: int
    title: str
    description: str


# Order is contractual: ① -> ⑤, never reordered.
DOWNGRADE_LADDER: tuple[Downgrade, ...] = (
    Downgrade(
        id="ai_scenes_to_stock",
        order=1,
        title="AI scenes → stock-only",
        description="No further AI image generation; remaining scenes use stock only.",
    ),
    Downgrade(
        id="premium_tts_to_edge",
        order=2,
        title="Premium TTS → edge-tts",
        description="Remaining scenes narrate with edge-tts instead of the premium voice.",
    ),
    Downgrade(
        id="t3_script_to_t2",
        order=3,
        title="T3 script pass → T2",
        description="The final script pass drops from the flagship tier (T3) to T2.",
    ),
    Downgrade(
        id="thumbnail_variants_3_to_1",
        order=4,
        title="Thumbnail variants 3 → 1",
        description="Only one thumbnail variant is produced instead of three.",
    ),
    Downgrade(
        id="render_1440p_to_1080p",
        order=5,
        title="1440p render → 1080p",
        description="Render resolution drops to 1080p (saves time as well as cost).",
    ),
)


class DowngradePolicy:
    """Ordered policy object the pipeline queries via the Guard."""

    ladder: tuple[Downgrade, ...] = DOWNGRADE_LADDER

    @staticmethod
    def ordered_ids() -> list[str]:
        return [d.id for d in DOWNGRADE_LADDER]
