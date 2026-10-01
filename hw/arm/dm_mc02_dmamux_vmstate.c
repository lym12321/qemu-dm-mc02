/* VMState contract for the board-independent DMAMUX component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_dma.h"
#include "migration/vmstate.h"

const VMStateDescription vmstate_dm_mc02_dmamux = {
    .name = "dm-mc02-dmamux",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Dmamux,
                            DM_MC02_DMAMUX_REGION_SIZE),
        VMSTATE_UINT64(generation, DmMc02Dmamux),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_dmamux_vmstate(void)
{
    return &vmstate_dm_mc02_dmamux;
}
