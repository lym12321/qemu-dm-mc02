"""Uncalibrated rigid rotor, sharing the existing NullEngine integration.

This half-implicit Euler approximation is useful for driver wiring and sign
tests. It does not model electrical dynamics, gearbox losses or saturation.
"""

import math


def integrate_rotor(position: float, velocity: float, effort: float, dt: float,
                    inertia: float, damping: float) -> tuple[float, float]:
    """Advance velocity then position; callers own configuration admission."""
    acceleration = (effort - damping * velocity) / inertia
    velocity += acceleration * dt
    position += velocity * dt
    return position, velocity


class Rotor:
    """One rotor in radians/radians per second, driven by torque in Nm."""

    def __init__(self, *, inertia: float = 0.02, damping: float = 0.08,
                 initial_position: float = 0.0, initial_velocity: float = 0.0):
        for name, value in (("inertia", inertia), ("damping", damping),
                            ("initial_position", initial_position),
                            ("initial_velocity", initial_velocity)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if inertia <= 0 or damping < 0:
            raise ValueError("inertia must be positive and damping nonnegative")
        self.inertia = inertia
        self.damping = damping
        self.initial_position = initial_position
        self.initial_velocity = initial_velocity
        self.reset()

    def reset(self) -> None:
        self.position = self.initial_position
        self.velocity = self.initial_velocity
        self.effort = 0.0

    def step(self, dt: float, effort: float) -> None:
        if (isinstance(dt, bool) or not isinstance(dt, (int, float))
                or not math.isfinite(dt) or dt < 0):
            raise ValueError("rotor dt must be finite and nonnegative")
        if (isinstance(effort, bool) or not isinstance(effort, (int, float))
                or not math.isfinite(effort)):
            raise ValueError("rotor effort must be finite")
        self.position, self.velocity = integrate_rotor(
            self.position, self.velocity, effort, dt, self.inertia, self.damping)
        self.effort = effort
