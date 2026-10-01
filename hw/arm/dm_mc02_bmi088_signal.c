#include "dm_mc02_bmi088_signal.h"

#include <math.h>
#include <string.h>

#define DM_MC02_BMI088_SIGNAL_DEFAULT_RNG UINT32_C(0x6d2b79f5)
#define DM_MC02_BMI088_SIGNAL_TWO_PI 6.28318530717958647692

static double default_full_scale(DmMc02Bmi088SignalKind kind)
{
    return kind == DM_MC02_BMI088_SIGNAL_ACCEL ? 3.0 : 2000.0;
}

static bool valid_kind(DmMc02Bmi088SignalKind kind)
{
    return kind == DM_MC02_BMI088_SIGNAL_ACCEL ||
           kind == DM_MC02_BMI088_SIGNAL_GYRO;
}

void dm_mc02_bmi088_signal_init(DmMc02Bmi088Signal *signal,
                                DmMc02Bmi088SignalKind kind)
{
    memset(signal, 0, sizeof(*signal));
    signal->kind = valid_kind(kind) ? kind : DM_MC02_BMI088_SIGNAL_ACCEL;
    signal->full_scale = default_full_scale(signal->kind);
    signal->temperature_reference_c =
        DM_MC02_BMI088_SIGNAL_DEFAULT_TEMPERATURE_C;
    signal->rng = DM_MC02_BMI088_SIGNAL_DEFAULT_RNG;
}

void dm_mc02_bmi088_signal_reset(DmMc02Bmi088Signal *signal)
{
    DmMc02Bmi088Signal saved = *signal;
    DmMc02Bmi088SignalKind kind = valid_kind(saved.kind) ? saved.kind :
                                   DM_MC02_BMI088_SIGNAL_ACCEL;

    dm_mc02_bmi088_signal_init(signal, kind);
    signal->noise_std = saved.noise_std;
    memcpy(signal->bias, saved.bias, sizeof(signal->bias));
    memcpy(signal->temperature_coefficient, saved.temperature_coefficient,
           sizeof(signal->temperature_coefficient));
    memcpy(signal->bias_random_walk_std, saved.bias_random_walk_std,
           sizeof(signal->bias_random_walk_std));
    signal->temperature_c = saved.temperature_c;
    signal->temperature_reference_c = saved.temperature_reference_c;
    signal->rng = saved.rng ? saved.rng : DM_MC02_BMI088_SIGNAL_DEFAULT_RNG;
}

void dm_mc02_bmi088_signal_set_kind(DmMc02Bmi088Signal *signal,
                                    DmMc02Bmi088SignalKind kind)
{
    if (!valid_kind(kind)) {
        return;
    }
    signal->kind = kind;
    signal->full_scale = default_full_scale(kind);
    signal->filter_valid = false;
    signal->sample_time_valid = false;
}

void dm_mc02_bmi088_signal_set_noise(DmMc02Bmi088Signal *signal,
                                     double standard_deviation)
{
    if (isfinite(standard_deviation) && standard_deviation >= 0.0) {
        signal->noise_std = standard_deviation;
    }
}

void dm_mc02_bmi088_signal_set_bias(DmMc02Bmi088Signal *signal,
                                    const double bias[DM_MC02_BMI088_SIGNAL_AXES])
{
    memcpy(signal->bias, bias, sizeof(signal->bias));
}

void dm_mc02_bmi088_signal_set_temperature_coefficient(
    DmMc02Bmi088Signal *signal,
    const double coefficient[DM_MC02_BMI088_SIGNAL_AXES])
{
    memcpy(signal->temperature_coefficient, coefficient,
           sizeof(signal->temperature_coefficient));
}

void dm_mc02_bmi088_signal_get_temperature_coefficient(
    const DmMc02Bmi088Signal *signal,
    double coefficient[DM_MC02_BMI088_SIGNAL_AXES])
{
    memcpy(coefficient, signal->temperature_coefficient,
           sizeof(signal->temperature_coefficient));
}

void dm_mc02_bmi088_signal_set_bias_random_walk(
    DmMc02Bmi088Signal *signal,
    const double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES])
{
    memcpy(signal->bias_random_walk_std, standard_deviation,
           sizeof(signal->bias_random_walk_std));
    signal->bias_time_valid = false;
}

void dm_mc02_bmi088_signal_get_bias_random_walk(
    const DmMc02Bmi088Signal *signal,
    double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES])
{
    memcpy(standard_deviation, signal->bias_random_walk_std,
           sizeof(signal->bias_random_walk_std));
}

void dm_mc02_bmi088_signal_set_full_scale(DmMc02Bmi088Signal *signal,
                                          double full_scale)
{
    if (isfinite(full_scale) && full_scale > 0.0) {
        signal->full_scale = full_scale;
    }
}

void dm_mc02_bmi088_signal_set_temperature(DmMc02Bmi088Signal *signal,
                                            double temperature_c)
{
    if (isfinite(temperature_c)) {
        signal->temperature_c = temperature_c;
    }
}

void dm_mc02_bmi088_signal_set_rng(DmMc02Bmi088Signal *signal, uint32_t seed)
{
    signal->rng = seed ? seed : DM_MC02_BMI088_SIGNAL_DEFAULT_RNG;
}

void dm_mc02_bmi088_signal_set_bandwidth(DmMc02Bmi088Signal *signal,
                                         double bandwidth_hz)
{
    if (isfinite(bandwidth_hz) && bandwidth_hz >= 0.0) {
        signal->bandwidth_hz = bandwidth_hz;
        signal->filter_valid = false;
    }
}

