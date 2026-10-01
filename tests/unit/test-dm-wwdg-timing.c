/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_wwdg_timing.h"

static void test_nominal_tick(void)
{
    g_assert_cmpuint(dm_mc02_wwdg_tick_ns_for_config(64000000, 0), ==,
                     64000);
    g_assert_cmpuint(dm_mc02_wwdg_tick_ns_for_config(64000000, 1), ==,
                     128000);
}

static void test_rounds_up(void)
{
    g_assert_cmpuint(dm_mc02_wwdg_tick_ns_for_config(24000000, 0), ==,
                     170667);
}

static void test_invalid_clock_or_divider(void)
{
    g_assert_cmpuint(dm_mc02_wwdg_tick_ns_for_config(0, 0), ==, 0);
    g_assert_cmpuint(dm_mc02_wwdg_tick_ns_for_config(64000000, 8), ==, 0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/wwdg-timing/nominal", test_nominal_tick);
    g_test_add_func("/dm-mc02/wwdg-timing/rounds-up", test_rounds_up);
    g_test_add_func("/dm-mc02/wwdg-timing/invalid", test_invalid_clock_or_divider);
    return g_test_run();
}
