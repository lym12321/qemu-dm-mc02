/* Tests for the board-independent synthetic USB upstream host. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host.h"

typedef struct TestUsbHost {
    DmUsbControlDevice control;
    DmUsbTransactionDevice device;
    DmUsbHost host;
    uint8_t descriptor[18];
    uint8_t in_data[32];
    size_t in_length;
    size_t in_offset;
    uint8_t out_data[32];
    size_t out_length;
    uint8_t class_data[8];
    size_t class_length;
    unsigned class_calls;
    unsigned in_calls;
    unsigned out_calls;
    bool nak_once;
    uint64_t timestamp_ns;
    uint8_t address;
} TestUsbHost;

static bool test_get_descriptor(void *opaque, uint8_t type, uint8_t index,
                                const uint8_t **data, size_t *length)
{
    TestUsbHost *test = opaque;

    if (type != 1 || index != 0) {
        return false;
    }
    *data = test->descriptor;
    *length = sizeof(test->descriptor);
    return true;
}

static DmUsbControlResult test_class_request(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *in_length)
{
    TestUsbHost *test = opaque;

    ++test->class_calls;
    if (request->request != 0x20 || request->length != 7) {
        return DM_USB_CONTROL_STALL;
    }
    if (!out_data) {
        return DM_USB_CONTROL_ACCEPTED;
    }
    if (out_length != 7 || in_data || in_capacity) {
        return DM_USB_CONTROL_INVALID;
    }
    memcpy(test->class_data, out_data, out_length);
    test->class_length = out_length;
    return DM_USB_CONTROL_ACCEPTED;
}

static void test_set_address(void *opaque, uint8_t address)
{
    TestUsbHost *test = opaque;

    test->address = address;
}

static DmUsbTransactionStatus test_in(void *opaque, uint8_t endpoint,
                                      uint8_t *data, size_t capacity,
                                      size_t *length, uint64_t timestamp_ns)
{
    TestUsbHost *test = opaque;
    size_t packet_length;

    g_assert_cmpuint(timestamp_ns, ==, test->timestamp_ns);
    ++test->in_calls;
    if (endpoint != 1 || test->nak_once) {
        if (test->nak_once) {
            test->nak_once = false;
            return DM_USB_TRANSACTION_NAK;
        }
        return DM_USB_TRANSACTION_NAK;
    }
    if (test->in_offset == test->in_length) {
        return DM_USB_TRANSACTION_NAK;
    }
    packet_length = MIN(capacity, test->in_length - test->in_offset);
    memcpy(data, test->in_data + test->in_offset, packet_length);
    test->in_offset += packet_length;
    *length = packet_length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static DmUsbTransactionStatus test_out(void *opaque, uint8_t endpoint,
                                       const uint8_t *data, size_t length,
                                       uint64_t timestamp_ns)
{
    TestUsbHost *test = opaque;

    g_assert_cmpuint(timestamp_ns, ==, test->timestamp_ns);
    ++test->out_calls;
    if (endpoint != 1 || test->out_length + length > sizeof(test->out_data)) {
        return DM_USB_TRANSACTION_INVALID;
    }
    memcpy(test->out_data + test->out_length, data, length);
    test->out_length += length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static DmUsbTransactionResult test_submit(
    void *opaque, const DmUsbTransaction *transaction)
{
    TestUsbHost *test = opaque;

    return dm_usb_transaction_submit(&test->device, transaction);
}

static void test_init(TestUsbHost *test)
{
    static const DmUsbControlOps control_ops = {
        .get_descriptor = test_get_descriptor,
        .class_request = test_class_request,
        .set_address = test_set_address,
    };
    static const DmUsbTransactionOps transaction_ops = {
        .in = test_in,
        .out = test_out,
    };

    memset(test, 0, sizeof(*test));
    for (unsigned i = 0; i < sizeof(test->descriptor); ++i) {
        test->descriptor[i] = (uint8_t)(i + 1);
    }
    memcpy(test->in_data, "ABCDEFG", 7);
    test->in_length = 7;
    test->timestamp_ns = 987654;
    dm_usb_control_init(&test->control, &control_ops, test, 4);
    dm_usb_transaction_init(&test->device, &test->control, &transaction_ops,
                            test, 4);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        &test->device, 1, DM_USB_ENDPOINT_IN,
                        DM_USB_ENDPOINT_BULK, 4, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        &test->device, 1, DM_USB_ENDPOINT_OUT,
                        DM_USB_ENDPOINT_BULK, 4, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    dm_usb_host_init(&test->host, test_submit, test, 4);
}

static void test_control_in_and_status(void)
{
    static const uint8_t setup[] = { 0x80, 6, 0, 1, 0, 0, 18, 0 };
    TestUsbHost test;
    uint8_t data[18];
    DmUsbHostResult result;

    test_init(&test);
    result = dm_usb_host_control_transfer(&test.host, setup, NULL, 0, data,
                                          sizeof(data), test.timestamp_ns);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(data));
    g_assert_cmpuint(result.transactions, ==, 7);
    g_assert_cmpmem(data, sizeof(data), test.descriptor,
                    sizeof(test.descriptor));
    g_assert_cmpint(dm_usb_control_phase(&test.control), ==,
                    DM_USB_CONTROL_IDLE);
}

static void test_control_out_packetization(void)
{
    static const uint8_t setup[] = { 0x21, 0x20, 0, 0, 0, 0, 7, 0 };
    static const uint8_t set_address[] = { 0, 5, 42, 0, 0, 0, 0, 0 };
    static const uint8_t data[] = { 1, 2, 3, 4, 5, 6, 7 };
    TestUsbHost test;
    DmUsbHostResult result;

    test_init(&test);
    result = dm_usb_host_control_transfer(&test.host, setup, data,
                                          sizeof(data), NULL, 0,
                                          test.timestamp_ns);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(data));
    g_assert_cmpuint(result.transactions, ==, 4);
    g_assert_cmpuint(test.class_calls, ==, 2);
    g_assert_cmpmem(test.class_data, test.class_length, data, sizeof(data));

    result = dm_usb_host_control_transfer(&test.host, set_address, NULL, 0,
                                          NULL, 0, test.timestamp_ns);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 0);
    g_assert_cmpuint(result.transactions, ==, 2);
    g_assert_cmpuint(test.address, ==, 42);
}

static void test_bulk_nak_and_packetization(void)
{
    static const uint8_t out_data[] = { 9, 8, 7, 6, 5 };
    TestUsbHost test;
    uint8_t in_data[8];
    DmUsbHostResult result;

    test_init(&test);
    test.nak_once = true;
    result = dm_usb_host_bulk_in(&test.host, 1, 4, in_data, sizeof(in_data),
                                 test.timestamp_ns);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NAK);
    g_assert_cmpuint(result.actual_length, ==, 0);
    g_assert_cmpuint(result.transactions, ==, 1);
    g_assert_cmpint(dm_usb_transaction_next_pid(
                        &test.device, 1, DM_USB_ENDPOINT_IN), ==,
                    DM_USB_TRANSACTION_PID_DATA0);

    result = dm_usb_host_bulk_in(&test.host, 1, 4, in_data, sizeof(in_data),
                                 test.timestamp_ns);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 7);
    g_assert_cmpuint(result.transactions, ==, 2);
    g_assert_cmpmem(in_data, 7, test.in_data, 7);

    result = dm_usb_host_bulk_out(&test.host, 1, 4, out_data,
                                  sizeof(out_data), test.timestamp_ns);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(out_data));
    g_assert_cmpuint(result.transactions, ==, 2);
    g_assert_cmpmem(test.out_data, test.out_length, out_data,
                    sizeof(out_data));
}

static void test_invalid_arguments_do_not_submit(void)
{
    static const uint8_t setup[] = { 0x80, 6, 0, 1, 0, 0, 4, 0 };
    TestUsbHost test;
    uint8_t data[2];
    DmUsbHostResult result;

    test_init(&test);
    result = dm_usb_host_control_transfer(&test.host, setup, NULL, 0, data,
                                          sizeof(data), test.timestamp_ns);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    g_assert_cmpuint(result.transactions, ==, 0);
    g_assert_cmpuint(test.in_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/host/control-in-status",
                    test_control_in_and_status);
    g_test_add_func("/dm-usb/host/control-out-packetization",
                    test_control_out_packetization);
    g_test_add_func("/dm-usb/host/bulk-nak-packetization",
                    test_bulk_nak_and_packetization);
    g_test_add_func("/dm-usb/host/invalid-arguments",
                    test_invalid_arguments_do_not_submit);
    return g_test_run();
}
