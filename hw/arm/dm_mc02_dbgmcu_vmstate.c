/* VMState contract for the board-independent DBGMCU register window. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_dbgmcu.h"
#include "migration/vmstate.h"

static const VMStateDescription vmstate_dm_mc02_dbgmcu = {
    .name = "dm-mc02-dbgmcu",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Dbgmcu,
                            DM_MC02_DBGMCU_REGION_SIZE),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_dbgmcu_vmstate(void)
{
    return &vmstate_dm_mc02_dbgmcu;
}
