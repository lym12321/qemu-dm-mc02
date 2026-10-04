"""Pure native IMU SI to the existing BMI088 co-simulation input units.

The source is a completed site-local proper-acceleration/gyro observation. It
does not step physics, add bias, quantize registers or assign sample identities.
The existing BMI088/link producer owns range/filter/ODR and source attribution.
"""

import math

from sim.physics import ImuSample


def imu_to_bmi088_units(sample: ImuSample) -> tuple[
        tuple[float, float, float], tuple[float, float, float]]:
    """Return (gyro degrees/s, accel g), where one g is 9.80665 m/s²."""
    if not isinstance(sample, ImuSample):
        raise TypeError("BMI088 source requires an ImuSample")
    for name, vector in (("gyro_rad_s", sample.gyro_rad_s),
                         ("accel_m_s2", sample.accel_m_s2)):
        if not isinstance(vector, tuple) or len(vector) != 3:
            raise ValueError(f"{name} must be a tuple of three finite numbers")
        for value in vector:
            if (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"{name} must contain finite numbers")
            try:
                finite = math.isfinite(value)
            except OverflowError as exc:
                raise ValueError(f"{name} must contain finite numbers") from exc
            if not finite:
                raise ValueError(f"{name} must contain finite numbers")
    # Keep the legacy worker's arithmetic and axis order. No orientation or
    # gravity subtraction belongs here: native site sensors already supply it.
    gyro = tuple(value * 180.0 / math.pi for value in sample.gyro_rad_s)
    accel = tuple(value / 9.80665 for value in sample.accel_m_s2)
    if not all(math.isfinite(value) for value in (*gyro, *accel)):
        raise ValueError("converted BMI088 units must be finite")
    return gyro, accel
