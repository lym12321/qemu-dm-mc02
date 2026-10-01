/* Tests for synchronous control transfers driven through H723 host channels. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_channel_control.h"
#include "hw/usb/dm_usb_host_channel_transport.h"

typedef struct ControlFixture {
    DmStm32H7OtgHost host;
    DmUsbHostChannelTransport transport;
    DmUsbHostChannelControl control;
    DmUsbControlDevice device_control;
    DmUsbTransactionDevice device;
    uint8_t descriptor[80];
    uint8_t class_data[64];
    size_t class_data_length;
    uint8_t routed_address[16];
    unsigned route_calls;
} ControlFixture;

static bool fixture_get_descriptor(void *opaque, uint8_t type, uint8_t index,
                                   const uint8_t **data, size_t *length)
{
    ControlFixture *fixture = opaque;

    if (type != 1 || index != 0) {
        return false;
    }
    *data = fixture->descriptor;
    *length = sizeof(fixture->descriptor);
    return true;
}

static DmUsbControlResult fixture_class_request(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *in_length)
{
    ControlFixture *fixture = opaque;

    if (request->request_type != 0x21 || request->request != 0x20 ||
        request->length != 4) {
        return DM_USB_CONTROL_STALL;
    }
    if (!out_data) {
        return DM_USB_CONTROL_ACCEPTED;
    }
    if (out_length != request->length || in_data || in_capacity || in_length) {
        return DM_USB_CONTROL_INVALID;
    }
    memcpy(fixture->class_data, out_data, out_length);
    fixture->class_data_length = out_length;
    return DM_USB_CONTROL_ACCEPTED;
}

static DmUsbTransactionResult fixture_route(
    void *opaque, uint8_t device_address,
    const DmUsbTransaction *transaction)
{
    ControlFixture *fixture = opaque;

    g_assert_cmpuint(fixture->route_calls,
                     <, G_N_ELEMENTS(fixture->routed_address));
    fixture->routed_address[fixture->route_calls++] = device_address;
    return dm_usb_transaction_submit(&fixture->device, transaction);
}

static void fixture_init(ControlFixture *fixture)
{
    static const DmUsbControlOps control_ops = {
        .get_descriptor = fixture_get_descriptor,
        .class_request = fixture_class_request,
    };

    memset(fixture, 0, sizeof(*fixture));
    for (unsigned index = 0; index < sizeof(fixture->descriptor); ++index) {
        fixture->descriptor[index] = index;
    }
    dm_usb_control_init(&fixture->device_control, &control_ops, fixture, 64);
    dm_usb_transaction_init(&fixture->device, &fixture->device_control,
                            NULL, NULL, 64);
    dm_stm32h7_otg_host_init(&fixture->host, NULL, NULL, NULL, NULL);
    dm_usb_host_channel_transport_init_with_opaques(
        &fixture->transport, &fixture->host, NULL, NULL,
        dm_stm32h7_otg_host_read_out_fifo, &fixture->host,
        dm_stm32h7_otg_host_write_in_fifo, &fixture->host);
    dm_usb_host_channel_transport_set_route(&fixture->transport,
                                            fixture_route, fixture);
    dm_stm32h7_otg_host_set_port_connected(&fixture->host, true,
                                            DM_STM32H7_OTG_PORT_FULL_SPEED);
    dm_stm32h7_otg_host_write(&fixture->host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 10);
    dm_stm32h7_otg_host_write(&fixture->host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 20);
    dm_usb_host_channel_control_init(&fixture->control, &fixture->host, 0,
                                     64);
}

static void test_control_in_packets_and_status(void)
{
    static const uint8_t get_descriptor[] = {
        0x80, 0x06, 0, 1, 0, 0, 80, 0,
    };
    ControlFixture fixture;
    DmUsbHostChannelControlResult result;
    uint8_t descriptor[80];

    fixture_init(&fixture);
    result = dm_usb_host_channel_control_transfer(
        &fixture.control, 0, get_descriptor, NULL, 0, descriptor,
        sizeof(descriptor), 100);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(descriptor));
    g_assert_cmpuint(result.transactions, ==, 4);
    g_assert_cmpmem(descriptor, sizeof(descriptor), fixture.descriptor,
                    sizeof(fixture.descriptor));
    g_assert_cmpuint(fixture.route_calls, ==, 4);
    for (unsigned index = 0; index < fixture.route_calls; ++index) {
        g_assert_cmpuint(fixture.routed_address[index], ==, 0);
    }
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_set_address_transitions_route_after_status(void)
{
    static const uint8_t set_address[] = {
        0x00, 0x05, 13, 0, 0, 0, 0, 0,
    };
    static const uint8_t get_status[] = {
        0x80, 0x00, 0, 0, 0, 0, 2, 0,
    };
    ControlFixture fixture;
    DmUsbHostChannelControlResult result;
    uint8_t status[2] = { 0xff, 0xff };

    fixture_init(&fixture);
    result = dm_usb_host_channel_control_transfer(
        &fixture.control, 0, set_address, NULL, 0, NULL, 0, 100);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 0);
    g_assert_cmpuint(result.transactions, ==, 2);
    g_assert_cmpuint(dm_usb_control_address(&fixture.device_control), ==, 13);
    g_assert_cmpuint(fixture.route_calls, ==, 2);
    g_assert_cmpuint(fixture.routed_address[0], ==, 0);
    g_assert_cmpuint(fixture.routed_address[1], ==, 0);

    result = dm_usb_host_channel_control_transfer(
        &fixture.control, 13, get_status, NULL, 0, status, sizeof(status),
        200);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(status));
    g_assert_cmpuint(result.transactions, ==, 3);
    g_assert_cmpmem(status, sizeof(status), "\0\0", sizeof(status));
    g_assert_cmpuint(fixture.route_calls, ==, 5);
    for (unsigned index = 2; index < fixture.route_calls; ++index) {
        g_assert_cmpuint(fixture.routed_address[index], ==, 13);
    }
}

static void test_control_out_data_and_status(void)
{
    static const uint8_t class_request[] = {
        0x21, 0x20, 0, 0, 0, 0, 4, 0,
    };
    ControlFixture fixture;
    DmUsbHostChannelControlResult result;

    fixture_init(&fixture);
    result = dm_usb_host_channel_control_transfer(
        &fixture.control, 0, class_request, (const uint8_t *)"DATA", 4,
        NULL, 0, 100);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 4);
    g_assert_cmpuint(result.transactions, ==, 3);
    g_assert_cmpuint(fixture.class_data_length, ==, 4);
    g_assert_cmpmem(fixture.class_data, fixture.class_data_length, "DATA", 4);
}

static void test_out_of_range_address_is_rejected(void)
{
    static const uint8_t get_status[] = {
        0x80, 0x00, 0, 0, 0, 0, 2, 0,
    };
    ControlFixture fixture;
    DmUsbHostChannelControlResult result;
    uint8_t status[2];

    fixture_init(&fixture);
    result = dm_usb_host_channel_control_transfer(
        &fixture.control, 128, get_status, NULL, 0, status, sizeof(status),
        100);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    g_assert_cmpuint(result.transactions, ==, 0);
    g_assert_cmpuint(fixture.route_calls, ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCCHAR(0)), ==, 0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/host-channel-control/control-in-packets-status",
                    test_control_in_packets_and_status);
    g_test_add_func("/dm-usb/host-channel-control/set-address-route-after-status",
                    test_set_address_transitions_route_after_status);
    g_test_add_func("/dm-usb/host-channel-control/control-out-data-status",
                    test_control_out_data_and_status);
    g_test_add_func("/dm-usb/host-channel-control/out-of-range-address",
                    test_out_of_range_address_is_rejected);
    return g_test_run();
}
