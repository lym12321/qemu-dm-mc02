/* Narrow adapter from a reusable controller callback to QEMU's CAN bus. */
#ifndef HW_NET_CAN_DM_CAN_BUS_ADAPTER_H
#define HW_NET_CAN_DM_CAN_BUS_ADAPTER_H

#include "net/can_emu.h"

#include <stdbool.h>
#include <stddef.h>

typedef bool (*DmCanBusCanReceive)(void *opaque);
typedef void (*DmCanBusReceive)(void *opaque,
                                const qemu_can_frame *frame);

typedef struct DmCanBusAdapter {
    CanBusClientState client;
    void *opaque;
    DmCanBusCanReceive can_receive;
    DmCanBusReceive receive;
    bool connected;
} DmCanBusAdapter;

/* Initialize an adapter before its first connect. */
void dm_can_bus_adapter_init(DmCanBusAdapter *adapter);

/*
 * Connect one controller endpoint to a QEMU standard CanBusState.
 * The adapter must have been initialized and disconnected.  Reconnects use
 * an explicit disconnect() followed by connect().
 */
bool dm_can_bus_adapter_connect(DmCanBusAdapter *adapter,
                                 CanBusState *bus,
                                 void *opaque,
                                 DmCanBusCanReceive can_receive,
                                 DmCanBusReceive receive);

/* Remove the endpoint. Safe for an already disconnected adapter. */
void dm_can_bus_adapter_disconnect(DmCanBusAdapter *adapter);

/* Send one QEMU-format frame. The return value is the standard bus result:
 * positive means at least one peer accepted the frame, zero means no peer
 * accepted it, and negative means the endpoint is disconnected. */
ssize_t dm_can_bus_adapter_send(DmCanBusAdapter *adapter,
                                const qemu_can_frame *frame);

#endif
