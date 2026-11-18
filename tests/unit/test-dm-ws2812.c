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

static uint64_t word_with_low(DmWs2812 *s, uint64_t t, uint32_t grb,
                              uint64_t low_ns)
{
    for (int bit = 23; bit >= 0; --bit) {
        uint64_t high_ns = grb & (1u << bit) ? 800 : 400;

        dm_ws2812_level(s, t, true, true);
        dm_ws2812_level(s, t + high_ns, true, false);
        t += high_ns + low_ns;
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

static void test_low_width_independent(void)
{
    static const uint64_t low_widths[] = { 1, 300, 850, 2000, 49999 };

    for (unsigned i = 0; i < ARRAY_SIZE(low_widths); ++i) {
        DmWs2812 s;
        uint64_t deadline;

        dm_ws2812_reset(&s, 0);
        dm_ws2812_level(&s, 0, true, false);
        word_with_low(&s, 50000, 0x123456, low_widths[i]);
        deadline = dm_ws2812_deadline(&s);
        dm_ws2812_advance(&s, deadline - 1);
        g_assert_cmpuint(s.frames, ==, 0);
        dm_ws2812_advance(&s, deadline);
        g_assert_cmphex(s.rgb, ==, 0x341256);
        g_assert_cmpuint(s.frames, ==, 1);
        g_assert_cmpuint(s.errors, ==, 0);
    }
}

static void test_low_width_before_cascade(void)
{
    DmWs2812 s;
    uint64_t t;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    t = word(&s, 50000, 0x123456);
    /* A 500 ns low gap after bit 24 still belongs to the same cascade. */
    dm_ws2812_level(&s, t - 350, true, true);
    dm_ws2812_level(&s, t + 50, true, false);
    dm_ws2812_advance(&s, t + 50050);
    g_assert_cmphex(s.rgb, ==, 0x341256);
    g_assert_cmpuint(s.frames, ==, 1);
    g_assert_cmpuint(s.errors, ==, 0);
}

static void test_reset_interrupts_partial(void)
{
    DmWs2812 s;
    uint64_t t;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    dm_ws2812_level(&s, 50000, true, true);
    dm_ws2812_level(&s, 50400, true, false);
    /* The first rising edge at exactly 50 us low starts a new frame. */
    t = word(&s, 100400, 0x123456);
    g_assert_cmpuint(s.errors, ==, 1);
    g_assert_cmpuint(s.frames, ==, 0);
    dm_ws2812_advance(&s, t + 50000);
    g_assert_cmphex(s.rgb, ==, 0x341256);
    g_assert_cmpuint(s.frames, ==, 1);
    g_assert_cmpuint(s.errors, ==, 1);
}

static void test_repeated_levels(void)
{
    DmWs2812 s;
    uint64_t t = 50000;
    uint64_t deadline;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    for (unsigned i = 0; i < 24; ++i) {
        dm_ws2812_level(&s, t, true, true);
        dm_ws2812_level(&s, t + 200, true, true);
        dm_ws2812_level(&s, t + 400, true, false);
        t += 1250;
    }
    deadline = dm_ws2812_deadline(&s);
    dm_ws2812_level(&s, deadline - 1, true, false);
    g_assert_cmpuint(dm_ws2812_deadline(&s), ==, deadline);
    g_assert_cmpuint(s.frames, ==, 0);
    dm_ws2812_level(&s, deadline, true, false);
    g_assert_cmphex(s.rgb, ==, 0);
    g_assert_cmpuint(s.frames, ==, 1);
    g_assert_cmpuint(s.errors, ==, 0);
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
    dm_ws2812_level(&s, t + 900, true, true);
    dm_ws2812_level(&s, t + 1300, true, false);
    dm_ws2812_advance(&s, t + 60000); /* incomplete frame */
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

static void test_backward_timestamp(void)
{
    DmWs2812 s;
    uint64_t t;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    t = word(&s, 50000, 0x00ff00);
    dm_ws2812_advance(&s, t + 50000);
    dm_ws2812_level(&s, t + 49999, true, true);
    g_assert_cmpuint(s.last_ns, ==, t + 50000);
    g_assert_cmpuint(s.errors, ==, 1);
    g_assert_cmphex(s.rgb, ==, 0xff0000);
    t = word(&s, t + 60000, 0x0000ff);
    dm_ws2812_advance(&s, t + 50000);
    g_assert_cmpuint(s.frames, ==, 2);
    g_assert_cmpuint(s.errors, ==, 1);
    g_assert_cmphex(s.rgb, ==, 0x0000ff);
}

static void test_resync_preserves_telemetry(void)
{
    DmWs2812 s;
    uint64_t t;

    dm_ws2812_reset(&s, 0);
    dm_ws2812_level(&s, 0, true, false);
    t = word(&s, 50000, 0x123456);
    dm_ws2812_advance(&s, t + 50000);
    g_assert_cmpuint(s.frames, ==, 1);
    g_assert_cmphex(s.rgb, ==, 0x341256);
    dm_ws2812_level(&s, t + 60000, true, true);
    dm_ws2812_level(&s, t + 60400, true, false);
    dm_ws2812_resync(&s, t + 110400);
    g_assert_cmpuint(s.frames, ==, 1);
    g_assert_cmphex(s.rgb, ==, 0x341256);
    g_assert_cmpuint(s.errors, ==, 0);
    g_assert_cmpuint(s.count, ==, 0);
    g_assert_false(s.known);
    g_assert_false(s.synchronized);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/ws2812/latch-cascade", test_latch_and_cascade);
    g_test_add_func("/ws2812/reject-resync", test_reject_and_resync);
    g_test_add_func("/ws2812/static-partial", test_static_and_partial);
    g_test_add_func("/ws2812/low-width-independent", test_low_width_independent);
    g_test_add_func("/ws2812/low-width-before-cascade", test_low_width_before_cascade);
    g_test_add_func("/ws2812/reset-interrupts-partial", test_reset_interrupts_partial);
    g_test_add_func("/ws2812/repeated-levels", test_repeated_levels);
    g_test_add_func("/ws2812/backward-timestamp", test_backward_timestamp);
    g_test_add_func("/ws2812/resync-preserves-telemetry",
                    test_resync_preserves_telemetry);
    return g_test_run();
}
