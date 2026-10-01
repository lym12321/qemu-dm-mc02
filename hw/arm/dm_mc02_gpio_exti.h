/* Reusable STM32H723 GPIO/SYSCFG/EXTI state composition. */
#ifndef HW_ARM_DM_MC02_GPIO_EXTI_H
#define HW_ARM_DM_MC02_GPIO_EXTI_H

#include "hw/arm/dm_mc02_exti.h"
#include "hw/arm/dm_mc02_gpio.h"
#include "hw/arm/dm_mc02_syscfg.h"

#include <stdbool.h>

#define DM_MC02_GPIO_EXTI_MAX_BANKS 8

/* Board-layer input injection remains outside this SoC composition.  The
 * callback may update GPIO sampled inputs and EXTI lines after SYSCFG has
 * been restored. */
typedef void DmMc02GpioExtiInputSync(void *opaque);

typedef struct DmMc02GpioExti {
    DmMc02GpioBank gpio[DM_MC02_GPIO_EXTI_MAX_BANKS];
    DmMc02Syscfg syscfg;
    DmMc02Exti exti;

    /* Profile geometry and input source are destination-owned wiring. */
    unsigned gpio_count;
    DmMc02GpioExtiInputSync *input_sync;
    void *input_sync_opaque;
} DmMc02GpioExti;

bool dm_mc02_gpio_exti_state_valid(const DmMc02GpioExti *state);
void dm_mc02_gpio_exti_sync_runtime(DmMc02GpioExti *state);
void dm_mc02_gpio_exti_set_input_sync(DmMc02GpioExti *state,
                                      DmMc02GpioExtiInputSync *callback,
                                      void *opaque);

/* Component-only composition.  IRQs, MemoryRegions, GPIO output callbacks
 * and board input sources are runtime wiring, not serialized state. */
const VMStateDescription *dm_mc02_gpio_exti_vmstate(void);

#endif
