/* Tests for the synchronous QEMU USB device transaction adapter. */
#include "qemu/osdep.h"
#include "migration/vmstate.h"
#include "monitor/monitor.h"
#include "qapi/error.h"
#include "hw/qdev-properties.h"
#include "hw/usb/dm_usb_dwc2_device.h"
#include "hw/usb/dm_stm32h7_otg_host.h"
#include "hw/usb/dm_usb_host_channel_control.h"
#include "hw/usb/dm_usb_host_channel_transport.h"
#include "hw/usb/dm_usb_host_qemu_port.h"
#include "hw/usb/dm_usb_host_qemu_transport.h"
#include "hw/usb/dm_usb_qemu_adapter.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qom/object.h"

/* A concrete owner is required for the real QOM USB bus fixture. */
#define TYPE_ADAPTER_TEST_USB_HOST "dm-usb-qemu-adapter-test-host"

typedef struct AdapterTestUsbHost {
    DeviceState parent_obj;
} AdapterTestUsbHost;

static const TypeInfo adapter_test_usb_host_type_info = {
    .name = TYPE_ADAPTER_TEST_USB_HOST,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(AdapterTestUsbHost),
};

#define TYPE_ADAPTER_TEST_ASYNC_USB_DEVICE \
    "dm-usb-qemu-adapter-test-async-device"
OBJECT_DECLARE_SIMPLE_TYPE(AdapterTestAsyncUsbDevice,
                           ADAPTER_TEST_ASYNC_USB_DEVICE)

struct AdapterTestAsyncUsbDevice {
    USBDevice parent_obj;
    unsigned cancel_calls;
    int packet_status;
};

static void adapter_test_async_usb_realize(USBDevice *dev, Error **errp)
{
    AdapterTestAsyncUsbDevice *async =
        ADAPTER_TEST_ASYNC_USB_DEVICE(dev);

    (void)errp;
    async->packet_status = USB_RET_ASYNC;
    dev->speedmask = USB_SPEED_MASK_FULL;
    dev->ep_in[0].type = USB_ENDPOINT_XFER_BULK;
    dev->ep_in[0].max_packet_size = 64;
    dev->ep_out[0].type = USB_ENDPOINT_XFER_BULK;
    dev->ep_out[0].max_packet_size = 64;
}

static void adapter_test_async_usb_handle_data(USBDevice *dev, USBPacket *packet)
{
    AdapterTestAsyncUsbDevice *async =
        ADAPTER_TEST_ASYNC_USB_DEVICE(dev);

    packet->status = async->packet_status;
}

static void adapter_test_async_usb_cancel_packet(USBDevice *dev,
                                                  USBPacket *packet)
{
    AdapterTestAsyncUsbDevice *async =
        ADAPTER_TEST_ASYNC_USB_DEVICE(dev);

    (void)packet;
    ++async->cancel_calls;
}

static void adapter_test_async_usb_class_init(ObjectClass *klass, void *data)
{
    USBDeviceClass *uc = USB_DEVICE_CLASS(klass);

    (void)data;
    uc->realize = adapter_test_async_usb_realize;
    uc->handle_data = adapter_test_async_usb_handle_data;
    uc->cancel_packet = adapter_test_async_usb_cancel_packet;
    uc->product_desc = "DM USB QEMU Adapter Async Test Device";
}

static const TypeInfo adapter_test_async_usb_device_type_info = {
    .name = TYPE_ADAPTER_TEST_ASYNC_USB_DEVICE,
    .parent = TYPE_USB_DEVICE,
    .instance_size = sizeof(AdapterTestAsyncUsbDevice),
    .class_init = adapter_test_async_usb_class_init,
};

typedef struct AdapterFixture {
    DmUsbControlDevice control;
    DmUsbTransactionDevice target;
    DmUsbDwc2Device dwc2;
    DmStm32H7OtgHost host_controller;
    DmUsbHostChannelTransport host_channel_transport;
    DmUsbHostQemuPort host_qemu_port;
    DmUsbHostQemuTransport host_qemu_transport;
    DmUsbQemuAdapter *adapter;
    DeviceState *host;
    USBBus bus;
    USBPort port;
    uint8_t descriptor[80];
    uint8_t bulk_in[8];
    size_t bulk_in_length;
    uint8_t bulk_out[64];
    size_t bulk_out_length;
    uint8_t class_data[64];
    size_t class_data_length;
    unsigned control_calls;
    uint64_t last_control_timestamp_ns;
    DmUsbTransactionStatus forced_status;
    unsigned submit_calls;
    unsigned reset_calls;
    unsigned attach_calls;
    unsigned detach_calls;
    unsigned complete_calls;
    uint64_t last_timestamp_ns;
    uint64_t last_reset_timestamp_ns;
    bool trigger_reentry;
    bool reentry_rejected;
    bool reentry_triggered;
    bool use_dwc2;
} AdapterFixture;

typedef struct AsyncTransportFixture {
    DeviceState *host;
    USBBus bus;
    USBPort port;
    AdapterTestAsyncUsbDevice *device;
    DmUsbHostQemuTransport transport;
} AsyncTransportFixture;

typedef struct HostPortFixture {
    DeviceState *host_owner;
    USBBus bus;
    DmStm32H7OtgHost host;
    DmUsbHostQemuPort qemu_port;
    AdapterTestAsyncUsbDevice *device;
} HostPortFixture;

static void fixture_packet_init(USBPacket *packet, USBDevice *dev, int pid,
                                USBEndpoint *endpoint, void *data,
                                size_t length);

static void fixture_port_attach(USBPort *port)
{
    AdapterFixture *fixture = port->opaque;

    g_assert_true(port == &fixture->port);
    fixture->attach_calls++;
}

static void fixture_port_detach(USBPort *port)
{
    AdapterFixture *fixture = port->opaque;

    g_assert_true(port == &fixture->port);
    fixture->detach_calls++;
}

static void fixture_port_complete(USBPort *port, USBPacket *packet)
{
    AdapterFixture *fixture = port->opaque;

    g_assert_true(port == &fixture->port);
    g_assert_false(usb_packet_is_inflight(packet));
    fixture->complete_calls++;
}

static USBPortOps fixture_port_ops = {
    .attach = fixture_port_attach,
    .detach = fixture_port_detach,
    .complete = fixture_port_complete,
};

static void async_transport_port_noop(USBPort *port)
{
    (void)port;
}

static USBPortOps async_transport_port_ops = {
    .attach = async_transport_port_noop,
    .detach = async_transport_port_noop,
};

static USBBusOps fixture_bus_ops = {
};

const VMStateInfo vmstate_info_uint8 = { 0 };
const VMStateInfo vmstate_info_int32 = { 0 };

const char *qdev_fw_name(DeviceState *dev)
{
    (void)dev;
    return "dm-usb-qemu-adapter";
}

