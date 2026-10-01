/* Component-only VMState contract for the board power model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_power.h"
#include "migration/vmstate.h"

static int dm_mc02_power_post_load(void *opaque, int version_id)
{
    DmMc02Power *power = opaque;

    if (version_id != 1 || !dm_mc02_power_state_valid(power)) {
        return -EINVAL;
    }

    /* ADC and board wiring are destination-owned.  Rebuild all derived rails
     * and ADC source values only after the dynamic fields are complete. */
    dm_mc02_power_sync_runtime(power);
    return 0;
}

static const VMStateField vmstate_dm_mc02_power_fields[] = {
    VMSTATE_UINT32(vin_mv, DmMc02Power),
    VMSTATE_UINT32(gpio_odr, DmMc02Power),
    VMSTATE_BOOL(electrical_power, DmMc02Power),
    VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_power_raw = {
    .name = "dm-mc02-power-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_mc02_power_fields,
};

static const VMStateDescription vmstate_dm_mc02_power = {
    .name = "dm-mc02-power",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_power_post_load,
    .fields = vmstate_dm_mc02_power_fields,
};

const VMStateDescription *dm_mc02_power_vmstate(void)
{
    return &vmstate_dm_mc02_power;
}

const VMStateDescription *dm_mc02_power_vmstate_raw(void)
{
    return &vmstate_dm_mc02_power_raw;
}
