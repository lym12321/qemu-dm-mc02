#[cfg(any(
    feature = "devices",
    feature = "machine",
    feature = "worker",
    feature = "lab"
))]
pub mod bmi088;
#[cfg(feature = "devices")]
pub mod dji;
#[cfg(feature = "referee")]
pub mod referee;
