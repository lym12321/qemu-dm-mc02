/* Synchronous, board-independent adapter for QEMU USB request/data APIs. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_qemu_adapter.h"
#include "qapi/error.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qom/object.h"

#define DM_USB_QEMU_ADAPTER_MAX_PACKET_SIZE 64
static int dm_usb_qemu_adapter_map_status(DmUsbTransactionStatus status)
{
    switch (status) {
    case DM_USB_TRANSACTION_ACCEPTED:
        return USB_RET_SUCCESS;
    case DM_USB_TRANSACTION_NAK:
        return USB_RET_NAK;
    case DM_USB_TRANSACTION_STALL:
        return USB_RET_STALL;
    case DM_USB_TRANSACTION_NO_DEVICE:
        return USB_RET_NODEV;
    case DM_USB_TRANSACTION_BABBLE:
        return USB_RET_BABBLE;
    case DM_USB_TRANSACTION_IO_ERROR:
        return USB_RET_IOERROR;
    case DM_USB_TRANSACTION_DEFERRED:
        /* This adapter owns a synchronous callback and has no packet
         * completion owner.  A deferred lower result is therefore an
         * adapter error, not a QEMU async packet. */
        return USB_RET_IOERROR;
    case DM_USB_TRANSACTION_INVALID:
    default:
        return USB_RET_IOERROR;
    }
}

static int dm_usb_qemu_adapter_map_control_status(DmUsbControlResult status)
{
    switch (status) {
    case DM_USB_CONTROL_ACCEPTED:
        return USB_RET_SUCCESS;
    case DM_USB_CONTROL_STALL:
        return USB_RET_STALL;
    case DM_USB_CONTROL_INVALID:
    default:
        return USB_RET_IOERROR;
    }
}

static DmUsbTransactionResult dm_usb_qemu_adapter_submit(
    DmUsbQemuAdapter *adapter, DmUsbTransaction *transaction)
{
    DmUsbTransactionResult result;

    if (!adapter->submit || adapter->submitting) {
        return (DmUsbTransactionResult) {
            .status = DM_USB_TRANSACTION_INVALID,
        };
    }
    transaction->timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    adapter->submitting = true;
    result = adapter->submit(adapter->opaque, transaction);
    adapter->submitting = false;
    return result;
}

static void dm_usb_qemu_adapter_commit_address(USBDevice *dev,
                                                const DmUsbControlRequest *request)
{
    if (request->request_type ==
            (USB_DIR_OUT | USB_TYPE_STANDARD | USB_RECIP_DEVICE) &&
        request->request == USB_REQ_SET_ADDRESS && request->value <= 0x7f &&
        request->index == 0 && request->length == 0) {
        dev->addr = request->value;
    }
}

static void dm_usb_qemu_adapter_handle_control(
    USBDevice *dev, USBPacket *p, int request, int value, int index,
    int length, uint8_t *data)
{
    DmUsbQemuAdapter *adapter = DM_USB_QEMU_ADAPTER(dev);
    DmUsbControlRequest control_request;
    DmUsbControlResult result;
    size_t actual_length = 0;

    p->actual_length = 0;
    if (!adapter->control_submit || adapter->submitting ||
        request < 0 || request > UINT16_MAX ||
        value < 0 || value > UINT16_MAX ||
        index < 0 || index > UINT16_MAX ||
        length < 0 || length > UINT16_MAX ||
        length > sizeof(dev->data_buf) || (length && !data)) {
        p->status = USB_RET_IOERROR;
        return;
    }

    control_request = (DmUsbControlRequest) {
        .request_type = (uint16_t)request >> 8,
        .request = request,
        .value = value,
        .index = index,
        .length = length,
    };
    adapter->submitting = true;
    result = adapter->control_submit(
        adapter->opaque, &control_request,
        control_request.request_type & USB_DIR_IN ? NULL : data,
        control_request.request_type & USB_DIR_IN ? 0 : (size_t)length,
        control_request.request_type & USB_DIR_IN ? data : NULL,
        control_request.request_type & USB_DIR_IN ? (size_t)length : 0,
        &actual_length, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    adapter->submitting = false;
    if (result != DM_USB_CONTROL_ACCEPTED ||
        (!(control_request.request_type & USB_DIR_IN) && actual_length) ||
        ((control_request.request_type & USB_DIR_IN) &&
         actual_length > (size_t)length)) {
        p->status = dm_usb_qemu_adapter_map_control_status(result);
        return;
    }

    dm_usb_qemu_adapter_commit_address(dev, &control_request);
    p->actual_length = actual_length;
    p->status = USB_RET_SUCCESS;
}

static void dm_usb_qemu_adapter_handle_data(USBDevice *dev, USBPacket *p)
{
    DmUsbQemuAdapter *adapter = DM_USB_QEMU_ADAPTER(dev);
    uint8_t *buffer = dev->data_buf;
    DmUsbTransaction transaction = {
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = p->ep ? p->ep->nr : 0,
    };
    DmUsbTransactionResult result;
    size_t packet_size;

    p->actual_length = 0;
    if (adapter->submitting) {
        p->status = USB_RET_IOERROR;
        return;
    }
    packet_size = usb_packet_size(p);
    if (!p->ep || p->ep->nr == 0 || p->ep->nr > USB_MAX_ENDPOINTS ||
        packet_size > sizeof(dev->data_buf)) {
        p->status = USB_RET_IOERROR;
        return;
    }

    switch (p->pid) {
    case USB_TOKEN_IN:
        transaction.token = DM_USB_TRANSACTION_IN;
        transaction.in_data = buffer;
        transaction.capacity = packet_size;
        result = dm_usb_qemu_adapter_submit(adapter, &transaction);
        if (result.status == DM_USB_TRANSACTION_ACCEPTED) {
            if (result.actual_length > packet_size) {
                p->status = USB_RET_IOERROR;
                return;
            }
            usb_packet_copy(p, buffer, result.actual_length);
        }
        break;
    case USB_TOKEN_OUT:
        transaction.token = DM_USB_TRANSACTION_OUT;
        transaction.out_data = buffer;
        transaction.length = packet_size;
        usb_packet_copy(p, buffer, packet_size);
        result = dm_usb_qemu_adapter_submit(adapter, &transaction);
        break;
    default:
        p->status = USB_RET_IOERROR;
        return;
    }

    if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
        p->actual_length = 0;
    }
    p->status = dm_usb_qemu_adapter_map_status(result.status);
}