int monitor_printf(Monitor *mon, const char *fmt, ...)
{
    (void)mon;
    (void)fmt;
    return 0;
}

void usb_pcap_init(FILE *fp)
{
    (void)fp;
}

void usb_pcap_ctrl(USBPacket *p, bool setup)
{
    (void)p;
    (void)setup;
}

void usb_pcap_data(USBPacket *p, bool setup)
{
    (void)p;
    (void)setup;
}

static bool fixture_get_descriptor(void *opaque, uint8_t type, uint8_t index,
                                   const uint8_t **data, size_t *length)
{
    AdapterFixture *fixture = opaque;

    if (type != USB_DT_DEVICE || index != 0) {
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
    AdapterFixture *fixture = opaque;

    if (request->request != 0x20 || request->length != 4 ||
        request->request_type != 0x21) {
        return DM_USB_CONTROL_STALL;
    }
    if (!out_data) {
        return DM_USB_CONTROL_ACCEPTED;
    }
    if (out_length != request->length || out_length > sizeof(fixture->class_data) ||
        !out_data || in_data || in_capacity || in_length) {
        return DM_USB_CONTROL_INVALID;
    }
    memcpy(fixture->class_data, out_data, out_length);
    fixture->class_data_length = out_length;
    return DM_USB_CONTROL_ACCEPTED;
}

static DmUsbTransactionStatus fixture_in(
    void *opaque, uint8_t endpoint, uint8_t *data, size_t capacity,
    size_t *length, uint64_t timestamp_ns)
{
    AdapterFixture *fixture = opaque;

    fixture->last_timestamp_ns = timestamp_ns;
    if (fixture->forced_status != DM_USB_TRANSACTION_ACCEPTED) {
        return fixture->forced_status;
    }
    if (endpoint != 1 || capacity < fixture->bulk_in_length) {
        return DM_USB_TRANSACTION_INVALID;
    }
    memcpy(data, fixture->bulk_in, fixture->bulk_in_length);
    *length = fixture->bulk_in_length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static DmUsbTransactionStatus fixture_out(
    void *opaque, uint8_t endpoint, const uint8_t *data, size_t length,
    uint64_t timestamp_ns)
{
    AdapterFixture *fixture = opaque;

    fixture->last_timestamp_ns = timestamp_ns;
    if (fixture->forced_status != DM_USB_TRANSACTION_ACCEPTED) {
        return fixture->forced_status;
    }
    if (endpoint != 1 || length > sizeof(fixture->bulk_out)) {
        return DM_USB_TRANSACTION_INVALID;
    }
    memcpy(fixture->bulk_out, data, length);
    fixture->bulk_out_length = length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static void fixture_trigger_reentry(AdapterFixture *fixture)
{
    if (fixture->trigger_reentry && !fixture->reentry_triggered) {
        USBDevice *dev = USB_DEVICE(fixture->adapter);
        USBPacket packet;
        uint8_t data = 0xaa;

        fixture->reentry_triggered = true;
        fixture_packet_init(&packet, dev, USB_TOKEN_OUT, &dev->ep_out[0],
                            &data, sizeof(data));
        USB_DEVICE_GET_CLASS(dev)->handle_data(dev, &packet);
        fixture->reentry_rejected = packet.status == USB_RET_IOERROR &&
                                    packet.actual_length == 0;
        usb_packet_cleanup(&packet);
    }
}

static DmUsbControlResult fixture_control_request(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length, uint64_t timestamp_ns)
{
    AdapterFixture *fixture = opaque;

    fixture->control_calls++;
    fixture->last_control_timestamp_ns = timestamp_ns;
    fixture_trigger_reentry(fixture);
    return dm_usb_control_execute_request(&fixture->control, request,
                                          out_data, out_length, in_data,
                                          in_capacity, actual_length);
}

static DmUsbTransactionResult fixture_submit(
    void *opaque, const DmUsbTransaction *transaction)
{
    AdapterFixture *fixture = opaque;
    DmUsbTransactionResult result;

    fixture->submit_calls++;
    fixture_trigger_reentry(fixture);
    if (fixture->use_dwc2) {
        result = dm_usb_dwc2_submit(&fixture->dwc2, transaction);
    } else {
        result = dm_usb_transaction_submit(&fixture->target, transaction);
    }
    return result;
}

static void fixture_reset(void *opaque, uint64_t timestamp_ns)
{
    AdapterFixture *fixture = opaque;

    fixture->reset_calls++;
    fixture->last_reset_timestamp_ns = timestamp_ns;
    if (fixture->use_dwc2) {
        dm_usb_dwc2_bus_reset(&fixture->dwc2, timestamp_ns);
    } else {
        dm_usb_control_bus_reset(&fixture->control);
        dm_usb_transaction_reset(&fixture->target);
    }
}

static USBDevice *fixture_device(AdapterFixture *fixture)
{
    USBDevice *dev = usb_find_device(&fixture->port, 0);

    g_assert_nonnull(dev);
    g_assert_true(dev == USB_DEVICE(fixture->adapter));
    return dev;
}

static void fixture_init_with_lower(AdapterFixture *fixture, bool use_dwc2)
{
    static const DmUsbControlOps control_ops = {
        .get_descriptor = fixture_get_descriptor,
        .class_request = fixture_class_request,
    };
    static const DmUsbTransactionOps transaction_ops = {
        .in = fixture_in,
        .out = fixture_out,
    };
    Error *err = NULL;
    USBDevice *dev;

    memset(fixture, 0, sizeof(*fixture));
    for (unsigned i = 0; i < sizeof(fixture->descriptor); ++i) {
        fixture->descriptor[i] = (uint8_t)i;
    }
    memcpy(fixture->bulk_in, "ABCDEFG", 7);
    fixture->bulk_in_length = 7;
    fixture->forced_status = DM_USB_TRANSACTION_ACCEPTED;
    fixture->use_dwc2 = use_dwc2;

    dm_usb_control_init(&fixture->control, &control_ops, fixture, 64);
    if (use_dwc2) {
        dm_usb_dwc2_init(&fixture->dwc2, &fixture->control, NULL, NULL, 64);
    } else {
        dm_usb_transaction_init(&fixture->target, &fixture->control,
                                &transaction_ops, fixture, 64);
        g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                            &fixture->target, 1, DM_USB_ENDPOINT_IN,
                            DM_USB_ENDPOINT_BULK, 64, true), ==,
                        DM_USB_TRANSACTION_ACCEPTED);
        g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                            &fixture->target, 1, DM_USB_ENDPOINT_OUT,
                            DM_USB_ENDPOINT_BULK, 64, true), ==,
                        DM_USB_TRANSACTION_ACCEPTED);
    }

    fixture->host = DEVICE(object_new(TYPE_ADAPTER_TEST_USB_HOST));
    usb_bus_new(&fixture->bus, sizeof(fixture->bus), &fixture_bus_ops,
                fixture->host);
    usb_register_port(&fixture->bus, &fixture->port, fixture, 0,
                      &fixture_port_ops, USB_SPEED_MASK_FULL);

    fixture->adapter = DM_USB_QEMU_ADAPTER(
        usb_new(TYPE_DM_USB_QEMU_ADAPTER));
    dev = USB_DEVICE(fixture->adapter);
    dm_usb_qemu_adapter_set_callbacks(fixture->adapter, fixture_submit,
                                       fixture_reset, fixture);
    dm_usb_qemu_adapter_set_control_callback(fixture->adapter,
                                              fixture_control_request);
    qdev_prop_set_string(DEVICE(dev), "port", fixture->port.path);
    g_assert_true(usb_realize_and_unref(dev, &fixture->bus, &err));
    g_assert_null(err);
    g_assert_true(dev->qdev.realized);
    g_assert_true(dev->qdev.parent_bus == &fixture->bus.qbus);
    g_assert_true(dev->port == &fixture->port);
    g_assert_true(fixture->port.dev == dev);
    g_assert_cmpuint(fixture->bus.nfree, ==, 0);
    g_assert_cmpuint(fixture->bus.nused, ==, 1);
    g_assert_cmpint(dev->speed, ==, USB_SPEED_FULL);
    g_assert_cmpuint(fixture->attach_calls, ==, 1);

    /* The device becomes addressable only after its host resets the port. */
    usb_port_reset(&fixture->port);
    g_assert_cmpuint(fixture->attach_calls, ==, 2);
    g_assert_cmpuint(fixture->detach_calls, ==, 1);
    g_assert_cmpuint(fixture->reset_calls, ==, 1);
    g_assert_true(fixture_device(fixture) == dev);
}

