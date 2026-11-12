/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_ws2812.h"

static uint64_t word(DmWs2812 *s, uint64_t t, uint32_t grb)
{
    for (int bit = 23; bit >= 0; --bit) {
        bool one = grb & (1u << bit);
        dm_ws2812_level(s, t, true, true);
        dm_ws2812_level(s, t + (one ? 800 : 400), true, false);
        t += 1250;
    }
    return t;
}

static void test_latch_and_cascade(void)
{
    DmWs2812 s;
    uint64_t t;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    t = word(&s, 50000, 0x123456);
    t = word(&s, t, 0xffffff);
    g_assert_cmphex(s.rgb, ==, 0);
    dm_ws2812_advance(&s, dm_ws2812_deadline(&s) - 1);
    g_assert_cmpuint(s.frames, ==, 0);
    dm_ws2812_advance(&s, dm_ws2812_deadline(&s));
    g_assert_cmphex(s.rgb, ==, 0x341256);
    g_assert_cmpuint(s.frames, ==, 1);
    g_assert_cmpuint(s.errors, ==, 0);
    g_assert_cmpuint(s.frame_ns, ==, t - 450 + 50000);
    t = word(&s, t + 60000, 0);
    dm_ws2812_advance(&s, t + 50000);
    g_assert_cmphex(s.rgb, ==, 0);
    g_assert_cmpuint(s.frames, ==, 2);
}

static void test_last_bit_low_before_cascade(void)
{
    DmWs2812 s;
    uint64_t t;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    t = word(&s, 50000, 0x123456);
    /* Last zero fell at t-850: the next rise leaves only 500ns low. */
    dm_ws2812_level(&s, t - 350, true, true);
    dm_ws2812_level(&s, t + 50, true, false);
    dm_ws2812_advance(&s, t + 50050);
    g_assert_cmpuint(s.frames, ==, 0);
    g_assert_cmpuint(s.errors, ==, 1);
}

static void test_reject_and_resync(void)
{
    DmWs2812 s;
    uint64_t t;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    t = word(&s, 50000, 0x00ff00);
    dm_ws2812_advance(&s, t + 50000);
    g_assert_cmphex(s.rgb, ==, 0xff0000);
    t += 60000;
    dm_ws2812_level(&s, t, true, true);
    dm_ws2812_level(&s, t + 600, true, false); /* neither symbol */
    t = word(&s, t + 1250, 0xffffff);
    dm_ws2812_advance(&s, t + 50000);
    g_assert_cmphex(s.rgb, ==, 0xff0000);
    g_assert_cmpuint(s.errors, ==, 1);
    t += 60000;
    dm_ws2812_level(&s, t, true, true);
    dm_ws2812_level(&s, t + 400, true, false);
    dm_ws2812_level(&s, t + 900, true, true); /* zero low too short */
    dm_ws2812_level(&s, t + 1300, true, false);
    dm_ws2812_advance(&s, t + 60000);
    g_assert_cmpuint(s.errors, ==, 2);
    t = word(&s, t + 70000, 0x0000ff);
    dm_ws2812_level(&s, t, false, false); /* disconnect before latch */
    dm_ws2812_advance(&s, t + 60000);
    g_assert_cmpuint(s.frames, ==, 1);
    g_assert_cmpuint(s.errors, ==, 3);
    dm_ws2812_level(&s, t + 70000, true, false);
    t = word(&s, t + 120000, 0x0000ff);
    dm_ws2812_advance(&s, t + 60000);
    g_assert_cmphex(s.rgb, ==, 0x0000ff);
    g_assert_cmpuint(s.frames, ==, 2);
}

static void test_static_and_partial(void)
{
    DmWs2812 s;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, true);
    dm_ws2812_advance(&s, 1000000);
    g_assert_cmpuint(s.frames, ==, 0);
    dm_ws2812_level(&s, 1000000, true, false);
    dm_ws2812_level(&s, 1050000, true, true);
    dm_ws2812_level(&s, 1050400, true, false);
    dm_ws2812_advance(&s, 1100400);
    g_assert_cmpuint(s.frames, ==, 0);
    g_assert_cmpuint(s.errors, ==, 1);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/ws2812/latch-cascade", test_latch_and_cascade);
    g_test_add_func("/ws2812/reject-resync", test_reject_and_resync);
    g_test_add_func("/ws2812/static-partial", test_static_and_partial);
    g_test_add_func("/ws2812/last-low-before-cascade", test_last_bit_low_before_cascade);
    return g_test_run();
}
