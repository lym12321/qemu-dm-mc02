/* QEMU USBDevice binding for the generic synchronous host transaction API. */
#ifndef HW_USB_DM_USB_HOST_QEMU_TRANSPORT_H
#define HW_USB_DM_USB_HOST_QEMU_TRANSPORT_H

#include "hw/usb.h"
#include "hw/usb/dm_usb_transaction.h"

typedef struct DmUsbHostQemuTransport {
    /* These are borrowed QEMU objects.  The owner must clear this adapter
     * before releasing the device, port, or bus they belong to. */
    USBDevice *device;
    USBPort *port;
} DmUsbHostQemuTransport;

void dm_usb_host_qemu_transport_init(DmUsbHostQemuTransport *transport,
                                     USBDevice *device);
void dm_usb_host_qemu_transport_init_port(DmUsbHostQemuTransport *transport,
                                          USBPort *port);
void dm_usb_host_qemu_transport_clear(DmUsbHostQemuTransport *transport);
DmUsbTransactionResult dm_usb_host_qemu_transport_submit(
    void *opaque, const DmUsbTransaction *transaction);
DmUsbTransactionResult dm_usb_host_qemu_transport_route(
    void *opaque, uint8_t device_address,
    const DmUsbTransaction *transaction);

#endif /* HW_USB_DM_USB_HOST_QEMU_TRANSPORT_H */
