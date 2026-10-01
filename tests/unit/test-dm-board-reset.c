/* Focused reset-order contract test for the board GPIO/power stage. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_board_reset.h"
#include "hw/arm/dm_mc02_exti.h"
#include "hw/arm/dm_mc02_gpio.h"
#include "hw/arm/dm_mc02_power.h"
#include "hw/arm/dm_mc02_syscfg.h"

typedef struct ResetTrace {
    unsigned events[8];
    unsigned count;
} ResetTrace;

static ResetTrace *active_trace;

void dm_test_board_reset_record(unsigned event);

void dm_test_board_reset_record(unsigned event)
{
    g_assert_nonnull(active_trace);
    g_assert_cmpuint(active_trace->count, <, ARRAY_SIZE(active_trace->events));
    active_trace->events[active_trace->count++] = event;
}

static void hook(void *opaque, unsigned event)
{
    (void)opaque;
    dm_test_board_reset_record(event);
}

static void reset_spi_cs(void *opaque)
{
    hook(opaque, 200);
}

static void apply_gpio_inputs(void *opaque)
{
    hook(opaque, 400);
}

static void apply_power(void *opaque)
{
    hook(opaque, 600);
}

static void test_gpio_power_order(void)
{
    DmMc02GpioBank gpio[2] = { 0 };
    DmMc02Syscfg syscfg = { 0 };
    DmMc02Exti exti = { 0 };
    DmMc02Power power = { 0 };
    DmMc02BoardGpioPowerReset reset = {
        .gpio = gpio,
        .gpio_count = ARRAY_SIZE(gpio),
        .syscfg = &syscfg,
        .exti = &exti,
        .power = &power,
        .reset_spi_cs = reset_spi_cs,
        .apply_gpio_inputs = apply_gpio_inputs,
        .apply_power = apply_power,
    };
    static const unsigned expected[] = {
        100, 101, 200, 300, 500, 400, 700, 600,
    };

    gpio[0].bank_index = 0;
    gpio[1].bank_index = 1;
    active_trace = &(ResetTrace) { 0 };
    dm_mc02_board_reset_gpio_power(&reset);
    g_assert_cmpuint(active_trace->count, ==, ARRAY_SIZE(expected));
    for (unsigned i = 0; i < ARRAY_SIZE(expected); ++i) {
        g_assert_cmpuint(active_trace->events[i], ==, expected[i]);
    }
    active_trace = NULL;
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/board-reset/gpio-power-order",
                    test_gpio_power_order);
    return g_test_run();
}
