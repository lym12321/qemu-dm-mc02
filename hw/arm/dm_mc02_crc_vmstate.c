/* VMState contract for the board-independent CRC component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_crc.h"
#include "migration/vmstate.h"

static const VMStateDescription vmstate_dm_mc02_crc = {
    .name = "dm-mc02-crc",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, DmMc02Crc,
                             DM_MC02_CRC_REGION_SIZE / sizeof(uint32_t)),
        VMSTATE_UINT32(value, DmMc02Crc),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_crc_vmstate(void)
{
    return &vmstate_dm_mc02_crc;
}
