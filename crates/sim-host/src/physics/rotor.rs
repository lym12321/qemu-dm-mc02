//! Existing uncalibrated half-implicit rigid rotor. Torque is Nm, position rad
//! and velocity rad/s; no electrical dynamics, gearbox loss or saturation.

use sim_core::{Error, Result};

#[derive(Clone, Copy, Debug)]
pub struct RotorOptions {
    pub inertia: f64,
    pub damping: f64,
    pub initial_position: f64,
    pub initial_velocity: f64,
}
impl Default for RotorOptions {
    fn default() -> Self {
        Self {
            inertia: 0.02,
            damping: 0.08,
            initial_position: 0.0,
            initial_velocity: 0.0,
        }
    }
}

pub struct Rotor {
    options: RotorOptions,
    position: f64,
    velocity: f64,
    effort: f64,
}
impl Rotor {
    pub fn new(options: RotorOptions) -> Result<Self> {
        if ![
            options.inertia,
            options.damping,
            options.initial_position,
            options.initial_velocity,
        ]
        .iter()
        .all(|v| v.is_finite())
            || options.inertia <= 0.0
            || options.damping < 0.0
        {
            return Err(std::sync::Arc::new(Error::Admission(
                "rotor parameters must be finite, inertia positive and damping nonnegative".into(),
            )));
        }
        let mut rotor = Self {
            options,
            position: 0.0,
            velocity: 0.0,
            effort: 0.0,
        };
        rotor.reset();
        Ok(rotor)
    }
    pub fn reset(&mut self) {
        self.position = self.options.initial_position;
        self.velocity = self.options.initial_velocity;
        self.effort = 0.0;
    }
    pub fn state(&self) -> (f64, f64) {
        (self.position, self.velocity)
    }
    pub fn initial_state(&self) -> (f64, f64) {
        (self.options.initial_position, self.options.initial_velocity)
    }
    pub fn effort(&self) -> f64 {
        self.effort
    }
    pub fn step(&mut self, dt: f64, effort: f64) -> Result<()> {
        if !dt.is_finite() || dt < 0.0 || !effort.is_finite() {
            return Err(std::sync::Arc::new(Error::Admission(
                "rotor dt must be finite/nonnegative and effort finite".into(),
            )));
        }
        let acceleration = (effort - self.options.damping * self.velocity) / self.options.inertia;
        self.velocity += acceleration * dt;
        self.position += self.velocity * dt;
        self.effort = effort;
        Ok(())
    }
}
