/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_gpio.h"

static void write_reg(DmMc02GpioBank *s, unsigned reg, uint32_t value)
{
    s->iomem.ops->write(s->iomem.opaque, reg, value, 4);
}

static void changed(void *opaque, unsigned bank, uint32_t odr)
{
    (*(unsigned *)opaque)++;
}

static void test_output_mux_and_pulls(void)
{
    DmMc02GpioBank s;
    unsigned changes = 0;
    bool level = false;

    dm_mc02_gpio_init(&s, NULL, "gpio", 0, changed, &changes);
    write_reg(&s, 0, 1u << 14); /* PA7 output */
    write_reg(&s, 0x18, 1u << 7);
    g_assert_true(dm_mc02_gpio_pin_output(&s, 7, &level));
    g_assert_true(level);
    g_assert_cmphex(s.iomem.ops->read(&s, 0x10, 4), ==, 1u << 7);
    write_reg(&s, 4, 1u << 7); /* open drain releases high */
    g_assert_cmpuint(changes, ==, 3);
    g_assert_false(dm_mc02_gpio_pin_output(&s, 7, &level));
    write_reg(&s, 0xc, 1u << 14); /* pull-up */
    g_assert_true(dm_mc02_gpio_pin_output(&s, 7, &level));
    g_assert_true(level);
    write_reg(&s, 0x18, 1u << 23); /* open drain low beats pull-up */
    g_assert_true(dm_mc02_gpio_pin_output(&s, 7, &level));
    g_assert_false(level);
    write_reg(&s, 0, 2u << 14); /* selected AF drives through GPIO stage */
    write_reg(&s, 4, 0);
    write_reg(&s, 0xc, 0);
    dm_mc02_gpio_set_af_output(&s, 7, true, true);
    g_assert_cmphex(s.iomem.ops->read(&s, 0x10, 4), ==, 1u << 7);
    dm_mc02_gpio_set_af_output(&s, 7, false, false);
    g_assert_false(dm_mc02_gpio_pin_output(&s, 7, &level));
    write_reg(&s, 0, 3u << 14); /* analog disconnects digital output */
    write_reg(&s, 0xc, 1u << 14);
    g_assert_false(dm_mc02_gpio_pin_output(&s, 7, &level));
    /* Input pull configuration must not override an injected low/high. */
    write_reg(&s, 0, 0);
    dm_mc02_gpio_set_input(&s, 1u << 7, 0);
    g_assert_cmphex(dm_mc02_gpio_sample(&s) & (1u << 7), ==, 0);
    write_reg(&s, 0xc, 2u << 14);
    dm_mc02_gpio_set_input(&s, 1u << 7, 1u << 7);
    g_assert_cmphex(dm_mc02_gpio_sample(&s) & (1u << 7), ==, 1u << 7);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/gpio/output-mux-pulls", test_output_mux_and_pulls);
    return g_test_run();
}