void dm_mc02_bmi088_signal_set_odr_period(DmMc02Bmi088Signal *signal,
                                          uint64_t period_ns)
{
    signal->odr_period_ns = period_ns;
    signal->next_sample_time_ns = 0;
    signal->sample_time_valid = false;
}

bool dm_mc02_bmi088_signal_accept_sample(DmMc02Bmi088Signal *signal,
                                         uint64_t sensor_time_ns)
{
    if (signal->odr_period_ns && signal->sample_time_valid &&
        sensor_time_ns < signal->next_sample_time_ns) {
        return false;
    }
    if (signal->odr_period_ns) {
        signal->sample_time_valid = true;
        signal->next_sample_time_ns = sensor_time_ns + signal->odr_period_ns;
    }
    return true;
}

static float dm_mc02_bmi088_signal_standard_normal(DmMc02Bmi088Signal *signal)
{
    double sum = 0.0;

    for (unsigned i = 0; i < 6; ++i) {
        signal->rng = signal->rng * UINT32_C(1664525) + UINT32_C(1013904223);
        sum += (double)signal->rng / 4294967296.0;
    }
    return (float)((sum - 3.0) * 1.41421356237309504880);
}

float dm_mc02_bmi088_signal_noise(DmMc02Bmi088Signal *signal)
{
    if (signal->noise_std == 0.0) {
        return 0.0f;
    }
    return (float)(signal->noise_std *
                   dm_mc02_bmi088_signal_standard_normal(signal));
}

static void dm_mc02_bmi088_signal_update_bias(DmMc02Bmi088Signal *signal,
                                               uint64_t sensor_time_ns)
{
    bool random_walk = false;

    for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
        random_walk |= signal->bias_random_walk_std[i] != 0.0;
    }
    if (!random_walk) {
        return;
    }
    if (signal->bias_time_valid && sensor_time_ns > signal->bias_time_ns) {
        double dt_seconds = (double)(sensor_time_ns - signal->bias_time_ns) *
                            1.0e-9;
        double scale = sqrt(dt_seconds);

        for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
            signal->dynamic_bias[i] += signal->bias_random_walk_std[i] *
                                       scale *
                                       dm_mc02_bmi088_signal_standard_normal(
                                           signal);
        }
    }
    signal->bias_time_ns = sensor_time_ns;
    signal->bias_time_valid = true;
}

bool dm_mc02_bmi088_signal_sample(DmMc02Bmi088Signal *signal,
                                  const float input[DM_MC02_BMI088_SIGNAL_AXES],
                                  uint64_t sensor_time_ns,
                                  float output[DM_MC02_BMI088_SIGNAL_AXES])
{
    double dt_seconds;
    double alpha;
    float adjusted[DM_MC02_BMI088_SIGNAL_AXES];

    if (!dm_mc02_bmi088_signal_accept_sample(signal, sensor_time_ns)) {
        return false;
    }
    dm_mc02_bmi088_signal_update_bias(signal, sensor_time_ns);
    for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
        double temperature_bias =
            signal->temperature_coefficient[i] *
            (signal->temperature_c - signal->temperature_reference_c);

        adjusted[i] = input[i] + (float)(signal->bias[i] + temperature_bias +
                                         signal->dynamic_bias[i]) +
                      dm_mc02_bmi088_signal_noise(signal);
    }
    if (!signal->bandwidth_hz || !signal->filter_valid ||
        sensor_time_ns < signal->filter_time_ns) {
        for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
            signal->filter_state[i] = adjusted[i];
            output[i] = adjusted[i];
        }
        signal->filter_time_ns = sensor_time_ns;
        signal->filter_valid = true;
        return true;
    }
    dt_seconds = (double)(sensor_time_ns - signal->filter_time_ns) * 1.0e-9;
    alpha = DM_MC02_BMI088_SIGNAL_TWO_PI * signal->bandwidth_hz * dt_seconds;
    alpha /= 1.0 + alpha;
    if (alpha < 0.0) {
        alpha = 0.0;
    } else if (alpha > 1.0) {
        alpha = 1.0;
    }
    for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
        signal->filter_state[i] += alpha *
                                   ((double)adjusted[i] - signal->filter_state[i]);
        output[i] = (float)signal->filter_state[i];
    }
    signal->filter_time_ns = sensor_time_ns;
    return true;
}

int16_t dm_mc02_bmi088_signal_raw(const DmMc02Bmi088Signal *signal,
                                  float value)
{
    double scaled;
    double rounded;

    if (!isfinite(value)) {
        return 0;
    }
    scaled = (double)value * (32768.0 / signal->full_scale);
    rounded = scaled >= 0.0 ? scaled + 0.5 : scaled - 0.5;
    if (rounded >= 32767.0) {
        return INT16_MAX;
    }
    if (rounded <= -32768.0) {
        return INT16_MIN;
    }
    return (int16_t)rounded;
}

void dm_mc02_bmi088_signal_raw_xyz(const DmMc02Bmi088Signal *signal,
                                   const float values[DM_MC02_BMI088_SIGNAL_AXES],
                                   int16_t raw[DM_MC02_BMI088_SIGNAL_AXES])
{
    for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
        raw[i] = dm_mc02_bmi088_signal_raw(signal, values[i]);
    }
}
