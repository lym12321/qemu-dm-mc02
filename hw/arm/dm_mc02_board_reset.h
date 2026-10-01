/* Board-level reset ordering for DM-MC02 GPIO and power wiring. */
#ifndef HW_ARM_DM_MC02_BOARD_RESET_H
#define HW_ARM_DM_MC02_BOARD_RESET_H

#include <stddef.h>

typedef struct DmMc02Exti DmMc02Exti;
typedef struct DmMc02GpioBank DmMc02GpioBank;
typedef struct DmMc02Power DmMc02Power;
typedef struct DmMc02Syscfg DmMc02Syscfg;

typedef void DmMc02BoardResetHook(void *opaque);

/*
 * The board owns the order in which generic GPIO state is reset and its
 * external projections are rebuilt.  The hooks are composition-owned because
 * CS, input and downstream power consumers belong to the machine wiring.
 */
typedef struct DmMc02BoardGpioPowerReset {
    DmMc02GpioBank *gpio;
    size_t gpio_count;
    DmMc02Syscfg *syscfg;
    DmMc02Exti *exti;
    DmMc02Power *power;
    DmMc02BoardResetHook *reset_spi_cs;
    DmMc02BoardResetHook *apply_gpio_inputs;
    DmMc02BoardResetHook *apply_power;
    void *opaque;
} DmMc02BoardGpioPowerReset;

/*
 * Reset order is: GPIO latches/configuration, inactive SPI CS, SYSCFG/EXTI,
 * external GPIO inputs, power source model, then downstream power consumers.
 * The caller resets unrelated SoC/peripheral state around this board stage.
 */
void dm_mc02_board_reset_gpio_power(
    const DmMc02BoardGpioPowerReset *reset);

#endif
