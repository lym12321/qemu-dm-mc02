/* Isolated contract test for the reusable QEMU CAN bus adapter. */
#include "qemu/osdep.h"
#include "qemu/module.h"
#include "hw/net/can/dm_can_bus_adapter.h"

typedef struct AdapterEndpoint {
    bool enabled;
    unsigned received;
    qemu_can_frame last;
} AdapterEndpoint;

static bool endpoint_can_receive(void *opaque)
{
    AdapterEndpoint *endpoint = opaque;

    return endpoint->enabled;
}

static void endpoint_receive(void *opaque, const qemu_can_frame *frame)
{
    AdapterEndpoint *endpoint = opaque;

    endpoint->last = *frame;
    endpoint->received++;
}

static void test_broadcast_ack_and_no_loopback(void)
{
    AdapterEndpoint endpoint_a = { .enabled = true };
    AdapterEndpoint endpoint_b = { .enabled = true };
    DmCanBusAdapter adapter_a = { 0 };
    DmCanBusAdapter adapter_b = { 0 };
    qemu_can_frame frame = {
        .can_id = 0x123 | QEMU_CAN_EFF_FLAG,
        .can_dlc = 4,
        .flags = QEMU_CAN_FRMF_TYPE_FD | QEMU_CAN_FRMF_BRS,
        .data = { 1, 2, 3, 4 },
    };
    CanBusState *bus = CAN_BUS(object_new(TYPE_CAN_BUS));

    dm_can_bus_adapter_init(&adapter_a);
    dm_can_bus_adapter_init(&adapter_b);
    g_assert_true(dm_can_bus_adapter_connect(
        &adapter_a, bus, &endpoint_a, endpoint_can_receive,
        endpoint_receive));
    g_assert_true(dm_can_bus_adapter_connect(
        &adapter_b, bus, &endpoint_b, endpoint_can_receive,
        endpoint_receive));
    g_assert_false(dm_can_bus_adapter_connect(
        &adapter_a, bus, &endpoint_a, endpoint_can_receive,
        endpoint_receive));
    g_assert_cmpint(dm_can_bus_adapter_send(&adapter_a, &frame), ==, 1);
    g_assert_cmpuint(endpoint_a.received, ==, 0);
    g_assert_cmpuint(endpoint_b.received, ==, 1);
    g_assert_cmphex(endpoint_b.last.can_id, ==, frame.can_id);
    g_assert_cmpuint(endpoint_b.last.can_dlc, ==, frame.can_dlc);
    g_assert_cmpuint(endpoint_b.last.flags, ==, frame.flags);
    g_assert_cmpmem(endpoint_b.last.data, sizeof(frame.data),
                    frame.data, sizeof(frame.data));

    endpoint_b.enabled = false;
    g_assert_cmpint(dm_can_bus_adapter_send(&adapter_a, &frame), ==, 0);
    g_assert_cmpuint(endpoint_b.received, ==, 1);

    dm_can_bus_adapter_disconnect(&adapter_a);
    g_assert_cmpint(dm_can_bus_adapter_send(&adapter_a, &frame), ==, -1);
    dm_can_bus_adapter_disconnect(&adapter_b);
    object_unref(OBJECT(bus));
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    module_call_init(MODULE_INIT_QOM);
    g_test_add_func("/dm-can-bus-adapter/broadcast-ack-no-loopback",
                    test_broadcast_ack_and_no_loopback);
    return g_test_run();
}