static void fixture_init(AdapterFixture *fixture)
{
    fixture_init_with_lower(fixture, false);
}

static void fixture_init_dwc2(AdapterFixture *fixture)
{
    fixture_init_with_lower(fixture, true);
}

static void fixture_host_port_reset(void *opaque, uint64_t timestamp_ns)
{
    AdapterFixture *fixture = opaque;

    (void)timestamp_ns;
    usb_port_reset(&fixture->port);
}

static void fixture_enable_host_controller(AdapterFixture *fixture)
{
    dm_stm32h7_otg_host_set_port_connected(
        &fixture->host_controller, true, DM_STM32H7_OTG_PORT_FULL_SPEED);
    dm_stm32h7_otg_host_write(&fixture->host_controller,
                               DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 50);
    dm_stm32h7_otg_host_write(&fixture->host_controller,
                               DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 60);
}

static void fixture_init_host_channel_qemu_transport(AdapterFixture *fixture)
{
    fixture_init(fixture);
    dm_usb_host_qemu_transport_init_port(&fixture->host_qemu_transport,
                                         &fixture->port);
    dm_stm32h7_otg_host_init(&fixture->host_controller,
                              fixture_host_port_reset, fixture, NULL, NULL);
    dm_usb_host_channel_transport_init_with_opaques(
        &fixture->host_channel_transport, &fixture->host_controller,
        NULL, NULL,
        dm_stm32h7_otg_host_read_out_fifo, &fixture->host_controller,
        dm_stm32h7_otg_host_write_in_fifo, &fixture->host_controller);
    dm_usb_host_channel_transport_set_route(
        &fixture->host_channel_transport, dm_usb_host_qemu_transport_route,
        &fixture->host_qemu_transport);
    fixture_enable_host_controller(fixture);
}

static void fixture_init_host_channel_lifecycle_qemu_transport(
    AdapterFixture *fixture)
{
    static const DmUsbControlOps control_ops = {
        .get_descriptor = fixture_get_descriptor,
        .class_request = fixture_class_request,
    };
    static const DmUsbTransactionOps transaction_ops = {
        .in = fixture_in,
        .out = fixture_out,
    };
    Error *err = NULL;
    USBDevice *device;

    memset(fixture, 0, sizeof(*fixture));
    for (unsigned index = 0; index < sizeof(fixture->descriptor); ++index) {
        fixture->descriptor[index] = index;
    }
    fixture->forced_status = DM_USB_TRANSACTION_ACCEPTED;
    dm_usb_control_init(&fixture->control, &control_ops, fixture, 64);
    dm_usb_transaction_init(&fixture->target, &fixture->control,
                            &transaction_ops, fixture, 64);

    fixture->host = DEVICE(object_new(TYPE_ADAPTER_TEST_USB_HOST));
    usb_bus_new(&fixture->bus, sizeof(fixture->bus), &fixture_bus_ops,
                fixture->host);
    dm_stm32h7_otg_host_init(&fixture->host_controller,
                              dm_usb_host_qemu_port_reset,
                              &fixture->host_qemu_port, NULL, NULL);
    dm_usb_host_qemu_port_init(&fixture->host_qemu_port, &fixture->bus,
                               &fixture->host_controller, 0);
    fixture->adapter = DM_USB_QEMU_ADAPTER(
        usb_new(TYPE_DM_USB_QEMU_ADAPTER));
    device = USB_DEVICE(fixture->adapter);
    dm_usb_qemu_adapter_set_callbacks(fixture->adapter, fixture_submit,
                                       fixture_reset, fixture);
    dm_usb_qemu_adapter_set_control_callback(fixture->adapter,
                                              fixture_control_request);
    qdev_prop_set_string(DEVICE(device), "port",
                         fixture->host_qemu_port.port.path);
    g_assert_true(usb_realize_and_unref(device, &fixture->bus, &err));
    g_assert_null(err);

    dm_usb_host_qemu_transport_init_port(&fixture->host_qemu_transport,
                                         &fixture->host_qemu_port.port);
    dm_usb_host_channel_transport_init_with_opaques(
        &fixture->host_channel_transport, &fixture->host_controller,
        NULL, NULL,
        dm_stm32h7_otg_host_read_out_fifo, &fixture->host_controller,
        dm_stm32h7_otg_host_write_in_fifo, &fixture->host_controller);
    dm_usb_host_channel_transport_set_route(
        &fixture->host_channel_transport, dm_usb_host_qemu_transport_route,
        &fixture->host_qemu_transport);

    dm_stm32h7_otg_host_write(&fixture->host_controller,
                               DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_CONNDET, 40);
    dm_stm32h7_otg_host_write(&fixture->host_controller,
                               DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 50);
    dm_stm32h7_otg_host_write(&fixture->host_controller,
                               DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 60);
}

static void fixture_cleanup_host_channel_lifecycle_qemu_transport(
    AdapterFixture *fixture)
{
    dm_usb_host_channel_transport_set_route(
        &fixture->host_channel_transport, NULL, NULL);
    dm_usb_host_qemu_transport_clear(&fixture->host_qemu_transport);
    object_unparent(OBJECT(fixture->adapter));
    dm_usb_host_qemu_port_cleanup(&fixture->host_qemu_port);
    usb_bus_release(&fixture->bus);
    object_unparent(OBJECT(&fixture->bus));
    object_unref(OBJECT(fixture->host));
}

