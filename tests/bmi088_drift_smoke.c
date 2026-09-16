#include "dm_mc02_bmi088_signal.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static void check_condition(bool condition, const char *expression,
                            unsigned line)
{
    if (!condition) {
        fprintf(stderr, "bmi088 drift smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) \
    check_condition((expression), #expression, __LINE__)

static void check_close(double actual, double expected, double tolerance)
{
    CHECK(isfinite(actual));
    CHECK(fabs(actual - expected) <= tolerance);
}

static void check_vector_close(const float actual[DM_MC02_BMI088_SIGNAL_AXES],
                               const double expected
                               [DM_MC02_BMI088_SIGNAL_AXES],
                               double tolerance)
{
    for (unsigned axis = 0; axis < DM_MC02_BMI088_SIGNAL_AXES; ++axis) {
        check_close(actual[axis], expected[axis], tolerance);
    }
}

static void check_vector_equal(const float left[DM_MC02_BMI088_SIGNAL_AXES],
                               const float right
                               [DM_MC02_BMI088_SIGNAL_AXES])
{
    for (unsigned axis = 0; axis < DM_MC02_BMI088_SIGNAL_AXES; ++axis) {
        CHECK(left[axis] == right[axis]);
    }
}

static void configure_drift(DmMc02Bmi088Signal *signal,
                            const double coefficient
                            [DM_MC02_BMI088_SIGNAL_AXES],
                            const double random_walk
                            [DM_MC02_BMI088_SIGNAL_AXES],
                            uint32_t seed)
{
    dm_mc02_bmi088_signal_init(signal, DM_MC02_BMI088_SIGNAL_GYRO);
    dm_mc02_bmi088_signal_set_temperature_coefficient(signal, coefficient);
    dm_mc02_bmi088_signal_set_bias_random_walk(signal, random_walk);
    dm_mc02_bmi088_signal_set_temperature(signal, 25.0);
    dm_mc02_bmi088_signal_set_rng(signal, seed);
}

static void test_reference_temperature_has_no_drift(void)
{
    DmMc02Bmi088Signal signal;
    const double coefficient[3] = { 0.125, -0.25, 0.5 };
    const double random_walk[3] = { 0.0, 0.0, 0.0 };
    const float input[3] = { 0.0f, 0.0f, 0.0f };
    const double expected[3] = { 0.0, 0.0, 0.0 };
    float output[3];

    configure_drift(&signal, coefficient, random_walk, UINT32_C(1));
    CHECK(dm_mc02_bmi088_signal_sample(&signal, input, 0, output));
    check_vector_close(output, expected, 0.0);
}

static void test_temperature_coefficient_is_per_axis(void)
{
    DmMc02Bmi088Signal signal;
    const double coefficient[3] = { 0.1, -0.2, 0.35 };
    const double random_walk[3] = { 0.0, 0.0, 0.0 };
    const float input[3] = { 0.0f, 0.0f, 0.0f };
    const double expected[3] = { 1.0, -2.0, 3.5 };
    double returned[3];
    float output[3];

    configure_drift(&signal, coefficient, random_walk, UINT32_C(2));
    dm_mc02_bmi088_signal_set_temperature(&signal, 35.0);
    dm_mc02_bmi088_signal_get_temperature_coefficient(&signal, returned);
    for (unsigned axis = 0; axis < DM_MC02_BMI088_SIGNAL_AXES; ++axis) {
        check_close(returned[axis], coefficient[axis], 0.0);
    }
    CHECK(dm_mc02_bmi088_signal_sample(&signal, input, 0, output));
    check_vector_close(output, expected, 1.0e-6);
}

static void test_random_walk_same_seed_is_deterministic(void)
{
    DmMc02Bmi088Signal left;
    DmMc02Bmi088Signal right;
    const double coefficient[3] = { 0.0, 0.0, 0.0 };
    const double random_walk[3] = { 1.0, 0.5, 0.25 };
    const float input[3] = { 0.0f, 0.0f, 0.0f };
    float left_output[3];
    float right_output[3];

    configure_drift(&left, coefficient, random_walk, UINT32_C(0x13579bdf));
    configure_drift(&right, coefficient, random_walk, UINT32_C(0x13579bdf));
    for (uint64_t time_ns = 0; time_ns <= UINT64_C(2000000000);
         time_ns += UINT64_C(1000000000)) {
        CHECK(dm_mc02_bmi088_signal_sample(&left, input, time_ns,
                                           left_output));
        CHECK(dm_mc02_bmi088_signal_sample(&right, input, time_ns,
                                           right_output));
        check_vector_equal(left_output, right_output);
    }
}

static void test_random_walk_changes_output(void)
{
    DmMc02Bmi088Signal signal;
    const double coefficient[3] = { 0.0, 0.0, 0.0 };
    const double random_walk[3] = { 1.0, 0.0, 0.0 };
    const float input[3] = { 0.0f, 0.0f, 0.0f };
    float first[3];
    float second[3];

    configure_drift(&signal, coefficient, random_walk, UINT32_C(0x2468ace1));
    CHECK(dm_mc02_bmi088_signal_sample(&signal, input, 0, first));
    CHECK(dm_mc02_bmi088_signal_sample(&signal, input, UINT64_C(1000000000),
                                       second));
    CHECK(fabsf(second[0] - first[0]) > 1.0e-7f);
    CHECK(second[1] == first[1]);
    CHECK(second[2] == first[2]);
}

static void test_rejected_odr_sample_does_not_advance_walk(void)
{
    DmMc02Bmi088Signal with_rejection;
    DmMc02Bmi088Signal without_rejection;
    const double coefficient[3] = { 0.0, 0.0, 0.0 };
    const double random_walk[3] = { 0.75, 0.5, 0.25 };
    const float input[3] = { 0.0f, 0.0f, 0.0f };
    float rejected_output[3];
    float accepted_after_rejection[3];
    float reference_output[3];

    configure_drift(&with_rejection, coefficient, random_walk,
                    UINT32_C(0x10203040));
    configure_drift(&without_rejection, coefficient, random_walk,
                    UINT32_C(0x10203040));
    dm_mc02_bmi088_signal_set_odr_period(&with_rejection,
                                         UINT64_C(1000000000));

    CHECK(dm_mc02_bmi088_signal_sample(&with_rejection, input, 0,
                                       rejected_output));
    CHECK(dm_mc02_bmi088_signal_sample(&without_rejection, input, 0,
                                       reference_output));
    CHECK(!dm_mc02_bmi088_signal_sample(&with_rejection, input,
                                        UINT64_C(500000000),
                                        rejected_output));
    CHECK(dm_mc02_bmi088_signal_sample(&with_rejection, input,
                                       UINT64_C(1000000000),
                                       accepted_after_rejection));
    CHECK(dm_mc02_bmi088_signal_sample(&without_rejection, input,
                                       UINT64_C(1000000000), reference_output));
    check_vector_equal(accepted_after_rejection, reference_output);
}

static void test_reset_clears_dynamic_bias_and_keeps_configuration(void)
{
    DmMc02Bmi088Signal signal;
    const double coefficient[3] = { 0.1, -0.2, 0.3 };
    const double random_walk[3] = { 1.0, 0.5, 0.25 };
    const double bias[3] = { 0.25, -0.5, 0.75 };
    const float input[3] = { 0.0f, 0.0f, 0.0f };
    const double expected[3] = { 0.75, -1.5, 2.25 };
    double returned_coefficient[3];
    double returned_random_walk[3];
    float output[3];

    configure_drift(&signal, coefficient, random_walk, UINT32_C(0xabcdef01));
    dm_mc02_bmi088_signal_set_bias(&signal, bias);
    dm_mc02_bmi088_signal_set_temperature(&signal, 30.0);
    CHECK(dm_mc02_bmi088_signal_sample(&signal, input, 0, output));
    CHECK(dm_mc02_bmi088_signal_sample(&signal, input, UINT64_C(1000000000),
                                       output));
    CHECK(signal.dynamic_bias[0] != 0.0 || signal.dynamic_bias[1] != 0.0 ||
          signal.dynamic_bias[2] != 0.0);

    dm_mc02_bmi088_signal_reset(&signal);
    for (unsigned axis = 0; axis < DM_MC02_BMI088_SIGNAL_AXES; ++axis) {
        CHECK(signal.dynamic_bias[axis] == 0.0);
    }
    CHECK(!signal.bias_time_valid);
    dm_mc02_bmi088_signal_get_temperature_coefficient(&signal,
                                                      returned_coefficient);
    dm_mc02_bmi088_signal_get_bias_random_walk(&signal, returned_random_walk);
    for (unsigned axis = 0; axis < DM_MC02_BMI088_SIGNAL_AXES; ++axis) {
        check_close(returned_coefficient[axis], coefficient[axis], 0.0);
        check_close(returned_random_walk[axis], random_walk[axis], 0.0);
    }
    CHECK(dm_mc02_bmi088_signal_sample(&signal, input, 0, output));
    check_vector_close(output, expected, 1.0e-6);
}

int main(void)
{
    test_reference_temperature_has_no_drift();
    test_temperature_coefficient_is_per_axis();
    test_random_walk_same_seed_is_deterministic();
    test_random_walk_changes_output();
    test_rejected_odr_sample_does_not_advance_walk();
    test_reset_clears_dynamic_bias_and_keeps_configuration();
    puts("bmi088 drift smoke: PASS");
    return 0;
}
