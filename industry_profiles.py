"""Industry-specific sending windows and scheduling rules."""
from dataclasses import dataclass
from typing import List


@dataclass
class Profile:
    industry: str
    start_hour: int
    end_hour: int
    days: List[int]  # Monday=0 ... Sunday=6
    peak_hours: List[int]


PROFILES = {
    "construction": Profile(
        industry="construction",
        start_hour=9,
        end_hour=18,
        days=[0, 1, 2, 3, 4],
        peak_hours=[9, 10, 14, 15],
    ),
    "hotel": Profile(
        industry="hotel",
        start_hour=0,
        end_hour=24,
        days=[0, 1, 2, 3, 4, 5],
        peak_hours=[10, 11, 15, 16],
    ),
    "restaurant": Profile(
        industry="restaurant",
        start_hour=10,
        end_hour=22,
        days=[0, 1, 2, 3, 4],
        peak_hours=[14, 15, 16],
    ),
    "manufacturing": Profile(
        industry="manufacturing",
        start_hour=8,
        end_hour=17,
        days=[0, 1, 2, 3, 4, 5],
        peak_hours=[9, 10, 14],
    ),
    "other": Profile(
        industry="other",
        start_hour=9,
        end_hour=18,
        days=[0, 1, 2, 3, 4, 5],
        peak_hours=[10, 14],
    ),
}


def get_profile(industry: str) -> Profile:
    return PROFILES.get((industry or "").strip().lower(), PROFILES["other"])