static void fixture_start_host_channel(AdapterFixture *fixture,
                                       uint32_t hctsiz, uint32_t hcchar,
                                       uint64_t timestamp_ns)
{
    dm_stm32h7_otg_host_write(&fixture->host_controller,
                               DM_STM32H7_OTG_HCTSIZ(0), hctsiz,
                               timestamp_ns);
    dm_stm32h7_otg_host_write(&fixture->host_controller,
                               DM_STM32H7_OTG_HCCHAR(0),
                               hcchar | DM_STM32H7_OTG_HCCHAR_CHENA,
                               timestamp_ns);
}

static void async_transport_fixture_init(AsyncTransportFixture *fixture)
{
    Error *err = NULL;
    USBDevice *device;

    memset(fixture, 0, sizeof(*fixture));
    fixture->host = DEVICE(object_new(TYPE_ADAPTER_TEST_USB_HOST));
    usb_bus_new(&fixture->bus, sizeof(fixture->bus), &fixture_bus_ops,
                fixture->host);
    usb_register_port(&fixture->bus, &fixture->port, fixture, 0,
                      &async_transport_port_ops, USB_SPEED_MASK_FULL);
    fixture->device = ADAPTER_TEST_ASYNC_USB_DEVICE(
        usb_new(TYPE_ADAPTER_TEST_ASYNC_USB_DEVICE));
    device = USB_DEVICE(fixture->device);
    qdev_prop_set_string(DEVICE(device), "port", fixture->port.path);
    g_assert_true(usb_realize_and_unref(device, &fixture->bus, &err));
    g_assert_null(err);
    usb_port_reset(&fixture->port);
    dm_usb_host_qemu_transport_init(&fixture->transport, device);
}

static void async_transport_fixture_cleanup(AsyncTransportFixture *fixture)
{
    USBDevice *device = USB_DEVICE(fixture->device);

    dm_usb_host_qemu_transport_clear(&fixture->transport);
    object_unparent(OBJECT(device));
    usb_unregister_port(&fixture->bus, &fixture->port);
    usb_bus_release(&fixture->bus);
    object_unparent(OBJECT(&fixture->bus));
    object_unref(OBJECT(fixture->host));
}

static void host_port_fixture_init(HostPortFixture *fixture)
{
    Error *err = NULL;
    USBDevice *device;

    memset(fixture, 0, sizeof(*fixture));
    fixture->host_owner = DEVICE(object_new(TYPE_ADAPTER_TEST_USB_HOST));
    usb_bus_new(&fixture->bus, sizeof(fixture->bus), &fixture_bus_ops,
                fixture->host_owner);
    dm_stm32h7_otg_host_init(&fixture->host, dm_usb_host_qemu_port_reset,
                              &fixture->qemu_port, NULL, NULL);
    dm_usb_host_qemu_port_init(&fixture->qemu_port, &fixture->bus,
                               &fixture->host, 0);
    fixture->device = ADAPTER_TEST_ASYNC_USB_DEVICE(
        usb_new(TYPE_ADAPTER_TEST_ASYNC_USB_DEVICE));
    device = USB_DEVICE(fixture->device);
    qdev_prop_set_string(DEVICE(device), "port", fixture->qemu_port.port.path);
    g_assert_true(usb_realize_and_unref(device, &fixture->bus, &err));
    g_assert_null(err);
}

static void host_port_fixture_cleanup(HostPortFixture *fixture)
{
    dm_usb_host_qemu_port_cleanup(&fixture->qemu_port);
    usb_bus_release(&fixture->bus);
    object_unparent(OBJECT(&fixture->bus));
    object_unref(OBJECT(fixture->host_owner));
}

static void fixture_cleanup(AdapterFixture *fixture)
{
    USBDevice *dev = USB_DEVICE(fixture->adapter);
    int busnr = fixture->bus.busnr;
    unsigned detach_calls = fixture->detach_calls;

    g_assert_true(dev->qdev.realized);
    g_assert_true(dev->port == &fixture->port);
    g_assert_true(fixture->port.dev == dev);
    g_assert_cmpuint(fixture->bus.nfree, ==, 0);
    g_assert_cmpuint(fixture->bus.nused, ==, 1);
    g_assert_cmpuint(fixture->complete_calls, ==, 0);

    dm_usb_host_qemu_transport_clear(&fixture->host_qemu_transport);
    object_unparent(OBJECT(dev));
    g_assert_null(fixture->port.dev);
    g_assert_cmpuint(fixture->detach_calls, ==, detach_calls + 1);
    g_assert_cmpuint(fixture->bus.nfree, ==, 1);
    g_assert_cmpuint(fixture->bus.nused, ==, 0);

    usb_unregister_port(&fixture->bus, &fixture->port);
    g_assert_cmpuint(fixture->bus.nfree, ==, 0);
    usb_bus_release(&fixture->bus);
    g_assert_null(usb_bus_find(busnr));
    object_unparent(OBJECT(&fixture->bus));
    object_unref(OBJECT(fixture->host));
}

static void fixture_packet_init(USBPacket *packet, USBDevice *dev, int pid,
                                USBEndpoint *endpoint, void *data,
                                size_t length)
{
    usb_packet_init(packet);
    usb_packet_setup(packet, pid, endpoint, 0, 0, false, false);
    if (length) {
        usb_packet_addbuf(packet, data, length);
    }
}

static void fixture_dwc2_queue_bulk_in(AdapterFixture *fixture,
                                       unsigned endpoint,
                                       const uint8_t *data, size_t length)
{
    uint32_t control;
    uint32_t transfer_size;
    uint32_t fifo;

    g_assert_true(fixture->use_dwc2);
    g_assert_cmpuint(endpoint, >, 0);
    g_assert_cmpuint(endpoint, <, DM_USB_DWC2_MAX_ENDPOINTS);
    g_assert_cmpuint(length, <=, 64);
    control = 64 | (2u << 18) | DM_USB_DWC2_DXEPCTL_USBAEP |
              DM_USB_DWC2_DXEPCTL_EPENA;
    transfer_size = (uint32_t)length |
                    (1u << DM_USB_DXEPTSIZ_PKTCNT_SHIFT);
    fifo = DM_USB_DWC2_FIFO0 + endpoint * DM_USB_DWC2_FIFO_STRIDE;
    dm_usb_dwc2_write(&fixture->dwc2,
                      DM_USB_DWC2_DIEPCTL0 + endpoint * DM_USB_DWC2_EP_STRIDE,
                      control, 4);
    dm_usb_dwc2_write(&fixture->dwc2,
                      DM_USB_DWC2_DIEPTSIZ0 + endpoint * DM_USB_DWC2_EP_STRIDE,
                      transfer_size, 4);
    for (size_t i = 0; i < length; ++i) {
        dm_usb_dwc2_write(&fixture->dwc2, fifo, data[i], 1);
    }
}

