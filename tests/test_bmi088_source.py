"""Pure SI-to-BMI088 profile conversion, with literal unit/axis oracles."""

import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sim.devices.bmi088 import imu_to_bmi088_units
from sim.physics import ImuSample


def test_literal_units_keep_axis_sign_and_frozen_input_without_accumulation():
    sample = ImuSample((math.pi, -math.pi / 2, 0), (9.80665, 0, -19.6133))
    original = (sample.gyro_rad_s, sample.accel_m_s2)
    gyro, accel = imu_to_bmi088_units(sample)
    assert type(gyro) is tuple and type(accel) is tuple
    assert gyro == pytest.approx((180, -90, 0))
    assert accel == pytest.approx((1, 0, -2))
    # Another observation and reread cannot integrate, offset, or mutate SI data.
    assert imu_to_bmi088_units(ImuSample((0, 0, 0), (0, 0, 0))) == (
        (0, 0, 0), (0, 0, 0))
    assert imu_to_bmi088_units(sample) == (gyro, accel)
    assert sample.gyro_rad_s is original[0] and sample.accel_m_s2 is original[1]
    assert (sample.gyro_rad_s, sample.accel_m_s2) == original


def test_nonzero_integer_axes_are_admitted_as_si_numbers():
    gyro, accel = imu_to_bmi088_units(ImuSample((1, 0, -1), (0, -1, 0)))
    assert gyro == pytest.approx((57.29577951308232, 0, -57.29577951308232))
    assert accel == pytest.approx((0, -.10197162129779283, 0))


def test_structural_lookalike_is_rejected_without_reading_a_physics_owner():
    lookalike = SimpleNamespace(gyro_rad_s=(0, 0, 0), accel_m_s2=(0, 0, 0))
    with pytest.raises((TypeError, ValueError)):
        imu_to_bmi088_units(lookalike)


def test_each_vector_requires_a_tuple_of_three_axes():
    for sample in (ImuSample((0, 0), (0, 0, 0)),
                   ImuSample((0, 0, 0), [0, 0, 0])):
        with pytest.raises((TypeError, ValueError)):
            imu_to_bmi088_units(sample)


def test_axes_reject_bool_string_and_nonfinite_si_numbers():
    for sample in (ImuSample((True, 0, 0), (0, 0, 0)),
                   ImuSample((0, 0, 0), (0, "1", 0)),
                   ImuSample((math.nan, 0, 0), (0, 0, 0)),
                   ImuSample((0, 0, 0), (0, 0, math.inf))):
        with pytest.raises((TypeError, ValueError)):
            imu_to_bmi088_units(sample)


def test_finite_si_overflow_is_rejected_but_large_finite_output_is_preserved():
    sample = ImuSample((1e308, 0, 0), (0, 0, 0))
    with pytest.raises(ValueError):
        imu_to_bmi088_units(sample)
    assert sample.gyro_rad_s == (1e308, 0, 0)
    gyro, accel = imu_to_bmi088_units(ImuSample((0, 0, 0), (0, 0, 1e308)))
    assert gyro == (0, 0, 0)
    assert accel == pytest.approx((0, 0, 1.0197162129779283e307))
