/* Board-level reset ordering for DM-MC02 GPIO and power wiring. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_board_reset.h"
#include "hw/arm/dm_mc02_exti.h"
#include "hw/arm/dm_mc02_gpio.h"
#include "hw/arm/dm_mc02_power.h"
#include "hw/arm/dm_mc02_syscfg.h"

void dm_mc02_board_reset_gpio_power(
    const DmMc02BoardGpioPowerReset *reset)
{
    if (!reset) {
        return;
    }

    for (size_t i = 0; i < reset->gpio_count; ++i) {
        dm_mc02_gpio_reset(&reset->gpio[i]);
    }
    if (reset->reset_spi_cs) {
        reset->reset_spi_cs(reset->opaque);
    }
    if (reset->syscfg) {
        dm_mc02_syscfg_reset(reset->syscfg);
    }
    if (reset->exti) {
        dm_mc02_exti_reset(reset->exti);
    }
    if (reset->apply_gpio_inputs) {
        reset->apply_gpio_inputs(reset->opaque);
    }
    if (reset->power) {
        dm_mc02_power_reset(reset->power);
    }
    if (reset->apply_power) {
        reset->apply_power(reset->opaque);
    }
}