static void test_control_in_multi_packet(void)
{
    AdapterFixture fixture;
    USBPacket packet;
    USBDevice *dev;
    uint8_t setup[8] = {
        USB_DIR_IN | USB_TYPE_STANDARD | USB_RECIP_DEVICE,
        USB_REQ_GET_DESCRIPTOR,
        0,
        USB_DT_DEVICE,
        0,
        0,
        sizeof(fixture.descriptor),
        0,
    };
    uint8_t first[64];
    uint8_t second[16];

    fixture_init(&fixture);
    dev = fixture_device(&fixture);
    fixture.trigger_reentry = true;
    fixture_packet_init(&packet, dev, USB_TOKEN_SETUP, &dev->ep_ctl,
                        setup, sizeof(setup));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpint(packet.actual_length, ==, sizeof(setup));
    usb_packet_cleanup(&packet);

    fixture_packet_init(&packet, dev, USB_TOKEN_IN, &dev->ep_ctl,
                        first, sizeof(first));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpint(packet.actual_length, ==, sizeof(first));
    g_assert_cmpmem(first, sizeof(first), fixture.descriptor, sizeof(first));
    usb_packet_cleanup(&packet);

    fixture_packet_init(&packet, dev, USB_TOKEN_IN, &dev->ep_ctl,
                        second, sizeof(second));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpint(packet.actual_length, ==, sizeof(second));
    g_assert_cmpmem(second, sizeof(second), fixture.descriptor + sizeof(first),
                    sizeof(second));
    usb_packet_cleanup(&packet);

    fixture_packet_init(&packet, dev, USB_TOKEN_OUT, &dev->ep_ctl, NULL, 0);
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpint(packet.actual_length, ==, 0);
    usb_packet_cleanup(&packet);

    g_assert_cmpuint(fixture.control_calls, ==, 1);
    g_assert_cmpuint(fixture.submit_calls, ==, 0);
    g_assert_true(fixture.reentry_rejected);
    g_assert_cmpint((int64_t)fixture.last_control_timestamp_ns, >=, 0);
    fixture_cleanup(&fixture);
}

static void test_control_out_and_status(void)
{
    static const uint8_t data[] = { 1, 2, 3, 4 };
    AdapterFixture fixture;
    USBPacket packet;
    USBDevice *dev;
    uint8_t setup[8] = {
        USB_DIR_OUT | USB_TYPE_CLASS | USB_RECIP_INTERFACE,
        0x20,
        0,
        0,
        0,
        0,
        sizeof(data),
        0,
    };

    fixture_init(&fixture);
    dev = fixture_device(&fixture);
    fixture_packet_init(&packet, dev, USB_TOKEN_SETUP, &dev->ep_ctl,
                        setup, sizeof(setup));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpint(packet.actual_length, ==, sizeof(setup));
    usb_packet_cleanup(&packet);

    fixture_packet_init(&packet, dev, USB_TOKEN_OUT, &dev->ep_ctl,
                        (uint8_t *)data, sizeof(data));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpint(packet.actual_length, ==, sizeof(data));
    usb_packet_cleanup(&packet);

    fixture_packet_init(&packet, dev, USB_TOKEN_IN, &dev->ep_ctl, NULL, 0);
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpuint(packet.actual_length, ==, 0);
    g_assert_cmpuint(fixture.control_calls, ==, 1);
    g_assert_cmpuint(fixture.submit_calls, ==, 0);
    g_assert_cmpmem(fixture.class_data, fixture.class_data_length,
                    data, sizeof(data));
    fixture_cleanup(&fixture);
}

static void test_data_iov_and_status_mapping(void)
{
    AdapterFixture fixture;
    USBDevice *dev;
    USBPacket packet;
    uint8_t out_a[] = { 9, 8 };
    uint8_t out_b[] = { 7, 6, 5 };
    static const uint8_t expected_out[] = { 9, 8, 7, 6, 5 };
    uint8_t in_a[3] = { 0 };
    uint8_t in_b[4] = { 0 };

    fixture_init(&fixture);
    dev = fixture_device(&fixture);

    fixture_packet_init(&packet, dev, USB_TOKEN_OUT, &dev->ep_out[0],
                        out_a, sizeof(out_a));
    usb_packet_addbuf(&packet, out_b, sizeof(out_b));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpuint(packet.actual_length, ==, 5);
    g_assert_cmpmem(fixture.bulk_out, fixture.bulk_out_length,
                    expected_out, sizeof(expected_out));
    usb_packet_cleanup(&packet);

    fixture_packet_init(&packet, dev, USB_TOKEN_IN, &dev->ep_in[0],
                        in_a, sizeof(in_a));
    usb_packet_addbuf(&packet, in_b, sizeof(in_b));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpuint(packet.actual_length, ==, 7);
    g_assert_cmpmem(in_a, sizeof(in_a), "ABC", 3);
    g_assert_cmpmem(in_b, sizeof(in_b), "DEFG", 4);
    usb_packet_cleanup(&packet);

    fixture.forced_status = DM_USB_TRANSACTION_NAK;
    fixture_packet_init(&packet, dev, USB_TOKEN_IN, &dev->ep_in[0],
                        in_a, sizeof(in_a));
    usb_handle_packet(dev, &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_NAK);
    g_assert_cmpuint(packet.actual_length, ==, 0);
    usb_packet_cleanup(&packet);

    fixture_cleanup(&fixture);
}

static void test_reset_callback(void)
{
    AdapterFixture fixture;
    unsigned attach_calls;
    unsigned detach_calls;
    unsigned reset_calls;
    uint64_t before_timestamp_ns;
    uint64_t after_timestamp_ns;

    fixture_init(&fixture);
    attach_calls = fixture.attach_calls;
    detach_calls = fixture.detach_calls;
    reset_calls = fixture.reset_calls;
    before_timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    usb_port_reset(&fixture.port);
    after_timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    g_assert_cmpuint(fixture.attach_calls, ==, attach_calls + 1);
    g_assert_cmpuint(fixture.detach_calls, ==, detach_calls + 1);
    g_assert_cmpuint(fixture.reset_calls, ==, reset_calls + 1);
    g_assert_cmpuint(fixture.last_reset_timestamp_ns, >=, before_timestamp_ns);
    g_assert_cmpuint(fixture.last_reset_timestamp_ns, <=, after_timestamp_ns);
    g_assert_nonnull(fixture_device(&fixture));
    fixture_cleanup(&fixture);
}

