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

typedef struct GpioAccessTrace {
    DmMc02GpioBank *bank;
    unsigned calls;
    uint32_t observed_odr;
    bool pending;
    uint32_t pending_odr;
} GpioAccessTrace;

static void before_access(void *opaque)
{
    GpioAccessTrace *trace = opaque;

    trace->calls++;
    trace->observed_odr = trace->bank->odr;
    if (trace->pending) {
        /* Model an older DMA write settled by the access barrier. */
        trace->bank->odr = trace->pending_odr;
        trace->pending = false;
    }
}

static void test_access_sync_order(void)
{
    DmMc02GpioBank s;
    GpioAccessTrace trace = { .bank = &s };
    unsigned changes = 0;

    dm_mc02_gpio_init(&s, NULL, "gpio", 0, changed, &changes);
    s.before_access = before_access;
    s.before_access_opaque = &trace;
    s.odr = 2;
    trace.pending = true;
    trace.pending_odr = 4;
    write_reg(&s, 0x18, 1); /* BSRR combines with the settled ODR. */
    g_assert_cmpuint(trace.calls, ==, 1);
    g_assert_cmphex(trace.observed_odr, ==, 2);
    g_assert_cmphex(s.odr, ==, 5);
    g_assert_cmpuint(changes, ==, 1);

    trace.pending = true;
    trace.pending_odr = 8;
    g_assert_cmphex(s.iomem.ops->read(&s, 0x14, 4), ==, 8);
    g_assert_cmpuint(trace.calls, ==, 2);

    /* Capture old_odr after synchronization: this write changes nothing. */
    trace.pending = true;
    trace.pending_odr = 0x80;
    write_reg(&s, 0x14, 0x80);
    g_assert_cmpuint(trace.calls, ==, 3);
    g_assert_cmphex(trace.observed_odr, ==, 8);
    g_assert_cmpuint(changes, ==, 1);

    /* Even an initially equal set_odr must settle old events first. */
    trace.pending = true;
    trace.pending_odr = 0;
    dm_mc02_gpio_set_odr(&s, 0x80);
    g_assert_cmpuint(trace.calls, ==, 4);
    g_assert_cmphex(s.odr, ==, 0x80);
    g_assert_cmpuint(changes, ==, 2);

    s.moder = 1u << 14;
    trace.pending = true;
    trace.pending_odr = 0;
    g_assert_cmphex(s.iomem.ops->read(&s, 0x10, 4), ==, 0);
    g_assert_cmpuint(trace.calls, ==, 5);

    dm_mc02_gpio_set_af_output(&s, 7, true, true);
    dm_mc02_gpio_set_input(&s, 1, 1);
    dm_mc02_gpio_reset(&s);
    g_assert_cmpuint(trace.calls, ==, 5);
    g_assert_true(s.before_access == before_access);
    g_assert_true(s.before_access_opaque == &trace);
    g_assert_cmphex(s.iomem.ops->read(&s, 0x14, 4), ==, 0);
    g_assert_cmpuint(trace.calls, ==, 6);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/gpio/output-mux-pulls", test_output_mux_and_pulls);
    g_test_add_func("/gpio/access-sync-order", test_access_sync_order);
    return g_test_run();
}
