/* Reusable STM32H723 FDCAN instance to SoC Message RAM boundary. */
#ifndef HW_ARM_DM_MC02_FDCAN_MSG_RAM_LINK_H
#define HW_ARM_DM_MC02_FDCAN_MSG_RAM_LINK_H

#include "hw/arm/dm_mc02_fdcan.h"
#include "hw/misc/dm_message_ram.h"

/* The Message RAM bytes are owned and migrated by DmMessageRam.  Each link
 * serializes only the geometry marker and the controller-owned raw state. */
typedef struct DmMc02FdcanMsgRamLink {
    DmMc02Fdcan fdcan;
    DmMessageRam *msg_ram;
    uint32_t msg_ram_size;
} DmMc02FdcanMsgRamLink;

/* Bind destination-owned shared RAM.  The link borrows the DmMessageRam and
 * does not take ownership or serialize its MemoryRegion/data bytes. */
bool dm_mc02_fdcan_msg_ram_link_bind(DmMc02FdcanMsgRamLink *link,
                                     DmMessageRam *msg_ram);

/* Validate raw FDCAN state, RAM identity/geometry and every configured
 * message element range without touching timer, IRQ, CAN or chardev state. */
bool dm_mc02_fdcan_msg_ram_link_state_valid(
    const DmMc02FdcanMsgRamLink *link);

/* Rebuild destination-owned timer and level-sensitive IRQ projection after
 * the complete link state has been validated. */
void dm_mc02_fdcan_msg_ram_link_sync_runtime(
    DmMc02FdcanMsgRamLink *link);

/* Component-only VMState contract; not machine-level migration. */
const VMStateDescription *dm_mc02_fdcan_msg_ram_link_vmstate(void);

#endif
