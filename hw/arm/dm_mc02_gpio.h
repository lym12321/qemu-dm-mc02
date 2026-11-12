/*
 * First-stage STM32H7 GPIO bank model for the DM-MC02 board.
 *
 * The board supplies the selected alternate-function output. GPIO owns mode,
 * push-pull/open-drain and pull resolution, shared by IDR and observers.
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
    /* Runtime projection supplied by the selected peripheral, not registers. */
    uint16_t af_driven;
    uint16_t af_level;
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
/* A selected AF producer supplies its current output; no GPIO write callback. */
void dm_mc02_gpio_set_af_output(DmMc02GpioBank *bank, unsigned pin,
                                bool driven, bool level);
/* False denotes an undefined output. Pulls resolve released open-drain pins.
 * External input values continue to use set_input and IDR. */
bool dm_mc02_gpio_pin_output(const DmMc02GpioBank *bank, unsigned pin,
                             bool *level);
/* Read-only digital observation, using the same projection as IDR. */
uint16_t dm_mc02_gpio_output_mask(const DmMc02GpioBank *bank);
uint16_t dm_mc02_gpio_sample(const DmMc02GpioBank *bank);
void dm_mc02_gpio_reset(DmMc02GpioBank *bank);

/* Component-only state contract; runtime wiring and profile metadata are not
 * part of the serialized state. */
const VMStateDescription *dm_mc02_gpio_vmstate(void);
const VMStateDescription *dm_mc02_gpio_vmstate_raw(void);
extern const VMStateDescription vmstate_dm_mc02_gpio_raw;

#endif
