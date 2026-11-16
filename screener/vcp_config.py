"""Configuration for independent Minervini-inspired VCP B-mode screening.

These are APEX operational thresholds, not official Minervini rules.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class VCPConfig:
    min_rs: float = 80.0
    min_base_days: int = 15
    max_base_days: int = 100
    pivot_wing: int = 3
    min_pivot_spacing: int = 3
    min_pullback_pct: float = 3.0
    min_contractions: int = 2
    contraction_ratio_max: float = 0.85
    confirmed_last_depth_max: float = 8.0
    watch_last_depth_max: float = 12.0
    confirmed_pivot_distance_max: float = 5.0
    watch_pivot_distance_max: float = 10.0
    vdu_ratio_max: float = 0.60
    breakout_volume_ratio_min: float = 1.40
    min_avg_volume_63d: float = 500_000.0


BALANCED = VCPConfig()
