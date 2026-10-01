/* See dm_can_bus_adapter.h for the intentionally small boundary. */
#include "qemu/osdep.h"
#include "hw/net/can/dm_can_bus_adapter.h"

static bool dm_can_bus_can_receive(CanBusClientState *client)
{
    DmCanBusAdapter *adapter = container_of(client, DmCanBusAdapter,
                                            client);

    return adapter->can_receive && adapter->can_receive(adapter->opaque);
}

static ssize_t dm_can_bus_receive(CanBusClientState *client,
                                  const qemu_can_frame *frames,
                                  size_t frames_cnt)
{
    DmCanBusAdapter *adapter = container_of(client, DmCanBusAdapter,
                                            client);

    if (!frames || !frames_cnt || !adapter->receive) {
        return 0;
    }
    for (size_t i = 0; i < frames_cnt; ++i) {
        adapter->receive(adapter->opaque, &frames[i]);
    }
    /* CAN bus ACK participation is selected by can_receive(), not by the
     * destination's acceptance filter or FIFO capacity. */
    return frames_cnt;
}

static CanBusClientInfo dm_can_bus_client_info = {
    .can_receive = dm_can_bus_can_receive,
    .receive = dm_can_bus_receive,
};

void dm_can_bus_adapter_init(DmCanBusAdapter *adapter)
{
    if (adapter) {
        memset(adapter, 0, sizeof(*adapter));
    }
}

bool dm_can_bus_adapter_connect(DmCanBusAdapter *adapter,
                                CanBusState *bus,
                                void *opaque,
                                DmCanBusCanReceive can_receive,
                                DmCanBusReceive receive)
{
    if (!adapter || !bus || !can_receive || !receive) {
        return false;
    }
    /* The caller owns lifecycle ordering.  An explicit init avoids treating
     * a first-use object as an already connected QEMU client; reconnects
     * must go through disconnect() so the QTAILQ link is removed first. */
    if (adapter->connected) {
        return false;
    }
    dm_can_bus_adapter_init(adapter);
    adapter->client.info = &dm_can_bus_client_info;
    adapter->opaque = opaque;
    adapter->can_receive = can_receive;
    adapter->receive = receive;
    if (can_bus_insert_client(bus, &adapter->client) < 0) {
        memset(adapter, 0, sizeof(*adapter));
        return false;
    }
    adapter->connected = true;
    return true;
}

void dm_can_bus_adapter_disconnect(DmCanBusAdapter *adapter)
{
    if (!adapter) {
        return;
    }
    if (adapter->connected) {
        can_bus_remove_client(&adapter->client);
    }
    adapter->connected = false;
    adapter->opaque = NULL;
    adapter->can_receive = NULL;
    adapter->receive = NULL;
    adapter->client.info = NULL;
}

ssize_t dm_can_bus_adapter_send(DmCanBusAdapter *adapter,
                                const qemu_can_frame *frame)
{
    if (!adapter || !adapter->connected || !frame) {
        return -1;
    }
    return can_bus_client_send(&adapter->client, frame, 1);
}
