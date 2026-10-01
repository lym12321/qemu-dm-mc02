/* Tests for the board-independent USB control-transfer core. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_control.h"

typedef struct TestUsb {
    uint8_t descriptor[16];
    uint8_t class_data[4];
    uint8_t address;
    uint8_t configuration;
    unsigned address_updates;
    unsigned configuration_updates;
    unsigned class_calls;
} TestUsb;

static bool test_descriptor(void *opaque, uint8_t type, uint8_t index,
                            const uint8_t **data, size_t *length)
{
    TestUsb *test = opaque;

    if (type != 1 || index != 0) {
        return false;
    }
    *data = test->descriptor;
    *length = sizeof(test->descriptor);
    return true;
}

static DmUsbControlResult test_class_request(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, size_t *in_length)
{
    TestUsb *test = opaque;

    test->class_calls++;
    if (request->request == 0x21 && !(request->request_type & 0x80) &&
        out_length == 0) {
        return DM_USB_CONTROL_ACCEPTED;
    }
    if (request->request == 0x21 && !(request->request_type & 0x80) &&
        out_length == sizeof(test->class_data)) {
        g_assert_cmpmem(out_data, out_length, test->class_data,
                        sizeof(test->class_data));
        return DM_USB_CONTROL_ACCEPTED;
    }
    if (request->request == 0x20 && (request->request_type & 0x80) &&
        in_capacity >= sizeof(test->class_data)) {
        memcpy(in_data, test->class_data, sizeof(test->class_data));
        *in_length = sizeof(test->class_data);
        return DM_USB_CONTROL_ACCEPTED;
    }
    return DM_USB_CONTROL_STALL;
}

static void test_set_address(void *opaque, uint8_t address)
{
    TestUsb *test = opaque;

    test->address = address;
    ++test->address_updates;
}

static void test_set_configuration(void *opaque, uint8_t configuration)
{
    TestUsb *test = opaque;

    test->configuration = configuration;
    ++test->configuration_updates;
}

static DmUsbControlDevice *test_device(DmUsbControlDevice *device,
                                       TestUsb *test)
{
    static const DmUsbControlOps ops = {
        .get_descriptor = test_descriptor,
        .class_request = test_class_request,
        .set_address = test_set_address,
        .set_configuration = test_set_configuration,
    };

    memset(test, 0, sizeof(*test));
    for (unsigned i = 0; i < sizeof(test->descriptor); ++i) {
        test->descriptor[i] = i + 1;
    }
    memcpy(test->class_data, "TEST", sizeof(test->class_data));
    dm_usb_control_init(device, &ops, test, 8);
    return device;
}

static void test_descriptor_packets_and_status(void)
{
    DmUsbControlDevice device;
    TestUsb test;
    uint8_t setup[] = { 0x80, 6, 0, 1, 0, 0, 32, 0 };
    uint8_t packet[8];
    size_t length;

    test_device(&device, &test);
    g_assert_cmpint(dm_usb_control_setup(&device, setup), ==,
                    DM_USB_CONTROL_ACCEPTED);
    for (unsigned i = 0; i < 2; ++i) {
        g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                          &length), ==,
                        DM_USB_CONTROL_ACCEPTED);
        g_assert_cmpuint(length, ==, sizeof(packet));
        g_assert_cmpuint(packet[0], ==, i * 8 + 1);
    }
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(length, ==, 0);
    g_assert_cmpint(dm_usb_control_phase(&device), ==,
                    DM_USB_CONTROL_STATUS_OUT);
    g_assert_cmpint(dm_usb_control_out(&device, NULL, 0), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_phase(&device), ==,
                    DM_USB_CONTROL_IDLE);
}

static void test_address_and_configuration_commit_after_status(void)
{
    DmUsbControlDevice device;
    TestUsb test;
    uint8_t set_address[] = { 0, 5, 42, 0, 0, 0, 0, 0 };
    uint8_t set_configuration[] = { 0, 9, 3, 0, 0, 0, 0, 0 };
    uint8_t packet[8];
    size_t length;

    test_device(&device, &test);
    g_assert_cmpint(dm_usb_control_setup(&device, set_address), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(dm_usb_control_address(&device), ==, 0);
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(length, ==, 0);
    g_assert_cmpuint(dm_usb_control_address(&device), ==, 42);
    g_assert_cmpuint(test.address, ==, 42);

    g_assert_cmpint(dm_usb_control_setup(&device, set_configuration), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(dm_usb_control_configuration(&device), ==, 3);
    g_assert_cmpuint(test.configuration, ==, 3);
}

static void test_class_data_in_and_out(void)
{
    DmUsbControlDevice device;
    TestUsb test;
    uint8_t out_setup[] = { 0x21, 0x21, 0, 0, 0, 0, 4, 0 };
    uint8_t in_setup[] = { 0xa1, 0x20, 0, 0, 0, 0, 4, 0 };
    uint8_t packet[8];
    size_t length;

    test_device(&device, &test);
    g_assert_cmpint(dm_usb_control_setup(&device, out_setup), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_out(&device, (uint8_t *)"TEST", 4), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(length, ==, 0);
    g_assert_cmpuint(test.class_calls, ==, 2);

    g_assert_cmpint(dm_usb_control_setup(&device, in_setup), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpmem(packet, length, "TEST", 4);
}

static void test_class_data_out_packet_boundary(void)
{
    DmUsbControlDevice device;
    TestUsb test;
    uint8_t setup[] = { 0x21, 0x21, 0, 0, 0, 0, 4, 0 };
    uint8_t packet[8];
    size_t length;

    test_device(&device, &test);
    g_assert_cmpint(dm_usb_control_setup(&device, setup), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_out(&device, (const uint8_t *)"TE", 2),
                    ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_phase(&device), ==,
                    DM_USB_CONTROL_DATA_OUT);
    g_assert_cmpint(dm_usb_control_out(&device, (const uint8_t *)"ST", 2),
                    ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_phase(&device), ==,
                    DM_USB_CONTROL_STATUS_IN);
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(length, ==, 0);
    g_assert_cmpuint(test.class_calls, ==, 2);
}

static void test_unsupported_request_stalls(void)
{
    DmUsbControlDevice device;
    TestUsb test;
    uint8_t setup[] = { 0x80, 0xff, 0, 0, 0, 0, 0, 0 };

    test_device(&device, &test);
    g_assert_cmpint(dm_usb_control_setup(&device, setup), ==,
                    DM_USB_CONTROL_STALL);
    g_assert_cmpint(dm_usb_control_phase(&device), ==,
                    DM_USB_CONTROL_STALLED);
}

static void test_bus_reset_clears_state_and_notifies_consumer(void)
{
    DmUsbControlDevice device;
    TestUsb test;
    uint8_t set_address[] = { 0, 5, 42, 0, 0, 0, 0, 0 };
    uint8_t set_configuration[] = { 0, 9, 3, 0, 0, 0, 0, 0 };
    uint8_t descriptor[] = { 0x80, 6, 0, 1, 0, 0, 16, 0 };
    uint8_t packet[8];
    size_t length;

    test_device(&device, &test);
    g_assert_cmpint(dm_usb_control_setup(&device, set_address), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_setup(&device, set_configuration), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_in(&device, packet, sizeof(packet),
                                      &length), ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(dm_usb_control_address(&device), ==, 42);
    g_assert_cmpuint(dm_usb_control_configuration(&device), ==, 3);

    g_assert_cmpint(dm_usb_control_setup(&device, descriptor), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_phase(&device), ==,
                    DM_USB_CONTROL_DATA_IN);
    dm_usb_control_bus_reset(&device);
    g_assert_cmpint(dm_usb_control_phase(&device), ==, DM_USB_CONTROL_IDLE);
    g_assert_cmpuint(dm_usb_control_address(&device), ==, 0);
    g_assert_cmpuint(dm_usb_control_configuration(&device), ==, 0);
    g_assert_cmpuint(test.address, ==, 0);
    g_assert_cmpuint(test.configuration, ==, 0);
    g_assert_cmpuint(test.address_updates, ==, 2);
    g_assert_cmpuint(test.configuration_updates, ==, 2);
}

static void test_request_level_execute(void)
{
    DmUsbControlDevice device;
    TestUsb test;
    DmUsbControlRequest descriptor = {
        .request_type = 0x80,
        .request = 6,
        .value = 1 << 8,
        .length = sizeof(test.descriptor),
    };
    DmUsbControlRequest set_address = {
        .request_type = 0,
        .request = 5,
        .value = 42,
    };
    DmUsbControlRequest class_out = {
        .request_type = 0x21,
        .request = 0x21,
        .length = sizeof(test.class_data),
    };
    uint8_t input[sizeof(test.descriptor)] = { 0 };
    size_t actual_length = SIZE_MAX;

    test_device(&device, &test);
    g_assert_cmpint(dm_usb_control_execute_request(
                        &device, &descriptor, NULL, 0, input, sizeof(input),
                        &actual_length), ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(actual_length, ==, sizeof(input));
    g_assert_cmpmem(input, sizeof(input), test.descriptor, sizeof(input));
    g_assert_cmpint(dm_usb_control_phase(&device), ==, DM_USB_CONTROL_IDLE);

    g_assert_cmpint(dm_usb_control_execute_request(
                        &device, &set_address, NULL, 0, NULL, 0,
                        &actual_length), ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(actual_length, ==, 0);
    g_assert_cmpuint(dm_usb_control_address(&device), ==, 42);
    g_assert_cmpuint(test.address, ==, 42);

    g_assert_cmpint(dm_usb_control_execute_request(
                        &device, &class_out, (const uint8_t *)"TEST", 4,
                        NULL, 0, &actual_length), ==, DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(actual_length, ==, 0);
    g_assert_cmpuint(test.class_calls, ==, 2);
    g_assert_cmpint(dm_usb_control_phase(&device), ==, DM_USB_CONTROL_IDLE);

    actual_length = SIZE_MAX;
    g_assert_cmpint(dm_usb_control_execute_request(
                        &device, &class_out, (const uint8_t *)"TE", 2,
                        NULL, 0, &actual_length), ==, DM_USB_CONTROL_INVALID);
    g_assert_cmpuint(actual_length, ==, 0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/control/descriptor-packets",
                    test_descriptor_packets_and_status);
    g_test_add_func("/dm-usb/control/address-configuration",
                    test_address_and_configuration_commit_after_status);
    g_test_add_func("/dm-usb/control/class-data",
                    test_class_data_in_and_out);
    g_test_add_func("/dm-usb/control/class-data-boundary",
                    test_class_data_out_packet_boundary);
    g_test_add_func("/dm-usb/control/unsupported-stalls",
                    test_unsupported_request_stalls);
    g_test_add_func("/dm-usb/control/bus-reset",
                    test_bus_reset_clears_state_and_notifies_consumer);
    g_test_add_func("/dm-usb/control/request-level-execute",
                    test_request_level_execute);
    return g_test_run();
}
