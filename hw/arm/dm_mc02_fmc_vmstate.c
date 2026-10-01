/* VMState contract for the board-independent FMC register window. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_fmc.h"
#include "migration/vmstate.h"

static const VMStateDescription vmstate_dm_mc02_fmc = {
    .name = "dm-mc02-fmc",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Fmc, DM_MC02_FMC_REG_REGION_SIZE),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_fmc_vmstate(void)
{
    return &vmstate_dm_mc02_fmc;
}