static void test_bus_attachment_and_routing(void)
{
    AdapterFixture fixture;
    USBDevice *dev;
    USBPacket packet;
    uint8_t data[] = { 0x12, 0x34, 0x56 };

    fixture_init(&fixture);
    dev = fixture_device(&fixture);
    g_assert_true(dev->attached);
    g_assert_cmpint(dev->state, ==, USB_STATE_DEFAULT);
    g_assert_cmpuint(dev->speedmask & fixture.port.speedmask, !=, 0);
    g_assert_cmpstr(fixture.port.path, ==, "1");

    fixture_packet_init(&packet, dev, USB_TOKEN_OUT, &dev->ep_out[0],
                        data, sizeof(data));
    usb_handle_packet(usb_find_device(&fixture.port, 0), &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpuint(packet.actual_length, ==, sizeof(data));
    g_assert_cmpmem(fixture.bulk_out, fixture.bulk_out_length,
                    data, sizeof(data));
    usb_packet_cleanup(&packet);
    fixture_cleanup(&fixture);
}

static void test_dwc2_transaction_bridge(void)
{
    static const uint8_t payload[] = { 0xa1, 0xb2, 0xc3, 0xd4, 0xe5 };
    AdapterFixture fixture;
    USBDevice *dev;
    USBPacket packet;
    uint8_t received[sizeof(payload)] = { 0 };
    uint32_t endpoint_offset;
    unsigned reset_calls;

    fixture_init_dwc2(&fixture);
    dev = fixture_device(&fixture);
    fixture_dwc2_queue_bulk_in(&fixture, 1, payload, sizeof(payload));

    fixture_packet_init(&packet, dev, USB_TOKEN_IN, &dev->ep_in[0],
                        received, sizeof(received));
    usb_handle_packet(usb_find_device(&fixture.port, 0), &packet);
    g_assert_cmpint(packet.status, ==, USB_RET_SUCCESS);
    g_assert_cmpuint(packet.actual_length, ==, sizeof(payload));
    g_assert_cmpmem(received, sizeof(received), payload, sizeof(payload));
    usb_packet_cleanup(&packet);

    endpoint_offset = DM_USB_DWC2_DIEPTSIZ0 + DM_USB_DWC2_EP_STRIDE;
    g_assert_cmpuint(dm_usb_dwc2_read(&fixture.dwc2, endpoint_offset, 4),
                     ==, 0);
    endpoint_offset = DM_USB_DWC2_DIEPINT0 + DM_USB_DWC2_EP_STRIDE;
    g_assert_cmpuint(dm_usb_dwc2_read(&fixture.dwc2, endpoint_offset, 4),
                     ==, DM_USB_DXEPINT_XFRC);

    reset_calls = fixture.reset_calls;
    usb_port_reset(&fixture.port);
    g_assert_cmpuint(fixture.reset_calls, ==, reset_calls + 1);
    endpoint_offset = DM_USB_DWC2_DIEPCTL0 + DM_USB_DWC2_EP_STRIDE;
    g_assert_cmpuint(dm_usb_dwc2_read(&fixture.dwc2, endpoint_offset, 4),
                     ==, DM_USB_DWC2_DXEPCTL_USBAEP | (2u << 18) | 64u);
    g_assert_nonnull(fixture_device(&fixture));
    fixture_cleanup(&fixture);
}

static void test_host_qemu_transport_data_and_status(void)
{
    static const uint8_t get_status_setup[] = {
        USB_DIR_IN | USB_TYPE_STANDARD | USB_RECIP_DEVICE,
        USB_REQ_GET_STATUS,
        0, 0, 0, 0, 2, 0,
    };
    AdapterFixture fixture;
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_OUT,
        .endpoint = 1,
        .out_data = (const uint8_t *)"OUT",
        .length = 3,
    };
    DmUsbTransactionResult result;
    uint8_t input[16] = { 0 };
    USBDevice *device;
    Error *err = NULL;

    fixture_init(&fixture);
    device = fixture_device(&fixture);
    dm_usb_host_qemu_transport_init(&fixture.host_qemu_transport,
                                    device);
    g_assert_cmpint(usb_device_detach(device), ==, 0);
    usb_device_attach(device, &err);
    g_assert_null(err);
    result = dm_usb_host_qemu_transport_submit(&fixture.host_qemu_transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NO_DEVICE);
    usb_port_reset(&fixture.port);
    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_SETUP,
        .endpoint = 0,
        .out_data = get_status_setup,
        .length = sizeof(get_status_setup),
    };
    result = dm_usb_host_qemu_transport_submit(&fixture.host_qemu_transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(get_status_setup));
    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_OUT,
        .endpoint = 1,
        .out_data = (const uint8_t *)"OUT",
        .length = 3,
    };
    result = dm_usb_host_qemu_transport_submit(&fixture.host_qemu_transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, transaction.length);
    g_assert_cmpmem(fixture.bulk_out, fixture.bulk_out_length, "OUT", 3);

    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_IN,
        .endpoint = 1,
        .in_data = input,
        .capacity = sizeof(input),
    };
    result = dm_usb_host_qemu_transport_submit(&fixture.host_qemu_transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, fixture.bulk_in_length);
    g_assert_cmpmem(input, result.actual_length, fixture.bulk_in,
                    fixture.bulk_in_length);

    fixture.forced_status = DM_USB_TRANSACTION_NAK;
    result = dm_usb_host_qemu_transport_submit(&fixture.host_qemu_transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NAK);
    g_assert_cmpuint(result.actual_length, ==, 0);
    fixture.forced_status = DM_USB_TRANSACTION_STALL;
    result = dm_usb_host_qemu_transport_submit(&fixture.host_qemu_transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_STALL);
    g_assert_cmpuint(result.actual_length, ==, 0);
    fixture_cleanup(&fixture);
}

static void test_host_qemu_port_transport_address_routing(void)
{
    static const uint8_t set_address_setup[] = {
        USB_DIR_OUT | USB_TYPE_STANDARD | USB_RECIP_DEVICE,
        USB_REQ_SET_ADDRESS,
        13, 0, 0, 0, 0, 0,
    };
    static const uint8_t get_status_setup[] = {
        USB_DIR_IN | USB_TYPE_STANDARD | USB_RECIP_DEVICE,
        USB_REQ_GET_STATUS,
        0, 0, 0, 0, 2, 0,
    };
    AdapterFixture fixture;
    DmUsbTransaction transaction;
    DmUsbTransactionResult result;
    USBDevice *device;
    uint8_t status[2] = { 0xff, 0xff };

    fixture_init(&fixture);
    device = fixture_device(&fixture);
    dm_usb_host_qemu_transport_init_port(&fixture.host_qemu_transport,
                                         &fixture.port);

    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_SETUP,
        .endpoint = 0,
        .out_data = set_address_setup,
        .length = sizeof(set_address_setup),
    };
    result = dm_usb_host_qemu_transport_route(&fixture.host_qemu_transport,
                                              0, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(set_address_setup));
    g_assert_cmpuint(device->addr, ==, 0);
    g_assert_true(usb_find_device(&fixture.port, 0) == device);
    g_assert_null(usb_find_device(&fixture.port, 13));

    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_IN,
        .endpoint = 0,
    };
    result = dm_usb_host_qemu_transport_route(&fixture.host_qemu_transport,
                                              0, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 0);
    g_assert_cmpuint(device->addr, ==, 13);
    g_assert_null(usb_find_device(&fixture.port, 0));
    g_assert_true(usb_find_device(&fixture.port, 13) == device);

    result = dm_usb_host_qemu_transport_route(&fixture.host_qemu_transport,
                                              0, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NO_DEVICE);

    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_SETUP,
        .endpoint = 0,
        .out_data = get_status_setup,
        .length = sizeof(get_status_setup),
    };
    result = dm_usb_host_qemu_transport_route(&fixture.host_qemu_transport,
                                              13, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(get_status_setup));
    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_IN,
        .endpoint = 0,
        .in_data = status,
        .capacity = sizeof(status),
    };
    result = dm_usb_host_qemu_transport_route(&fixture.host_qemu_transport,
                                              13, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(status));
    g_assert_cmpmem(status, sizeof(status), "\0\0", sizeof(status));
    fixture_cleanup(&fixture);
}

