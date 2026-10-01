/* QEMU USBDevice binding for the generic synchronous host transaction API. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_qemu_transport.h"

static DmUsbTransactionStatus dm_usb_host_qemu_transport_status(int status)
{
    switch (status) {
    case USB_RET_SUCCESS:
        return DM_USB_TRANSACTION_ACCEPTED;
    case USB_RET_NAK:
        return DM_USB_TRANSACTION_NAK;
    case USB_RET_STALL:
        return DM_USB_TRANSACTION_STALL;
    case USB_RET_NODEV:
        return DM_USB_TRANSACTION_NO_DEVICE;
    case USB_RET_BABBLE:
        return DM_USB_TRANSACTION_BABBLE;
    case USB_RET_IOERROR:
        return DM_USB_TRANSACTION_IO_ERROR;
    case USB_RET_ASYNC:
    case USB_RET_ADD_TO_QUEUE:
        return DM_USB_TRANSACTION_DEFERRED;
    default:
        return DM_USB_TRANSACTION_INVALID;
    }
}

static bool dm_usb_host_qemu_transport_packet(
    const DmUsbTransaction *transaction, int *pid, USBEndpoint **endpoint,
    uint8_t **data, size_t *length, USBDevice *device)
{
    if (!transaction || !pid || !endpoint || !data || !length || !device) {
        return false;
    }
    switch (transaction->token) {
    case DM_USB_TRANSACTION_SETUP:
        if (transaction->endpoint || transaction->length != 8 ||
            !transaction->out_data) {
            return false;
        }
        *pid = USB_TOKEN_SETUP;
        *data = (uint8_t *)transaction->out_data;
        *length = transaction->length;
        break;
    case DM_USB_TRANSACTION_IN:
        if (transaction->endpoint > USB_MAX_ENDPOINTS ||
            (transaction->capacity && !transaction->in_data)) {
            return false;
        }
        *pid = USB_TOKEN_IN;
        *data = transaction->in_data;
        *length = transaction->capacity;
        break;
    case DM_USB_TRANSACTION_OUT:
        if (transaction->endpoint > USB_MAX_ENDPOINTS ||
            (transaction->length && !transaction->out_data)) {
            return false;
        }
        *pid = USB_TOKEN_OUT;
        *data = (uint8_t *)transaction->out_data;
        *length = transaction->length;
        break;
    default:
        return false;
    }

    *endpoint = usb_ep_get(device, *pid, transaction->endpoint);
    return true;
}

void dm_usb_host_qemu_transport_init(DmUsbHostQemuTransport *transport,
                                     USBDevice *device)
{
    if (!transport) {
        return;
    }
    *transport = (DmUsbHostQemuTransport) {
        .device = device,
    };
}

void dm_usb_host_qemu_transport_init_port(DmUsbHostQemuTransport *transport,
                                          USBPort *port)
{
    if (!transport) {
        return;
    }
    *transport = (DmUsbHostQemuTransport) {
        .port = port,
    };
}

void dm_usb_host_qemu_transport_clear(DmUsbHostQemuTransport *transport)
{
    if (transport) {
        *transport = (DmUsbHostQemuTransport) { 0 };
    }
}

static DmUsbTransactionResult dm_usb_host_qemu_transport_submit_device(
    USBDevice *device, const DmUsbTransaction *transaction)
{
    DmUsbTransactionResult result = {
        .status = DM_USB_TRANSACTION_INVALID,
    };
    USBPacket packet;
    USBEndpoint *endpoint;
    uint8_t *data;
    size_t length;
    int pid;

    if (!device || !device->attached || device->state != USB_STATE_DEFAULT ||
        !dm_usb_host_qemu_transport_packet(
                                 transaction, &pid, &endpoint, &data, &length,
                                 device)) {
        if (!device || !device->attached ||
            device->state != USB_STATE_DEFAULT) {
            result.status = DM_USB_TRANSACTION_NO_DEVICE;
        }
        return result;
    }

    usb_packet_init(&packet);
    usb_packet_setup(&packet, pid, endpoint, 0, 0, false, false);
    if (length) {
        usb_packet_addbuf(&packet, data, length);
    }
    usb_handle_packet(device, &packet);
    if (usb_packet_is_inflight(&packet)) {
        result.status = dm_usb_host_qemu_transport_status(packet.status);
        usb_cancel_packet(&packet);
    } else {
        result.status = dm_usb_host_qemu_transport_status(packet.status);
        if (result.status == DM_USB_TRANSACTION_ACCEPTED &&
            packet.actual_length >= 0 && packet.actual_length <= length) {
            result.actual_length = packet.actual_length;
        } else if (result.status == DM_USB_TRANSACTION_ACCEPTED) {
            result.status = DM_USB_TRANSACTION_INVALID;
        }
    }
    usb_packet_cleanup(&packet);
    return result;
}

DmUsbTransactionResult dm_usb_host_qemu_transport_submit(
    void *opaque, const DmUsbTransaction *transaction)
{
    DmUsbHostQemuTransport *transport = opaque;

    if (!transport || transport->port || !transport->device) {
        return (DmUsbTransactionResult) {
            .status = DM_USB_TRANSACTION_INVALID,
        };
    }
    return dm_usb_host_qemu_transport_submit_device(transport->device,
                                                     transaction);
}

DmUsbTransactionResult dm_usb_host_qemu_transport_route(
    void *opaque, uint8_t device_address,
    const DmUsbTransaction *transaction)
{
    DmUsbHostQemuTransport *transport = opaque;

    if (!transport || transport->device || !transport->port) {
        return (DmUsbTransactionResult) {
            .status = DM_USB_TRANSACTION_INVALID,
        };
    }
    return dm_usb_host_qemu_transport_submit_device(
        transport->port ? usb_find_device(transport->port, device_address) :
                          NULL,
        transaction);
}
