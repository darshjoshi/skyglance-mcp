"""Which of the 60 aircraft overhead is worth mentioning.

Ported from app/Sources/OverheadKit/Interest.swift, thresholds unchanged.

Scores are deliberately harsh. At a location with 85 large aircraft below 10,000 ft and
a dozen helicopters up at any moment, a generous scorer surfaces something every ninety
seconds, which is indistinguishable from noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Types worth walking outside for. Deliberately short — the point is rarity.
RARE_TYPE_CODES = frozenset({
    "A124", "A225",                            # Antonov
    "A388",                                    # A380
    "B748", "B741", "B742", "B743", "B744",    # 747
    "B52", "B1", "B2",                         # heavy bombers
    "C5M", "C17",                              # outsize military transport
    "CONC", "SR71", "U2",
    "SPIT", "LANC", "B17", "P51", "DC3", "DC6", "CVLT",   # warbirds and classics
})

#: ADS-B emitter category is far more reliable for classifying an aircraft than
#: pattern-matching type codes. A3/A4/A5 are airliner-sized or bigger; A7 is rotorcraft.
LARGE_CATEGORIES = frozenset({"A3", "A4", "A5"})
ROTORCRAFT_CATEGORY = "A7"

#: Priority breaks ties when two categories score identically.
_PRIORITY = {"emergency": 4, "rare": 3, "military": 2, "big_and_low": 1, "rotorcraft": 0}


@dataclass(frozen=True)
class Interest:
    category: str
    score: float
    reason: str


def score(
    *,
    type_code: Optional[str],
    category: Optional[str],
    altitude_ft: Optional[float],
    slant_km: float,
    is_military: bool,
    has_emergency: bool,
) -> Optional[Interest]:
    """The single most interesting thing about this aircraft, or None if it's routine."""
    candidates: list[Interest] = []
    alt = altitude_ft if altitude_ft is not None else 0.0

    if has_emergency:
        candidates.append(Interest("emergency", 100.0, "squawking emergency"))

    if type_code and type_code.upper() in RARE_TYPE_CODES:
        # Rare enough that distance barely matters; you'd walk outside for it.
        proximity = max(0.0, 1 - slant_km / 40)
        candidates.append(
            Interest("rare", 75 + 20 * proximity, f"rare type {type_code.upper()}"))

    if is_military:
        proximity = max(0.0, 1 - slant_km / 40)
        candidates.append(Interest("military", 70 + 20 * proximity, "military"))

    # Big and low: an airliner on approach is enormous and unmistakable even at a
    # shallow angle, which is exactly what a pure elevation rule misses.
    if category in LARGE_CATEGORIES and alt <= 4000 and slant_km <= 8:
        lowness = 1 - min(1.0, alt / 4000)
        closeness = 1 - min(1.0, slant_km / 8)
        candidates.append(Interest("big_and_low",
                                   45 + 50 * (0.5 * lowness + 0.5 * closeness),
                                   "large aircraft low overhead"))

    # Rotorcraft are constant near any city with a heliport, so only the genuinely
    # close and low ones clear the bar.
    if category == ROTORCRAFT_CATEGORY and alt <= 1500 and slant_km <= 5:
        lowness = 1 - min(1.0, alt / 1500)
        closeness = 1 - min(1.0, slant_km / 5)
        candidates.append(Interest("rotorcraft",
                                   40 + 45 * (0.4 * lowness + 0.6 * closeness),
                                   "helicopter close and low"))

    if not candidates:
        return None
    return max(candidates, key=lambda i: (i.score, _PRIORITY[i.category]))
