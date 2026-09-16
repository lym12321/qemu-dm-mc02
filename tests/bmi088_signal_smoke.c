#include "dm_mc02_bmi088_signal.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static void require_condition(bool condition, const char *expression,
                              unsigned line)
{
    if (!condition) {
        fprintf(stderr, "bmi088 signal smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) \
    require_condition((expression), #expression, __LINE__)

static void test_odr(void)
{
    DmMc02Bmi088Signal signal;
    float in[3] = { 1.0f, 0.0f, 0.0f };
    float out[3];

    dm_mc02_bmi088_signal_init(&signal, DM_MC02_BMI088_SIGNAL_ACCEL);
    dm_mc02_bmi088_signal_set_odr_period(&signal, 1000);
    CHECK(dm_mc02_bmi088_signal_sample(&signal, in, 100, out));
    CHECK(!dm_mc02_bmi088_signal_sample(&signal, in, 1099, out));
    CHECK(dm_mc02_bmi088_signal_sample(&signal, in, 1100, out));
    CHECK(!dm_mc02_bmi088_signal_sample(&signal, in, 1000, out));
}

static void test_bias_noise_seed(void)
{
    DmMc02Bmi088Signal a, b;
    double bias[3] = { 0.25, -0.5, 1.0 };
    float input[3] = { 0.0f, 0.0f, 0.0f };
    float ao[3], bo[3];

    dm_mc02_bmi088_signal_init(&a, DM_MC02_BMI088_SIGNAL_GYRO);
    dm_mc02_bmi088_signal_init(&b, DM_MC02_BMI088_SIGNAL_GYRO);
    dm_mc02_bmi088_signal_set_bias(&a, bias);
    dm_mc02_bmi088_signal_set_bias(&b, bias);
    dm_mc02_bmi088_signal_set_noise(&a, 0.1);
    dm_mc02_bmi088_signal_set_noise(&b, 0.1);
    dm_mc02_bmi088_signal_set_rng(&a, 1234);
    dm_mc02_bmi088_signal_set_rng(&b, 1234);
    CHECK(dm_mc02_bmi088_signal_sample(&a, input, 0, ao));
    CHECK(dm_mc02_bmi088_signal_sample(&b, input, 0, bo));
    for (unsigned i = 0; i < 3; ++i) {
        CHECK(ao[i] == bo[i]);
        CHECK(isfinite(ao[i]));
    }
    CHECK(fabsf(ao[0] - 0.25f) < 0.5f);
}

static void test_filter_step(void)
{
    DmMc02Bmi088Signal signal;
    float zero[3] = { 0.0f, 0.0f, 0.0f };
    float step[3] = { 1.0f, 0.0f, 0.0f };
    float out[3];

    dm_mc02_bmi088_signal_init(&signal, DM_MC02_BMI088_SIGNAL_ACCEL);
    dm_mc02_bmi088_signal_set_bandwidth(&signal, 10.0);
    CHECK(dm_mc02_bmi088_signal_sample(&signal, zero, 0, out));
    CHECK(dm_mc02_bmi088_signal_sample(&signal, step, 1000000, out));
    CHECK(out[0] > 0.0f && out[0] < 1.0f);
    CHECK(dm_mc02_bmi088_signal_sample(&signal, step, 1000000000, out));
    CHECK(out[0] > 0.9f && out[0] < 1.0f);
    CHECK(dm_mc02_bmi088_signal_sample(&signal, step, 2000000000, out));
    CHECK(out[0] > 0.99f && out[0] < 1.01f);
}

static void test_raw_saturation(void)
{
    DmMc02Bmi088Signal accel, gyro;

    dm_mc02_bmi088_signal_init(&accel, DM_MC02_BMI088_SIGNAL_ACCEL);
    dm_mc02_bmi088_signal_init(&gyro, DM_MC02_BMI088_SIGNAL_GYRO);
    CHECK(dm_mc02_bmi088_signal_raw(&accel, 3.0f) == INT16_MAX);
    CHECK(dm_mc02_bmi088_signal_raw(&accel, -3.0f) == INT16_MIN);
    CHECK(dm_mc02_bmi088_signal_raw(&gyro, 2000.0f) == INT16_MAX);
    CHECK(dm_mc02_bmi088_signal_raw(&gyro, -2000.0f) == INT16_MIN);
    CHECK(dm_mc02_bmi088_signal_raw(&gyro, NAN) == 0);
}

int main(void)
{
    test_odr();
    test_bias_noise_seed();
    test_filter_step();
    test_raw_saturation();
    puts("bmi088 signal smoke: PASS");
    return 0;
}
