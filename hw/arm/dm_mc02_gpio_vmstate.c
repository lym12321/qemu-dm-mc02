/* VMState contract for the board-independent GPIO register state. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_gpio.h"
#include "migration/vmstate.h"

static int dm_mc02_gpio_post_load(void *opaque, int version_id)
{
    DmMc02GpioBank *bank = opaque;

    if (version_id != 1) {
        return -EINVAL;
    }
    if (bank->odr_changed) {
        bank->odr_changed(bank->odr_changed_opaque, bank->bank_index,
                          bank->odr);
    }
    return 0;
}

static const VMStateField vmstate_dm_mc02_gpio_fields[] = {
        VMSTATE_UINT32(moder, DmMc02GpioBank),
        VMSTATE_UINT32(otyper, DmMc02GpioBank),
        VMSTATE_UINT32(ospeedr, DmMc02GpioBank),
        VMSTATE_UINT32(pupdr, DmMc02GpioBank),
        VMSTATE_UINT32(idr, DmMc02GpioBank),
        VMSTATE_UINT32(odr, DmMc02GpioBank),
        VMSTATE_UINT32(afr0, DmMc02GpioBank),
        VMSTATE_UINT32(afr1, DmMc02GpioBank),
        VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_gpio_raw = {
    .name = "dm-mc02-gpio-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_mc02_gpio_fields,
};

static const VMStateDescription vmstate_dm_mc02_gpio = {
    .name = "dm-mc02-gpio",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_gpio_post_load,
    .fields = vmstate_dm_mc02_gpio_fields,
};

const VMStateDescription *dm_mc02_gpio_vmstate(void)
{
    return &vmstate_dm_mc02_gpio;
}

const VMStateDescription *dm_mc02_gpio_vmstate_raw(void)
{
    return &vmstate_dm_mc02_gpio_raw;
}
