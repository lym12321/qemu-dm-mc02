/*
 * Tests for the board-independent USB endpoint packet queue.
 */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_endpoint_queue.h"

static DmUsbEndpointConfig
test_config(DmUsbEndpointDirection direction)
{
    return (DmUsbEndpointConfig) {
        .number = 3,
        .direction = direction,
        .type = DM_USB_ENDPOINT_BULK,
        .max_packet_size = 4,
        .capacity = 2,
    };
}

static void test_empty_and_invalid(void)
{
    DmUsbEndpointQueue queue = { 0 };
    DmUsbEndpointPacket packet;
    DmUsbEndpointConfig config = test_config(DM_USB_ENDPOINT_OUT);

    g_assert_cmpint(dm_usb_endpoint_queue_init(&queue, &config), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_peek(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_EMPTY);
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_EMPTY);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, NULL, 1, 0), ==,
                    DM_USB_ENDPOINT_QUEUE_INVALID);
    g_assert_cmpint(dm_usb_endpoint_queue_peek(&queue, NULL), ==,
                    DM_USB_ENDPOINT_QUEUE_INVALID);
    dm_usb_endpoint_queue_cleanup(&queue);

    config.max_packet_size = 0;
    g_assert_cmpint(dm_usb_endpoint_queue_init(&queue, &config), ==,
                    DM_USB_ENDPOINT_QUEUE_INVALID);
    g_assert_cmpint(dm_usb_endpoint_queue_init(NULL, &config), ==,
                    DM_USB_ENDPOINT_QUEUE_INVALID);
    g_assert_cmpint(dm_usb_endpoint_queue_init(&queue, NULL), ==,
                    DM_USB_ENDPOINT_QUEUE_INVALID);
}

static void test_zero_length_and_oversize(void)
{
    DmUsbEndpointQueue queue = { 0 };
    DmUsbEndpointPacket packet;
    static const guint8 data[] = { 1, 2, 3, 4, 5 };
    DmUsbEndpointConfig config = test_config(DM_USB_ENDPOINT_OUT);

    g_assert_cmpint(dm_usb_endpoint_queue_init(&queue, &config), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, NULL, 0, 17), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_peek(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpuint(packet.length, ==, 0);
    g_assert_cmpuint(packet.timestamp_ns, ==, 17);
    g_assert_nonnull(packet.data);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, data, sizeof(data), 18), ==,
                    DM_USB_ENDPOINT_QUEUE_OVERSIZE);
    dm_usb_endpoint_queue_cleanup(&queue);
}

static void test_full_and_fifo_order(void)
{
    DmUsbEndpointQueue queue = { 0 };
    DmUsbEndpointPacket packet;
    static const guint8 first[] = { 0xaa, 0xbb };
    static const guint8 second[] = { 0xcc, 0xdd, 0xee };
    static const guint8 third[] = { 0xf0 };
    DmUsbEndpointConfig config = test_config(DM_USB_ENDPOINT_OUT);

    g_assert_cmpint(dm_usb_endpoint_queue_init(&queue, &config), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, first, sizeof(first), 100), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, second, sizeof(second), 200), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, third, sizeof(third), 300), ==,
                    DM_USB_ENDPOINT_QUEUE_FULL);

    g_assert_cmpint(dm_usb_endpoint_queue_peek(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpmem(packet.data, packet.length, first, sizeof(first));
    g_assert_cmpuint(packet.timestamp_ns, ==, 100);
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpmem(packet.data, packet.length, first, sizeof(first));

    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, third, sizeof(third), 300), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpmem(packet.data, packet.length, second, sizeof(second));
    g_assert_cmpuint(packet.timestamp_ns, ==, 200);
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpmem(packet.data, packet.length, third, sizeof(third));
    g_assert_cmpuint(packet.timestamp_ns, ==, 300);
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_EMPTY);
    dm_usb_endpoint_queue_cleanup(&queue);
}

static void test_reset(void)
{
    DmUsbEndpointQueue queue = { 0 };
    DmUsbEndpointPacket packet;
    static const guint8 data[] = { 9, 8, 7, 6 };
    DmUsbEndpointConfig config = test_config(DM_USB_ENDPOINT_OUT);

    g_assert_cmpint(dm_usb_endpoint_queue_init(&queue, &config), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, data, sizeof(data), 42), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    dm_usb_endpoint_queue_reset(&queue);
    g_assert_cmpint(dm_usb_endpoint_queue_peek(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_EMPTY);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&queue, data, sizeof(data), 43), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&queue, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpuint(packet.timestamp_ns, ==, 43);
    dm_usb_endpoint_queue_cleanup(&queue);
}

static void test_in_out_are_independent(void)
{
    DmUsbEndpointQueue in = { 0 };
    DmUsbEndpointQueue out = { 0 };
    DmUsbEndpointPacket packet;
    static const guint8 in_data[] = { 1 };
    static const guint8 out_data[] = { 2 };

    g_assert_cmpint(dm_usb_endpoint_queue_init(&in,
                                                &(DmUsbEndpointConfig) {
                                                    .number = 1,
                                                    .direction = DM_USB_ENDPOINT_IN,
                                                    .type = DM_USB_ENDPOINT_INTERRUPT,
                                                    .max_packet_size = 1,
                                                    .capacity = 1,
                                                }), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_init(&out,
                                                &(DmUsbEndpointConfig) {
                                                    .number = 1,
                                                    .direction = DM_USB_ENDPOINT_OUT,
                                                    .type = DM_USB_ENDPOINT_INTERRUPT,
                                                    .max_packet_size = 1,
                                                    .capacity = 1,
                                                }), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(in.direction, ==, DM_USB_ENDPOINT_IN);
    g_assert_cmpint(out.direction, ==, DM_USB_ENDPOINT_OUT);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&in, in_data, sizeof(in_data), 1), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_enqueue(&out, out_data, sizeof(out_data), 2), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&in, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpmem(packet.data, packet.length, in_data, sizeof(in_data));
    g_assert_cmpint(dm_usb_endpoint_queue_dequeue(&out, &packet), ==,
                    DM_USB_ENDPOINT_QUEUE_ACCEPTED);
    g_assert_cmpmem(packet.data, packet.length, out_data, sizeof(out_data));
    dm_usb_endpoint_queue_cleanup(&in);
    dm_usb_endpoint_queue_cleanup(&out);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/endpoint-queue/empty-invalid", test_empty_and_invalid);
    g_test_add_func("/dm-usb/endpoint-queue/zero-length-oversize",
                    test_zero_length_and_oversize);
    g_test_add_func("/dm-usb/endpoint-queue/full-fifo", test_full_and_fifo_order);
    g_test_add_func("/dm-usb/endpoint-queue/reset", test_reset);
    g_test_add_func("/dm-usb/endpoint-queue/in-out-independent",
                    test_in_out_are_independent);
    return g_test_run();
}