static void test_host_qemu_transport_cancels_async(void)
{
    AsyncTransportFixture fixture;
    uint8_t data[] = { 1, 2, 3 };
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_OUT,
        .endpoint = 1,
        .out_data = data,
        .length = sizeof(data),
    };
    DmUsbTransactionResult result;

    async_transport_fixture_init(&fixture);
    result = dm_usb_host_qemu_transport_submit(&fixture.transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_DEFERRED);
    g_assert_cmpuint(result.actual_length, ==, 0);
    g_assert_cmpuint(fixture.device->cancel_calls, ==, 1);
    g_assert_true(QTAILQ_EMPTY(&fixture.device->parent_obj.ep_out[0].queue));
    async_transport_fixture_cleanup(&fixture);
}

static void test_host_qemu_transport_status_mapping(void)
{
    AsyncTransportFixture fixture;
    uint8_t data[] = { 1, 2, 3 };
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_OUT,
        .endpoint = 1,
        .out_data = data,
        .length = sizeof(data),
    };
    DmUsbTransactionResult result;

    async_transport_fixture_init(&fixture);

    fixture.device->packet_status = USB_RET_NODEV;
    result = dm_usb_host_qemu_transport_submit(&fixture.transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_NO_DEVICE);

    fixture.device->packet_status = USB_RET_BABBLE;
    result = dm_usb_host_qemu_transport_submit(&fixture.transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_BABBLE);

    fixture.device->packet_status = USB_RET_IOERROR;
    result = dm_usb_host_qemu_transport_submit(&fixture.transport,
                                               &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_IO_ERROR);

    async_transport_fixture_cleanup(&fixture);
}

static void test_host_qemu_transport_binding_contract(void)
{
    AdapterFixture fixture;
    DmUsbHostQemuTransport transport = { 0 };
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_IN,
        .endpoint = 1,
    };
    DmUsbTransactionResult result;

    fixture_init(&fixture);

    /* A transport is a single borrowed binding, never an implicit fallback. */
    result = dm_usb_host_qemu_transport_submit(&transport, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    result = dm_usb_host_qemu_transport_route(&transport, 0, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    result = dm_usb_host_qemu_transport_submit(&transport, NULL);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);

    dm_usb_host_qemu_transport_init(&transport, fixture_device(&fixture));
    result = dm_usb_host_qemu_transport_submit(&transport, NULL);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    transport.port = &fixture.port;
    result = dm_usb_host_qemu_transport_submit(&transport, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    result = dm_usb_host_qemu_transport_route(&transport, 0, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    dm_usb_host_qemu_transport_clear(&transport);
    result = dm_usb_host_qemu_transport_submit(&transport, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);

    dm_usb_host_qemu_transport_init_port(&transport, &fixture.port);
    result = dm_usb_host_qemu_transport_route(&transport, 0, NULL);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);
    dm_usb_host_qemu_transport_clear(&transport);
    result = dm_usb_host_qemu_transport_route(&transport, 0, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_INVALID);

    fixture_cleanup(&fixture);
}

static void test_host_qemu_port_lifecycle(void)
{
    HostPortFixture fixture;
    USBDevice *device;
    Error *err = NULL;
    uint32_t hprt0;

    host_port_fixture_init(&fixture);
    device = USB_DEVICE(fixture.device);
    hprt0 = dm_stm32h7_otg_host_read(&fixture.host,
                                     DM_STM32H7_OTG_HPRT0);
    g_assert_cmpuint(hprt0 & (DM_STM32H7_OTG_HPRT0_PWR |
                              DM_STM32H7_OTG_HPRT0_CONNSTS |
                              DM_STM32H7_OTG_HPRT0_CONNDET), ==,
                     DM_STM32H7_OTG_HPRT0_PWR |
                     DM_STM32H7_OTG_HPRT0_CONNSTS |
                     DM_STM32H7_OTG_HPRT0_CONNDET);
    g_assert_cmpuint((hprt0 & DM_STM32H7_OTG_HPRT0_SPD_MASK) >>
                     DM_STM32H7_OTG_HPRT0_SPD_SHIFT, ==,
                     DM_STM32H7_OTG_PORT_FULL_SPEED);

    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_CONNDET, 10);
    device->addr = 13;
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 20);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 30);
    hprt0 = dm_stm32h7_otg_host_read(&fixture.host,
                                     DM_STM32H7_OTG_HPRT0);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_CONNSTS);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_ENA);
    g_assert_false(hprt0 & DM_STM32H7_OTG_HPRT0_CONNDET);
    g_assert_cmpint(device->state, ==, USB_STATE_DEFAULT);
    g_assert_cmpuint(device->addr, ==, 0);

    g_assert_cmpint(usb_device_detach(device), ==, 0);
    hprt0 = dm_stm32h7_otg_host_read(&fixture.host,
                                     DM_STM32H7_OTG_HPRT0);
    g_assert_false(hprt0 & DM_STM32H7_OTG_HPRT0_CONNSTS);
    g_assert_false(hprt0 & DM_STM32H7_OTG_HPRT0_ENA);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_CONNDET);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_ENACHG);

    usb_device_attach(device, &err);
    g_assert_null(err);
    hprt0 = dm_stm32h7_otg_host_read(&fixture.host,
                                     DM_STM32H7_OTG_HPRT0);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_CONNSTS);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_CONNDET);
    host_port_fixture_cleanup(&fixture);
    g_assert_false(fixture.qemu_port.registered);
    g_assert_null(fixture.qemu_port.host);
    g_assert_null(fixture.qemu_port.bus);
    dm_usb_host_qemu_port_reset(&fixture.qemu_port, 40);
}

