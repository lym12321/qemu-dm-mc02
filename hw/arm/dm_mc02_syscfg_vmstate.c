/* VMState contract for the board-independent SYSCFG component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_syscfg.h"
#include "migration/vmstate.h"

static int dm_mc02_syscfg_post_load(void *opaque, int version_id)
{
    DmMc02Syscfg *state = opaque;

    if (version_id != 1) {
        return -EINVAL;
    }
    if (state->changed) {
        state->changed(state->changed_opaque);
    }
    return 0;
}

static const VMStateField vmstate_dm_mc02_syscfg_fields[] = {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Syscfg,
                            DM_MC02_SYSCFG_REGION_SIZE),
        VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_syscfg_raw = {
    .name = "dm-mc02-syscfg-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_mc02_syscfg_fields,
};

static const VMStateDescription vmstate_dm_mc02_syscfg = {
    .name = "dm-mc02-syscfg",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_syscfg_post_load,
    .fields = vmstate_dm_mc02_syscfg_fields,
};

const VMStateDescription *dm_mc02_syscfg_vmstate(void)
{
    return &vmstate_dm_mc02_syscfg;
}

const VMStateDescription *dm_mc02_syscfg_vmstate_raw(void)
{
    return &vmstate_dm_mc02_syscfg_raw;
}
