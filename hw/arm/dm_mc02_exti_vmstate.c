/* Component-only VMState contract for the DM-MC02 EXTI model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_exti.h"
#include "migration/vmstate.h"

static int dm_mc02_exti_post_load(void *opaque, int version_id)
{
    DmMc02Exti *state = opaque;

    if (version_id != 1) {
        return -EINVAL;
    }
    dm_mc02_exti_sync_runtime(state);
    return 0;
}

static const VMStateField vmstate_dm_mc02_exti_fields[] = {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Exti, DM_MC02_EXTI_REGION_SIZE),
        VMSTATE_UINT16(line_level, DmMc02Exti),
        VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_exti_raw = {
    .name = "dm-mc02-exti-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_mc02_exti_fields,
};

static const VMStateDescription vmstate_dm_mc02_exti = {
    .name = "dm-mc02-exti",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_exti_post_load,
    .fields = vmstate_dm_mc02_exti_fields,
};

const VMStateDescription *dm_mc02_exti_vmstate(void)
{
    return &vmstate_dm_mc02_exti;
}

const VMStateDescription *dm_mc02_exti_vmstate_raw(void)
{
    return &vmstate_dm_mc02_exti_raw;
}
