/*
 * Device-independent BMI088 sampling signal model.
 *
 * The model deliberately contains no QEMU object types.  A board/device
 * adapter supplies physical samples and timestamps, then consumes the
 * processed values or their SPI-width raw representation.
 */
#ifndef DM_MC02_BMI088_SIGNAL_H
#define DM_MC02_BMI088_SIGNAL_H

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_BMI088_SIGNAL_AXES 3
#define DM_MC02_BMI088_SIGNAL_DEFAULT_TEMPERATURE_C 25.0

typedef enum DmMc02Bmi088SignalKind {
    DM_MC02_BMI088_SIGNAL_ACCEL = 0,
    DM_MC02_BMI088_SIGNAL_GYRO = 1,
} DmMc02Bmi088SignalKind;

typedef struct DmMc02Bmi088Signal {
    DmMc02Bmi088SignalKind kind;
    double noise_std;
    double bias[DM_MC02_BMI088_SIGNAL_AXES];
    double temperature_coefficient[DM_MC02_BMI088_SIGNAL_AXES];
    double bias_random_walk_std[DM_MC02_BMI088_SIGNAL_AXES];
    double dynamic_bias[DM_MC02_BMI088_SIGNAL_AXES];
    double full_scale;
    double temperature_c;
    double temperature_reference_c;
    uint32_t rng;
    double bandwidth_hz;
    double filter_state[DM_MC02_BMI088_SIGNAL_AXES];
    uint64_t filter_time_ns;
    bool filter_valid;
    uint64_t odr_period_ns;
    uint64_t next_sample_time_ns;
    bool sample_time_valid;
    uint64_t bias_time_ns;
    bool bias_time_valid;
} DmMc02Bmi088Signal;

void dm_mc02_bmi088_signal_init(DmMc02Bmi088Signal *signal,
                                DmMc02Bmi088SignalKind kind);
void dm_mc02_bmi088_signal_reset(DmMc02Bmi088Signal *signal);

void dm_mc02_bmi088_signal_set_kind(DmMc02Bmi088Signal *signal,
                                    DmMc02Bmi088SignalKind kind);
void dm_mc02_bmi088_signal_set_noise(DmMc02Bmi088Signal *signal,
                                     double standard_deviation);
void dm_mc02_bmi088_signal_set_bias(DmMc02Bmi088Signal *signal,
                                    const double bias[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_signal_set_temperature_coefficient(
    DmMc02Bmi088Signal *signal,
    const double coefficient[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_signal_get_temperature_coefficient(
    const DmMc02Bmi088Signal *signal,
    double coefficient[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_signal_set_bias_random_walk(
    DmMc02Bmi088Signal *signal,
    const double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_signal_get_bias_random_walk(
    const DmMc02Bmi088Signal *signal,
    double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_signal_set_full_scale(DmMc02Bmi088Signal *signal,
                                          double full_scale);
void dm_mc02_bmi088_signal_set_temperature(DmMc02Bmi088Signal *signal,
                                            double temperature_c);
void dm_mc02_bmi088_signal_set_rng(DmMc02Bmi088Signal *signal,
                                   uint32_t seed);
void dm_mc02_bmi088_signal_set_bandwidth(DmMc02Bmi088Signal *signal,
                                         double bandwidth_hz);
void dm_mc02_bmi088_signal_set_odr_period(DmMc02Bmi088Signal *signal,
                                          uint64_t period_ns);

/* Returns false for a sample rejected by the configured ODR window. */
bool dm_mc02_bmi088_signal_accept_sample(DmMc02Bmi088Signal *signal,
                                         uint64_t sensor_time_ns);

/* Adds configured bias/noise and applies the stateful low-pass filter. */
bool dm_mc02_bmi088_signal_sample(DmMc02Bmi088Signal *signal,
                                  const float input[DM_MC02_BMI088_SIGNAL_AXES],
                                  uint64_t sensor_time_ns,
                                  float output[DM_MC02_BMI088_SIGNAL_AXES]);

/* Returns a deterministic near-normal sample in the sensor's input unit. */
float dm_mc02_bmi088_signal_noise(DmMc02Bmi088Signal *signal);

int16_t dm_mc02_bmi088_signal_raw(const DmMc02Bmi088Signal *signal,
                                  float value);
void dm_mc02_bmi088_signal_raw_xyz(const DmMc02Bmi088Signal *signal,
                                   const float values[DM_MC02_BMI088_SIGNAL_AXES],
                                   int16_t raw[DM_MC02_BMI088_SIGNAL_AXES]);

#endif
