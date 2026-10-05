"""Completed joint observations shared by native physics backends."""

from dataclasses import dataclass


@dataclass(frozen=True)
class JointState:
    position_rad: float
    velocity_rad_s: float
