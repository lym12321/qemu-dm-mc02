#![cfg(any(feature = "devices", feature = "machine", feature = "lab"))]
use sim_host::{devices::bmi088::imu_to_bmi088_units, signals::ImuSample};

#[test]
fn sensor_local_si_preserves_axes_and_proper_acceleration_in_wire_units() {
    let sample = ImuSample {
        gyro_rad_s: [
            std::f64::consts::PI,
            -std::f64::consts::FRAC_PI_2,
            std::f64::consts::FRAC_PI_4,
        ],
        accel_m_s2: [9.80665, -19.6133, 0.],
    };
    assert_eq!(
        imu_to_bmi088_units(&sample).unwrap(),
        ([180., -90., 45.], [1., -2., 0.])
    );
    for value in [f64::NAN, f64::INFINITY, f64::MAX] {
        assert!(
            imu_to_bmi088_units(&ImuSample {
                gyro_rad_s: [value, 0., 0.],
                ..sample
            })
            .is_err()
        );
        assert!(
            imu_to_bmi088_units(&ImuSample {
                accel_m_s2: [0., value, 0.],
                ..sample
            })
            .is_err()
        );
    }
}
