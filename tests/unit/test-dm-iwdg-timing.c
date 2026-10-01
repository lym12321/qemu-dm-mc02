/* Focused tests for the board-independent IWDG LSI timing boundary. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_iwdg_timing.h"

static void test_lsi_configuration_and_effective_frequency(void)
{
    g_assert_true(dm_mc02_iwdg_lsi_config_valid(32000, 0));
    g_assert_true(dm_mc02_iwdg_lsi_config_valid(32000,
                                                DM_MC02_IWDG_MIN_LSI_ERROR_PPM));
    g_assert_true(dm_mc02_iwdg_lsi_config_valid(32000,
                                                DM_MC02_IWDG_MAX_LSI_ERROR_PPM));
    g_assert_false(dm_mc02_iwdg_lsi_config_valid(0, 0));
    g_assert_false(dm_mc02_iwdg_lsi_config_valid(32000,
                                                 DM_MC02_IWDG_MIN_LSI_ERROR_PPM - 1));
    g_assert_false(dm_mc02_iwdg_lsi_config_valid(32000,
                                                 DM_MC02_IWDG_MAX_LSI_ERROR_PPM + 1));

    g_assert_cmpuint(dm_mc02_iwdg_effective_lsi_hz(32000, 0), ==, 32000);
    g_assert_cmpuint(dm_mc02_iwdg_effective_lsi_hz(32000, 100000), ==, 35200);
    g_assert_cmpuint(dm_mc02_iwdg_effective_lsi_hz(32000, -100000), ==, 28800);
    g_assert_cmpuint(dm_mc02_iwdg_effective_lsi_hz(0, 0), ==, 0);
}

static void test_timeout_is_monotonic_and_rounded_up(void)
{
    uint64_t slow = dm_mc02_iwdg_timeout_ns_for_config(32000, -100000,
                                                       6, 31);
    uint64_t nominal = dm_mc02_iwdg_timeout_ns_for_config(32000, 0, 6, 31);
    uint64_t fast = dm_mc02_iwdg_timeout_ns_for_config(32000, 100000,
                                                       6, 31);

    g_assert_cmpuint(fast, ==, UINT64_C(232727273));
    g_assert_cmpuint(nominal, ==, UINT64_C(256000000));
    g_assert_cmpuint(slow, ==, UINT64_C(284444445));
    g_assert_cmpuint(fast, <, nominal);
    g_assert_cmpuint(nominal, <, slow);
    g_assert_cmpuint(dm_mc02_iwdg_timeout_ns_for_config(32000, 0, 6, 0), ==,
                     UINT64_C(8000000));
}

static void test_status_update_delay_uses_effective_lsi(void)
{
    uint64_t slow = dm_mc02_iwdg_status_update_delay_ns_for_config(
        32000, -100000);
    uint64_t nominal = dm_mc02_iwdg_status_update_delay_ns_for_config(
        32000, 0);
    uint64_t fast = dm_mc02_iwdg_status_update_delay_ns_for_config(
        32000, 100000);

    g_assert_cmpuint(fast, ==, UINT64_C(142046));
    g_assert_cmpuint(nominal, ==, UINT64_C(156250));
    g_assert_cmpuint(slow, ==, UINT64_C(173612));
    g_assert_cmpuint(fast, <, nominal);
    g_assert_cmpuint(nominal, <, slow);
    g_assert_cmpuint(dm_mc02_iwdg_status_update_delay_ns_for_config(0, 0), ==,
                     INT64_MAX);
}

static void test_counter_uses_absolute_deadline(void)
{
    uint64_t deadline = dm_mc02_iwdg_timeout_ns_for_config(32000, 0, 6, 31);

    g_assert_cmpuint(dm_mc02_iwdg_counter_for_deadline(32000, 0, 6, 31,
                                                       deadline, 0), ==, 31);
    g_assert_cmpuint(dm_mc02_iwdg_counter_for_deadline(32000, 0, 6, 31,
                                                       deadline, 8000000), ==, 30);
    g_assert_cmpuint(dm_mc02_iwdg_counter_for_deadline(32000, 0, 6, 31,
                                                       deadline, deadline), ==, 0);
    g_assert_cmpuint(dm_mc02_iwdg_counter_for_deadline(32000, 0, 6, 31,
                                                       deadline, deadline + 1), ==, 0);
    g_assert_cmpuint(dm_mc02_iwdg_counter_for_deadline(32000, 0, 6, 31,
                                                       0, 0), ==, 31);
    g_assert_cmpuint(dm_mc02_iwdg_counter_for_deadline(32000, 0, 6, 31,
                                                       0, 100), ==, 31);
}

static void test_invalid_and_extreme_inputs_are_bounded(void)
{
    g_assert_cmpuint(dm_mc02_iwdg_timeout_ns_for_config(0, 0, 0, 0), ==,
                     INT64_MAX);
    g_assert_cmpuint(dm_mc02_iwdg_timeout_ns_for_config(32000, 0, 7, 0), ==,
                     INT64_MAX);
    g_assert_cmpuint(dm_mc02_iwdg_timeout_ns_for_config(32000, 0, 0, 0x1000), ==,
                     INT64_MAX);
    g_assert_cmpuint(dm_mc02_iwdg_counter_for_deadline(0, 0, 0, 31, 100, 0), ==,
                     31);
    g_assert_cmpuint(dm_mc02_iwdg_timeout_ns_for_config(UINT32_MAX,
                                                        DM_MC02_IWDG_MAX_LSI_ERROR_PPM,
                                                        6, 0), ==, 30);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-iwdg-timing/configuration",
                    test_lsi_configuration_and_effective_frequency);
    g_test_add_func("/dm-iwdg-timing/timeout-monotonic",
                    test_timeout_is_monotonic_and_rounded_up);
    g_test_add_func("/dm-iwdg-timing/status-update-delay",
                    test_status_update_delay_uses_effective_lsi);
    g_test_add_func("/dm-iwdg-timing/absolute-deadline",
                    test_counter_uses_absolute_deadline);
    g_test_add_func("/dm-iwdg-timing/bounds",
                    test_invalid_and_extreme_inputs_are_bounded);
    return g_test_run();
}
