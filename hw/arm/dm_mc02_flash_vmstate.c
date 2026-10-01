/* Component-only VMState contract for the DM-MC02 Flash register model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_flash.h"
#include "migration/vmstate.h"

static int dm_mc02_flash_post_load(void *opaque, int version_id)
{
    DmMc02Flash *state = opaque;

    if (version_id != 1) {
        return -EINVAL;
    }
    dm_mc02_flash_sync_runtime(state);
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_flash = {
    .name = "dm-mc02-flash",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_flash_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Flash,
                            DM_MC02_FLASH_REG_REGION_SIZE),
        VMSTATE_BOOL(key1_seen, DmMc02Flash),
        VMSTATE_BOOL(optkey_seen, DmMc02Flash),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_flash_vmstate(void)
{
    return &vmstate_dm_mc02_flash;
}
