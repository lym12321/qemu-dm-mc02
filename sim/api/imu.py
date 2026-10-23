"""Backend independent, site-local SI IMU observations.

No frame rotation, gravity correction, sensor quantization or sample identity
belongs here. A signal timestamp describes a completed observation, not a
firmware read or a sensor's electrical sampling instant.
"""

from dataclasses import dataclass
import math

from .events import check_timestamp


@dataclass(frozen=True)
class ImuSample:
    gyro_rad_s: tuple[float, float, float]
    accel_m_s2: tuple[float, float, float]


def validate_imu_sample(sample: ImuSample) -> None:
    """Admit finite SI vectors at a consumer boundary, without transforming them."""
    if not isinstance(sample, ImuSample):
        raise TypeError("IMU source requires an ImuSample")
    for name, vector in (("gyro_rad_s", sample.gyro_rad_s),
                         ("accel_m_s2", sample.accel_m_s2)):
        if not isinstance(vector, tuple) or len(vector) != 3:
            raise ValueError(f"{name} must be a tuple of three finite numbers")
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must contain finite numbers")
            try:
                finite = math.isfinite(value)
            except OverflowError as exc:
                raise ValueError(f"{name} must contain finite numbers") from exc
            if not finite:
                raise ValueError(f"{name} must contain finite numbers")


@dataclass(frozen=True)
class ImuSignalSample:
    sample: ImuSample
    source_time_ns: int | None
    mapped_time_ns: int

    def __post_init__(self):
        validate_imu_sample(self.sample)
        if self.source_time_ns is not None:
            check_timestamp(self.source_time_ns)
        check_timestamp(self.mapped_time_ns)
