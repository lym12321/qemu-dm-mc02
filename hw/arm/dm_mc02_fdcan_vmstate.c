/* Component-only VMState contract for the reusable FDCAN data path. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_fdcan.h"
#include "migration/vmstate.h"

static int dm_mc02_fdcan_prepare_post_load(void *opaque, int version_id)
{
    DmMc02Fdcan *state = opaque;

    /* v1/v2 decoded Message RAM byte fields as word counts and NDTB as
     * TFQS. Their registers and RAM bytes cannot be safely reinterpreted. */
    if (version_id != 3) {
        return -EINVAL;
    }
    return dm_mc02_fdcan_state_valid(state) ? 0 : -EINVAL;
}

static int dm_mc02_fdcan_post_load(void *opaque, int version_id)
{
    DmMc02Fdcan *state = opaque;

    if (dm_mc02_fdcan_prepare_post_load(state, version_id)) {
        return -EINVAL;
    }

    /*
     * CAN bus, chardev, message RAM, retry sources and IRQ handles are
     * destination-owned.  Rebuild only derived sizes, retry projection and
     * level-sensitive IRQ projection after every serialized field is valid. */
    dm_mc02_fdcan_sync_runtime(state);
    return 0;
}

static const VMStateField vmstate_dm_mc02_fdcan_fields[] = {
    VMSTATE_UINT8_ARRAY(regs, DmMc02Fdcan, DM_MC02_FDCAN_REGION_SIZE),
    VMSTATE_UINT8_ARRAY(rx_wire, DmMc02Fdcan, DM_MC02_CAN_WIRE_SIZE),
    VMSTATE_UINT16(rx_wire_len, DmMc02Fdcan),
    VMSTATE_UINT8(rx_fifo_fill, DmMc02Fdcan),
    VMSTATE_UINT8(rx_fifo_get, DmMc02Fdcan),
    VMSTATE_UINT8(rx_fifo_put, DmMc02Fdcan),
    VMSTATE_UINT8(rx_fifo1_fill, DmMc02Fdcan),
    VMSTATE_UINT8(rx_fifo1_get, DmMc02Fdcan),
    VMSTATE_UINT8(rx_fifo1_put, DmMc02Fdcan),
    VMSTATE_UINT8(tx_fifo_put, DmMc02Fdcan),
    VMSTATE_BOOL(rx_fifo_configured, DmMc02Fdcan),
    VMSTATE_BOOL(rx_buffer_configured, DmMc02Fdcan),
    VMSTATE_BOOL(tx_fifo_configured, DmMc02Fdcan),
    VMSTATE_UINT32(tx_pending_mask, DmMc02Fdcan),
    VMSTATE_UINT8(tx_fifo_get, DmMc02Fdcan),
    VMSTATE_UINT64(rx_dropped, DmMc02Fdcan),
    VMSTATE_UINT64(tx_dropped, DmMc02Fdcan),
    VMSTATE_UINT64(tx_no_ack, DmMc02Fdcan),
    VMSTATE_UINT8_2DARRAY(tx_queue, DmMc02Fdcan,
                          DM_MC02_FDCAN_TX_QUEUE_SIZE,
                          DM_MC02_CAN_WIRE_SIZE),
    VMSTATE_UINT16_ARRAY(tx_queue_offset, DmMc02Fdcan,
                         DM_MC02_FDCAN_TX_QUEUE_SIZE),
    VMSTATE_UINT8(tx_queue_head, DmMc02Fdcan),
    VMSTATE_UINT8(tx_queue_count, DmMc02Fdcan),
    VMSTATE_UINT64(tx_short_writes, DmMc02Fdcan),
    VMSTATE_UINT64(tx_next_ns, DmMc02Fdcan),
    VMSTATE_BOOL_V(bus_off, DmMc02Fdcan, 2),
    VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_fdcan_raw = {
    .name = "dm-mc02-fdcan-raw",
    .version_id = 3,
    .minimum_version_id = 3,
    .post_load = dm_mc02_fdcan_prepare_post_load,
    .fields = vmstate_dm_mc02_fdcan_fields,
};

static const VMStateDescription vmstate_dm_mc02_fdcan = {
    .name = "dm-mc02-fdcan",
    .version_id = 3,
    .minimum_version_id = 3,
    .post_load = dm_mc02_fdcan_post_load,
    .fields = vmstate_dm_mc02_fdcan_fields,
};

const VMStateDescription *dm_mc02_fdcan_vmstate(void)
{
    return &vmstate_dm_mc02_fdcan;
}

const VMStateDescription *dm_mc02_fdcan_vmstate_raw(void)
{
    return &vmstate_dm_mc02_fdcan_raw;
}
