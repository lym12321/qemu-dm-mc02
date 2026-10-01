/* Minimal Bosch M_CAN/FDCAN model for the DM-MC02 machine. */
#ifndef HW_ARM_DM_MC02_FDCAN_H
#define HW_ARM_DM_MC02_FDCAN_H

#include "chardev/char-fe.h"
#include "exec/memory.h"
#include "hw/net/can/dm_can_bus_adapter.h"
#include "net/dm_can_frame.h"
#include "hw/irq.h"
#include "migration/vmstate.h"
#include "qapi/error.h"
#include "qemu/timer.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MC02_FDCAN_REGION_SIZE 0x800
#define DM_MC02_CAN_WIRE_SIZE 84
#define DM_MC02_FDCAN_TX_QUEUE_SIZE 16

typedef struct DmMc02Fdcan {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_FDCAN_REGION_SIZE];
    uint8_t *msg_ram;
    size_t msg_ram_size;
    CharBackend chr;
    qemu_irq irq[2];
    bool chr_enabled;
    uint8_t rx_wire[DM_MC02_CAN_WIRE_SIZE];
    uint16_t rx_wire_len;
    uint8_t rx_fifo_fill;
    uint8_t rx_fifo_get;
    uint8_t rx_fifo_put;
    uint8_t rx_fifo1_fill;
    uint8_t rx_fifo1_get;
    uint8_t rx_fifo1_put;
    uint8_t tx_fifo_put;
    uint8_t rx_fifo_size;
    uint8_t rx_fifo1_size;
    uint8_t tx_fifo_size;
    bool rx_fifo_configured;
    bool rx_buffer_configured;
    bool tx_fifo_configured;
    uint32_t tx_pending_mask;
    uint8_t tx_fifo_get;
    uint64_t rx_dropped;
    uint64_t tx_dropped;
    uint64_t tx_no_ack;
    DmCanBusAdapter can_bus;
    bool transceiver_powered;
    /* Optional host-bridge policy: a connected external backend represents
     * the ACK participant for this controller.  It is deliberately off by
     * default because a chardev write is not itself a physical CAN ACK. */
    bool host_ack;
    /* Controller state entered after excessive transmit errors. */
    bool bus_off;
    uint8_t tx_queue[DM_MC02_FDCAN_TX_QUEUE_SIZE][DM_MC02_CAN_WIRE_SIZE];
    uint16_t tx_queue_offset[DM_MC02_FDCAN_TX_QUEUE_SIZE];
    uint8_t tx_queue_head;
    uint8_t tx_queue_count;
    uint64_t tx_short_writes;
    /* Absolute virtual deadline for retrying a partial/blocked chardev
     * frame.  The timer itself remains destination-owned runtime state. */
    uint64_t tx_next_ns;
    QEMUTimer *tx_timer;
    bool irq_level[2];
    bool irq_level_valid[2];
} DmMc02Fdcan;

bool dm_mc02_fdcan_init(DmMc02Fdcan *state, Object *owner, const char *name,
                        uint32_t region_size, uint8_t *msg_ram,
                        size_t msg_ram_size, Chardev *chardev, Error **errp);
void dm_mc02_fdcan_cleanup(DmMc02Fdcan *state);
/* Connect FDCAN interrupt line 0 or 1 to the Cortex-M NVIC. */
void dm_mc02_fdcan_set_irq(DmMc02Fdcan *state, unsigned line, qemu_irq irq);
void dm_mc02_fdcan_set_canbus(DmMc02Fdcan *state, CanBusState *canbus);
void dm_mc02_fdcan_set_powered(DmMc02Fdcan *state, bool powered);
void dm_mc02_fdcan_set_host_ack(DmMc02Fdcan *state, bool enabled);
void dm_mc02_fdcan_reset(DmMc02Fdcan *state);

/* Validate serialized controller-owned state without touching bus, chardev,
 * timer or IRQ wiring. */
bool dm_mc02_fdcan_state_valid(const DmMc02Fdcan *state);

/* Validate the controller's configured message-RAM regions against its
 * destination-owned RAM window.  This is intentionally separate from the
 * raw controller validator so a parent can check the shared-RAM boundary
 * after loading all serialized controller fields. */
bool dm_mc02_fdcan_message_ram_state_valid(const DmMc02Fdcan *state);

/* Rebuild destination-owned timer and IRQ projections after component state
 * restore.  CAN/chardev/message-RAM wiring remains caller-owned. */
void dm_mc02_fdcan_sync_runtime(DmMc02Fdcan *state);

/* Component-only VMState contracts; neither registers machine migration. */
const VMStateDescription *dm_mc02_fdcan_vmstate(void);
const VMStateDescription *dm_mc02_fdcan_vmstate_raw(void);

extern const VMStateDescription vmstate_dm_mc02_fdcan_raw;

#endif
