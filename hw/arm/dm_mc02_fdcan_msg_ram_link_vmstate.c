/* Composite VMState for one FDCAN controller and shared SoC Message RAM. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_fdcan_msg_ram_link.h"
#include "migration/vmstate.h"

bool dm_mc02_fdcan_msg_ram_link_bind(DmMc02FdcanMsgRamLink *link,
                                     DmMessageRam *msg_ram)
{
    if (!link || !msg_ram || !msg_ram->data || !msg_ram->size ||
        msg_ram->size > UINT32_MAX) {
        return false;
    }

    link->msg_ram = msg_ram;
    link->msg_ram_size = (uint32_t)msg_ram->size;
    link->fdcan.msg_ram = msg_ram->data;
    link->fdcan.msg_ram_size = msg_ram->size;
    return true;
}

bool dm_mc02_fdcan_msg_ram_link_state_valid(
    const DmMc02FdcanMsgRamLink *link)
{
    const DmMessageRam *msg_ram;

    if (!link || !(msg_ram = link->msg_ram) || !msg_ram->data ||
        !msg_ram->size || msg_ram->size > UINT32_MAX ||
        link->msg_ram_size != msg_ram->size ||
        link->fdcan.msg_ram != msg_ram->data ||
        link->fdcan.msg_ram_size != msg_ram->size) {
        return false;
    }
    return dm_mc02_fdcan_message_ram_state_valid(&link->fdcan);
}

void dm_mc02_fdcan_msg_ram_link_sync_runtime(
    DmMc02FdcanMsgRamLink *link)
{
    if (link) {
        dm_mc02_fdcan_sync_runtime(&link->fdcan);
    }
}

static int dm_mc02_fdcan_msg_ram_link_pre_save(void *opaque)
{
    DmMc02FdcanMsgRamLink *link = opaque;
    uint64_t size;

    if (!link || !link->msg_ram || !link->msg_ram->data ||
        !(size = link->msg_ram->size) || size > UINT32_MAX) {
        return -EINVAL;
    }
    /* The marker is derived from the destination-owned geometry at the save
     * boundary.  Loading still compares it with the destination RAM. */
    link->msg_ram_size = (uint32_t)size;
    return dm_mc02_fdcan_msg_ram_link_state_valid(link) ? 0 : -EINVAL;
}

static int dm_mc02_fdcan_msg_ram_link_post_load(void *opaque,
                                                int version_id)
{
    DmMc02FdcanMsgRamLink *link = opaque;

    if (!link || version_id != 1 ||
        !dm_mc02_fdcan_msg_ram_link_state_valid(link)) {
        return -EINVAL;
    }
    /* The raw child has no timer/IRQ/CAN/chardev projection.  The parent is
     * the only owner of the shared-RAM boundary and performs the final
     * runtime projection after geometry and all controller fields validate. */
    dm_mc02_fdcan_msg_ram_link_sync_runtime(link);
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_fdcan_msg_ram_link = {
    .name = "dm-mc02-fdcan-msg-ram-link",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_fdcan_msg_ram_link_pre_save,
    .post_load = dm_mc02_fdcan_msg_ram_link_post_load,
    .fields = (VMStateField[]) {
        /* Geometry is first so a destination can reject a different RAM
         * owner before any runtime projection is allowed. */
        VMSTATE_UINT32(msg_ram_size, DmMc02FdcanMsgRamLink),
        VMSTATE_STRUCT(fdcan, DmMc02FdcanMsgRamLink, 0,
                       vmstate_dm_mc02_fdcan_raw, DmMc02Fdcan),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_fdcan_msg_ram_link_vmstate(void)
{
    return &vmstate_dm_mc02_fdcan_msg_ram_link;
}
