/*
 * TEST-ONLY fixture: process-local CAN medium used for future scheduler tests.
 * It is not part of the DM-MC02 ARM production target.
 *
 * This is deliberately a virtual-time message switch.  It is not a
 * SocketCAN ABI and does not model bit timing, electrical levels, ACK
 * slots, error frames, bus-off recovery, or a physical transceiver.
 */
#ifndef HW_ARM_DM_MC02_CAN_MEDIUM_H
#define HW_ARM_DM_MC02_CAN_MEDIUM_H

#include "qemu/timer.h"
#include "net/dm_can_frame.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MC02_CAN_MEDIUM_MAX_NODES 8
#define DM_MC02_CAN_MEDIUM_QUEUE_SIZE 128

typedef bool (*DmMc02CanMediumEnabled)(void *opaque);
typedef void (*DmMc02CanMediumDeliver)(void *opaque,
                                       const DmMc02CanFrame *frame);
typedef void (*DmMc02CanMediumComplete)(void *opaque,
                                        const DmMc02CanFrame *frame,
                                        bool acknowledged);

typedef struct DmMc02CanMediumNode {
    bool registered;
    void *opaque;
    unsigned registration_order;
    DmMc02CanMediumEnabled enabled;
    DmMc02CanMediumDeliver deliver;
    DmMc02CanMediumComplete complete;
} DmMc02CanMediumNode;

typedef struct DmMc02CanMediumPending {
    bool used;
    void *sender;
    DmMc02CanFrame frame;
    unsigned registration_order;
    uint64_t sequence;
} DmMc02CanMediumPending;

typedef struct DmMc02CanMedium {
    QEMUTimer *dispatch_timer;
    DmMc02CanMediumNode nodes[DM_MC02_CAN_MEDIUM_MAX_NODES];
    DmMc02CanMediumPending pending[DM_MC02_CAN_MEDIUM_QUEUE_SIZE];
    /* Fixed-capacity min-heap of pending slot indices.  The frame storage
     * stays in the stable array so cancellation and reset remain cheap. */
    uint8_t pending_heap[DM_MC02_CAN_MEDIUM_QUEUE_SIZE];
    int16_t pending_heap_pos[DM_MC02_CAN_MEDIUM_QUEUE_SIZE];
    size_t pending_heap_count;
    uint64_t next_sequence;
    unsigned next_registration_order;
    size_t pending_count;
    uint64_t busy_until_ns;
} DmMc02CanMedium;

void dm_mc02_can_medium_init(DmMc02CanMedium *medium);
void dm_mc02_can_medium_cleanup(DmMc02CanMedium *medium);
void dm_mc02_can_medium_reset(DmMc02CanMedium *medium);
bool dm_mc02_can_medium_register(DmMc02CanMedium *medium, void *opaque,
                                 DmMc02CanMediumEnabled enabled,
                                 DmMc02CanMediumDeliver deliver,
                                 DmMc02CanMediumComplete complete);
void dm_mc02_can_medium_unregister(DmMc02CanMedium *medium, void *opaque);
void dm_mc02_can_medium_cancel_sender(DmMc02CanMedium *medium, void *sender);
bool dm_mc02_can_medium_submit(DmMc02CanMedium *medium, void *sender,
                               const DmMc02CanFrame *frame);

#endif
