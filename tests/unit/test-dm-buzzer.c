/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_buzzer.h"

static void test_short_tone_and_dc(void)
{
    DmBuzzer s = { 0 };
    dm_buzzer_reset(&s, 100);
    dm_buzzer_observe(&s, 200, true, 2000, 500);
    dm_buzzer_observe(&s, 300, true, 2000, 500);
    dm_buzzer_observe(&s, 1000200, false, 0, 0);
    g_assert_cmpuint(s.sequence, ==, 3);
    g_assert_cmpuint(dm_buzzer_event(&s, 2)->frequency_hz, ==, 2000);
    g_assert_cmpuint(dm_buzzer_event(&s, 3)->timestamp_ns -
                     dm_buzzer_event(&s, 2)->timestamp_ns, ==, 1000000);
    dm_buzzer_observe(&s, 2000000, true, 2000, 1000);
    g_assert_cmpuint(s.sequence, ==, 3);
    dm_buzzer_observe(&s, 2100000, true, 2000, 0);
    g_assert_cmpuint(s.sequence, ==, 3);
}

static void test_overwrite_and_reset(void)
{
    DmBuzzer s = { 0 };
    dm_buzzer_reset(&s, 0);
    for (unsigned i = 1; i <= DM_BUZZER_EVENTS; ++i) {
        dm_buzzer_observe(&s, i, true, 1000 + i, 500);
    }
    g_assert_null(dm_buzzer_event(&s, 1));
    g_assert_nonnull(dm_buzzer_event(&s, 2));
    g_assert_null(dm_buzzer_event(&s, s.sequence + 1));
    dm_buzzer_reset(&s, 1000);
    g_assert_cmpuint(s.generation, ==, 2);
    g_assert_cmpuint(s.sequence, ==, 1);
    g_assert_cmpuint(dm_buzzer_event(&s, 1)->frequency_hz, ==, 0);
    g_assert_null(dm_buzzer_event(&s, 2));
}

static void test_gpio_pitch_and_stop(void)
{
    DmBuzzer s = { 0 };
    dm_buzzer_reset(&s, 0);
    dm_buzzer_pin(&s, 0, true, false);
    for (unsigned i = 0; i < 4; ++i) {
        dm_buzzer_pin(&s, 100 + i * 500000, true, true);
        dm_buzzer_pin(&s, 100 + i * 500000 + 125000, true, false);
    }
    g_assert_cmpuint(s.frequency_hz, ==, 2000);
    g_assert_cmpuint(s.duty_permille, ==, 250);
    g_assert_cmpuint(s.sequence, ==, 2);
    dm_buzzer_pin_advance(&s, 2500100);
    g_assert_cmpuint(s.frequency_hz, ==, 0);
    g_assert_cmpuint(dm_buzzer_event(&s, 3)->timestamp_ns, ==, 2000100);
    dm_buzzer_pin(&s, 3000000, true, true);
    dm_buzzer_pin_advance(&s, 10000000);
    g_assert_cmpuint(s.frequency_hz, ==, 0); /* static high */
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-buzzer/short-tone-dc", test_short_tone_and_dc);
    g_test_add_func("/dm-buzzer/overwrite-reset", test_overwrite_and_reset);
    g_test_add_func("/dm-buzzer/gpio-pitch-stop", test_gpio_pitch_and_stop);
    return g_test_run();
}