static void test_host_channel_qemu_transport_composition(void)
{
    AdapterFixture fixture;
    uint32_t out_hctsiz = 3 |
                          (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t in_hctsiz = 16 |
                         (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t out_hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                          (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    uint32_t in_hcchar = out_hcchar | DM_STM32H7_OTG_HCCHAR_EPDIR;

    fixture_init_host_channel_qemu_transport(&fixture);
    dm_stm32h7_otg_host_fifo_write(&fixture.host_controller, 0, 0x0054554f,
                                    3);
    fixture_start_host_channel(&fixture, out_hctsiz, out_hcchar, 100);
    g_assert_cmpmem(fixture.bulk_out, fixture.bulk_out_length, "OUT", 3);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host_controller,
                         DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);

    memcpy(fixture.bulk_in, "PONG", 4);
    fixture.bulk_in_length = 4;
    fixture_start_host_channel(&fixture, in_hctsiz, in_hcchar, 200);
    g_assert_cmphex(dm_stm32h7_otg_host_fifo_read(&fixture.host_controller,
                                                   0, 4), ==, 0x474e4f50);
    g_assert_cmphex(dm_stm32h7_otg_host_fifo_read(&fixture.host_controller,
                                                   0, 4), ==, 0);
    fixture_cleanup(&fixture);
}

static void test_host_channel_control_qemu_lifecycle_enumeration(void)
{
    static const uint8_t get_descriptor[] = {
        USB_DIR_IN | USB_TYPE_STANDARD | USB_RECIP_DEVICE,
        USB_REQ_GET_DESCRIPTOR,
        0, USB_DT_DEVICE, 0, 0, 18, 0,
    };
    static const uint8_t set_address[] = {
        USB_DIR_OUT | USB_TYPE_STANDARD | USB_RECIP_DEVICE,
        USB_REQ_SET_ADDRESS,
        13, 0, 0, 0, 0, 0,
    };
    static const uint8_t get_status[] = {
        USB_DIR_IN | USB_TYPE_STANDARD | USB_RECIP_DEVICE,
        USB_REQ_GET_STATUS,
        0, 0, 0, 0, 2, 0,
    };
    AdapterFixture fixture;
    DmUsbHostChannelControl control;
    DmUsbHostChannelControlResult result;
    USBDevice *device;
    uint8_t descriptor[18];
    uint8_t status[2] = { 0xff, 0xff };
    uint32_t hprt0;

    fixture_init_host_channel_lifecycle_qemu_transport(&fixture);
    hprt0 = dm_stm32h7_otg_host_read(&fixture.host_controller,
                                     DM_STM32H7_OTG_HPRT0);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_CONNSTS);
    g_assert_true(hprt0 & DM_STM32H7_OTG_HPRT0_ENA);
    g_assert_false(hprt0 & DM_STM32H7_OTG_HPRT0_CONNDET);
    g_assert_cmpuint(fixture.reset_calls, ==, 1);
    device = usb_find_device(&fixture.host_qemu_port.port, 0);
    g_assert_nonnull(device);

    dm_usb_host_channel_control_init(&control, &fixture.host_controller, 0,
                                     64);
    result = dm_usb_host_channel_control_transfer(
        &control, 0, get_descriptor, NULL, 0, descriptor, sizeof(descriptor),
        100);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(descriptor));
    g_assert_cmpuint(result.transactions, ==, 3);
    g_assert_cmpmem(descriptor, sizeof(descriptor), fixture.descriptor,
                    sizeof(descriptor));

    result = dm_usb_host_channel_control_transfer(
        &control, 0, set_address, NULL, 0, NULL, 0, 200);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.transactions, ==, 2);
    g_assert_cmpuint(device->addr, ==, 13);
    g_assert_null(usb_find_device(&fixture.host_qemu_port.port, 0));
    g_assert_true(usb_find_device(&fixture.host_qemu_port.port, 13) == device);

    result = dm_usb_host_channel_control_transfer(
        &control, 13, get_status, NULL, 0, status, sizeof(status), 300);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(status));
    g_assert_cmpuint(result.transactions, ==, 3);
    g_assert_cmpmem(status, sizeof(status), "\0\0", sizeof(status));
    fixture_cleanup_host_channel_lifecycle_qemu_transport(&fixture);
}

static void test_realize_requires_submit(void)
{
    DmUsbQemuAdapter *adapter = DM_USB_QEMU_ADAPTER(
        object_new(TYPE_DM_USB_QEMU_ADAPTER));
    USBDevice *dev = USB_DEVICE(adapter);
    USBDeviceClass *klass;
    Error *err = NULL;

    usb_ep_init(dev);
    klass = USB_DEVICE_GET_CLASS(dev);
    klass->realize(dev, &err);
    g_assert_nonnull(err);
    error_free(err);
    object_unref(OBJECT(adapter));
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    module_call_init(MODULE_INIT_QOM);
    type_register_static(&adapter_test_usb_host_type_info);
    type_register_static(&adapter_test_async_usb_device_type_info);
    g_test_add_func("/dm-usb/qemu-adapter/control-in-multi-packet",
                    test_control_in_multi_packet);
    g_test_add_func("/dm-usb/qemu-adapter/control-out-status",
                    test_control_out_and_status);
    g_test_add_func("/dm-usb/qemu-adapter/data-iov-status",
                    test_data_iov_and_status_mapping);
    g_test_add_func("/dm-usb/qemu-adapter/reset-callback",
                    test_reset_callback);
    g_test_add_func("/dm-usb/qemu-adapter/bus-attachment-routing",
                    test_bus_attachment_and_routing);
    g_test_add_func("/dm-usb/qemu-adapter/dwc2-transaction-bridge",
                    test_dwc2_transaction_bridge);
    g_test_add_func("/dm-usb/qemu-adapter/host-qemu-transport-data-status",
                    test_host_qemu_transport_data_and_status);
    g_test_add_func("/dm-usb/qemu-adapter/host-qemu-port-address-routing",
                    test_host_qemu_port_transport_address_routing);
    g_test_add_func("/dm-usb/qemu-adapter/host-qemu-transport-cancels-async",
                    test_host_qemu_transport_cancels_async);
    g_test_add_func("/dm-usb/qemu-adapter/host-qemu-transport-status-mapping",
                    test_host_qemu_transport_status_mapping);
    g_test_add_func("/dm-usb/qemu-adapter/host-qemu-transport-binding-contract",
                    test_host_qemu_transport_binding_contract);
    g_test_add_func("/dm-usb/qemu-adapter/host-qemu-port-lifecycle",
                    test_host_qemu_port_lifecycle);
    g_test_add_func("/dm-usb/qemu-adapter/host-channel-qemu-composition",
                    test_host_channel_qemu_transport_composition);
    g_test_add_func("/dm-usb/qemu-adapter/host-channel-control-lifecycle-enumeration",
                    test_host_channel_control_qemu_lifecycle_enumeration);
    g_test_add_func("/dm-usb/qemu-adapter/realize-requires-submit",
                    test_realize_requires_submit);
    return g_test_run();
}