static void dm_usb_qemu_adapter_handle_reset(USBDevice *dev)
{
    DmUsbQemuAdapter *adapter = DM_USB_QEMU_ADAPTER(dev);

    dm_usb_host_port_reset(&adapter->host_port,
                           qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static void dm_usb_qemu_adapter_realize(USBDevice *dev, Error **errp)
{
    DmUsbQemuAdapter *adapter = DM_USB_QEMU_ADAPTER(dev);

    if (!adapter->submit || !adapter->control_submit) {
        error_setg(errp,
                   "dm-usb-qemu-adapter requires submit and control callbacks");
        return;
    }

    /* The default transaction contract uses a 64-byte bulk packet. */
    dev->speedmask = USB_SPEED_MASK_FULL;
    dev->ep_ctl.max_packet_size = DM_USB_QEMU_ADAPTER_MAX_PACKET_SIZE;
    for (unsigned i = 0; i < USB_MAX_ENDPOINTS; ++i) {
        dev->ep_in[i].type = USB_ENDPOINT_XFER_BULK;
        dev->ep_in[i].max_packet_size =
            DM_USB_QEMU_ADAPTER_MAX_PACKET_SIZE;
        dev->ep_out[i].type = USB_ENDPOINT_XFER_BULK;
        dev->ep_out[i].max_packet_size =
            DM_USB_QEMU_ADAPTER_MAX_PACKET_SIZE;
    }
}

void dm_usb_qemu_adapter_set_callbacks(DmUsbQemuAdapter *adapter,
                                        DmUsbQemuSubmit *submit,
                                        DmUsbQemuBusReset *reset,
                                        void *opaque)
{
    if (!adapter) {
        return;
    }
    adapter->submit = submit;
    adapter->opaque = opaque;
    dm_usb_host_port_init(&adapter->host_port, reset, opaque);
}

void dm_usb_qemu_adapter_set_control_callback(
    DmUsbQemuAdapter *adapter, DmUsbQemuControlSubmit *control_submit)
{
    if (!adapter) {
        return;
    }
    adapter->control_submit = control_submit;
}

static void dm_usb_qemu_adapter_class_init(ObjectClass *klass, void *data)
{
    USBDeviceClass *uc = USB_DEVICE_CLASS(klass);

    uc->realize = dm_usb_qemu_adapter_realize;
    uc->product_desc = "DM USB QEMU Adapter";
    uc->handle_reset = dm_usb_qemu_adapter_handle_reset;
    uc->handle_control = dm_usb_qemu_adapter_handle_control;
    uc->handle_data = dm_usb_qemu_adapter_handle_data;
}

static const TypeInfo dm_usb_qemu_adapter_type_info = {
    .name = TYPE_DM_USB_QEMU_ADAPTER,
    .parent = TYPE_USB_DEVICE,
    .instance_size = sizeof(DmUsbQemuAdapter),
    .class_init = dm_usb_qemu_adapter_class_init,
};

static void dm_usb_qemu_adapter_register_types(void)
{
    type_register_static(&dm_usb_qemu_adapter_type_info);
}

type_init(dm_usb_qemu_adapter_register_types)
