/* Tests for the board-independent USB device transaction dispatcher. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_transaction.h"

typedef struct TestUsbTransaction {
    uint8_t in_data[8];
    size_t in_length;
    uint8_t out_data[8];
    size_t out_length;
    unsigned in_calls;
    unsigned out_calls;
    bool in_nak;
    bool out_stall;
} TestUsbTransaction;

static DmUsbTransactionStatus test_in(void *opaque, uint8_t endpoint,
                                      uint8_t *data, size_t capacity,
                                      size_t *length, uint64_t timestamp_ns)
{
    TestUsbTransaction *test = opaque;

    g_assert_cmpuint(timestamp_ns, ==, 1234);
    g_assert_cmpuint(endpoint, ==, 1);
    test->in_calls++;
    if (test->in_nak) {
        return DM_USB_TRANSACTION_NAK;
    }
    if (capacity < test->in_length) {
        return DM_USB_TRANSACTION_INVALID;
    }
    memcpy(data, test->in_data, test->in_length);
    *length = test->in_length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static DmUsbTransactionStatus test_out(void *opaque, uint8_t endpoint,
                                       const uint8_t *data, size_t length,
                                       uint64_t timestamp_ns)
{
    TestUsbTransaction *test = opaque;

    g_assert_cmpuint(timestamp_ns, ==, 1234);
    g_assert_cmpuint(endpoint, ==, 1);
    test->out_calls++;
    if (test->out_stall) {
        return DM_USB_TRANSACTION_STALL;
    }
    g_assert_cmpuint(length, <=, sizeof(test->out_data));
    memcpy(test->out_data, data, length);
    test->out_length = length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static void test_device(DmUsbTransactionDevice *device,
                        DmUsbControlDevice *control,
                        TestUsbTransaction *test)
{
    static const DmUsbTransactionOps ops = {
        .in = test_in,
        .out = test_out,
    };

    memset(test, 0, sizeof(*test));
    memcpy(test->in_data, "IN", 2);
    test->in_length = 2;
    dm_usb_control_init(control, NULL, NULL, 8);
    dm_usb_transaction_init(device, control, &ops, test, 8);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        device, 1, DM_USB_ENDPOINT_IN,
                        DM_USB_ENDPOINT_BULK, 4, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        device, 1, DM_USB_ENDPOINT_OUT,
                        DM_USB_ENDPOINT_BULK, 4, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
}

static DmUsbTransaction transaction_out(uint8_t endpoint,
                                        const uint8_t *data, size_t length)
{
    return (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_OUT,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = endpoint,
        .out_data = data,
        .length = length,
    };
}

static DmUsbTransaction transaction_in(uint8_t endpoint, uint8_t *data,
                                       size_t capacity)
{
    return (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_IN,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = endpoint,
        .in_data = data,
        .capacity = capacity,
    };
}

static void test_data_callbacks_and_toggles(void)
{
    DmUsbTransactionDevice device;
    DmUsbControlDevice control;
    TestUsbTransaction test;
    uint8_t in_data[4];
    DmUsbTransaction request;
    DmUsbTransactionResult result;

    test_device(&device, &control, &test);
    request = transaction_out(1, (const uint8_t *)"OUT", 3);
    request.timestamp_ns = 1234;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 3);
    g_assert_cmpint(result.next_pid, ==, DM_USB_TRANSACTION_PID_DATA1);
    g_assert_cmpmem(test.out_data, test.out_length, "OUT", 3);

    request = transaction_out(1, (const uint8_t *)"!", 1);
    request.timestamp_ns = 1234;
    request.pid = DM_USB_TRANSACTION_PID_DATA1;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(result.next_pid, ==, DM_USB_TRANSACTION_PID_DATA0);

    request = transaction_in(1, in_data, sizeof(in_data));
    request.timestamp_ns = 1234;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 2);
    g_assert_cmpmem(in_data, 2, "IN", 2);
    g_assert_cmpint(result.next_pid, ==, DM_USB_TRANSACTION_PID_DATA1);

    test.in_nak = true;
    request = transaction_in(1, in_data, sizeof(in_data));
    request.timestamp_ns = 1234;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NAK);
    g_assert_cmpint(result.next_pid, ==, DM_USB_TRANSACTION_PID_DATA1);
}

static void test_stall_and_clear_halt(void)
{
    DmUsbTransactionDevice device;
    DmUsbControlDevice control;
    TestUsbTransaction test;
    DmUsbTransaction request;
    DmUsbTransactionResult result;

    test_device(&device, &control, &test);
    test.out_stall = true;
    request = transaction_out(1, (const uint8_t *)"x", 1);
    request.timestamp_ns = 1234;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_STALL);
    g_assert_true(dm_usb_transaction_halted(&device, 1,
                                            DM_USB_ENDPOINT_OUT));

    test.out_stall = false;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_STALL);
    g_assert_cmpint(dm_usb_transaction_clear_halt(
                        &device, 1, DM_USB_ENDPOINT_OUT), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_transaction_next_pid(
                        &device, 1, DM_USB_ENDPOINT_OUT), ==,
                    DM_USB_TRANSACTION_PID_DATA0);
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
}

static void test_control_transfer_and_status_pid(void)
{
    static const uint8_t set_address[] = {
        0, 5, 42, 0, 0, 0, 0, 0,
    };
    static const uint8_t get_status[] = {
        0x80, 0, 0, 0, 0, 0, 2, 0,
    };
    static const uint8_t unsupported[] = {
        0x80, 0xff, 0, 0, 0, 0, 0, 0,
    };
    DmUsbTransactionDevice device;
    DmUsbControlDevice control;
    TestUsbTransaction test;
    DmUsbTransaction request;
    DmUsbTransactionResult result;
    uint8_t packet[8];

    test_device(&device, &control, &test);
    request = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_SETUP,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .out_data = unsupported,
        .length = sizeof(unsupported),
    };
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_STALL);
    g_assert_true(dm_usb_transaction_halted(&device, 0,
                                            DM_USB_ENDPOINT_IN));
    g_assert_true(dm_usb_transaction_halted(&device, 0,
                                            DM_USB_ENDPOINT_OUT));
    request = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_SETUP,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .out_data = set_address,
        .length = sizeof(set_address),
    };
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_false(dm_usb_transaction_halted(&device, 0, DM_USB_ENDPOINT_IN));
    g_assert_false(dm_usb_transaction_halted(&device, 0, DM_USB_ENDPOINT_OUT));
    g_assert_cmpuint(dm_usb_control_address(&control), ==, 0);
    request = transaction_in(0, packet, sizeof(packet));
    request.pid = DM_USB_TRANSACTION_PID_DATA1;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(dm_usb_control_address(&control), ==, 42);

    request = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_SETUP,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .out_data = get_status,
        .length = sizeof(get_status),
    };
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    request = transaction_in(0, packet, sizeof(packet));
    request.pid = DM_USB_TRANSACTION_PID_DATA1;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 2);
    request = transaction_out(0, NULL, 0);
    request.pid = DM_USB_TRANSACTION_PID_DATA1;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
}

static void test_disabled_and_invalid_packets(void)
{
    DmUsbTransactionDevice device;
    DmUsbControlDevice control;
    TestUsbTransaction test;
    DmUsbTransaction request;
    DmUsbTransactionResult result;
    uint8_t data[4];

    test_device(&device, &control, &test);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        &device, 2, DM_USB_ENDPOINT_IN,
                        DM_USB_ENDPOINT_BULK, 4, false), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    request = transaction_in(2, data, sizeof(data));
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NAK);

    request = transaction_out(1, data, 5);
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    g_assert_cmpuint(test.out_calls, ==, 0);

    request = transaction_in(1, NULL, sizeof(data));
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);

    request = transaction_in(1, data, sizeof(data));
    request.length = 1;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
}

static void test_reset_restores_toggle_and_halt(void)
{
    DmUsbTransactionDevice device;
    DmUsbControlDevice control;
    TestUsbTransaction test;
    DmUsbTransaction request;
    DmUsbTransactionResult result;

    test_device(&device, &control, &test);
    request = transaction_out(1, (const uint8_t *)"x", 1);
    request.timestamp_ns = 1234;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.next_pid, ==, DM_USB_TRANSACTION_PID_DATA1);
    test.out_stall = true;
    result = dm_usb_transaction_submit(&device, &request);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_STALL);
    dm_usb_transaction_reset(&device);
    g_assert_false(dm_usb_transaction_halted(&device, 1,
                                             DM_USB_ENDPOINT_OUT));
    g_assert_cmpint(dm_usb_transaction_next_pid(
                        &device, 1, DM_USB_ENDPOINT_OUT), ==,
                    DM_USB_TRANSACTION_PID_DATA0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/transaction/data-callbacks",
                    test_data_callbacks_and_toggles);
    g_test_add_func("/dm-usb/transaction/stall-clear-halt",
                    test_stall_and_clear_halt);
    g_test_add_func("/dm-usb/transaction/control-status-pid",
                    test_control_transfer_and_status_pid);
    g_test_add_func("/dm-usb/transaction/disabled-invalid",
                    test_disabled_and_invalid_packets);
    g_test_add_func("/dm-usb/transaction/reset-state",
                    test_reset_restores_toggle_and_halt);
    return g_test_run();
}
