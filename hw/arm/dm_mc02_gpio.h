/*
 * First-stage STM32H7 GPIO bank model for the DM-MC02 board.
 *
 * This model intentionally does not contain the alternate-function matrix or
 * electrical drive rules.  An output pin reads back its ODR value through IDR;
 * board/external input values can be injected into input pins explicitly.
 */
#ifndef HW_ARM_DM_MC02_GPIO_H
#define HW_ARM_DM_MC02_GPIO_H

#include "exec/memory.h"
#include "qemu/typedefs.h"

#include <stdint.h>

typedef void DmMc02GpioOdrChanged(void *opaque, unsigned bank_index,
                                  uint32_t odr);

typedef struct DmMc02GpioBank {
    MemoryRegion iomem;
    unsigned bank_index;
    uint32_t moder;
    uint32_t otyper;
    uint32_t ospeedr;
    uint32_t pupdr;
    uint32_t idr;
    uint32_t odr;
    uint32_t afr0;
    uint32_t afr1;
    DmMc02GpioOdrChanged *odr_changed;
    void *odr_changed_opaque;
} DmMc02GpioBank;

void dm_mc02_gpio_init(DmMc02GpioBank *bank, Object *owner,
                       const char *name, unsigned bank_index,
                       DmMc02GpioOdrChanged *odr_changed,
                       void *odr_changed_opaque);
void dm_mc02_gpio_set_odr(DmMc02GpioBank *bank, uint32_t value);
/* Set externally-driven input bits without changing the output latch. */
void dm_mc02_gpio_set_input(DmMc02GpioBank *bank, uint32_t mask,
                            uint32_t value);
void dm_mc02_gpio_reset(DmMc02GpioBank *bank);

/* Component-only state contract; runtime wiring and profile metadata are not
 * part of the serialized state. */
const VMStateDescription *dm_mc02_gpio_vmstate(void);
const VMStateDescription *dm_mc02_gpio_vmstate_raw(void);
extern const VMStateDescription vmstate_dm_mc02_gpio_raw;

#endif
