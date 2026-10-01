/* Synchronous, board-independent adapter for a DM USB device transaction API. */
#ifndef DM_USB_QEMU_ADAPTER_H
#define DM_USB_QEMU_ADAPTER_H

#include "hw/usb.h"
#include "hw/usb/dm_usb_host_port.h"
#include "hw/usb/dm_usb_transaction.h"

#include <stddef.h>

#define TYPE_DM_USB_QEMU_ADAPTER "dm-usb-qemu-adapter"
OBJECT_DECLARE_SIMPLE_TYPE(DmUsbQemuAdapter, DM_USB_QEMU_ADAPTER)

typedef DmUsbTransactionResult DmUsbQemuSubmit(
    void *opaque, const DmUsbTransaction *transaction);
/* QEMU exposes control devices at this complete-request boundary.  The
 * callback must return synchronously; it receives a parsed request and the
 * complete data stage, never individual SETUP/DATA/STATUS tokens. */
typedef DmUsbControlResult DmUsbQemuControlSubmit(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length, uint64_t timestamp_ns);
typedef DmUsbHostPortReset DmUsbQemuBusReset;

struct DmUsbQemuAdapter {
    USBDevice parent_obj;

    DmUsbQemuSubmit *submit;
    DmUsbQemuControlSubmit *control_submit;
    DmUsbHostPort host_port;
    void *opaque;
    bool submitting;
};

void dm_usb_qemu_adapter_set_callbacks(DmUsbQemuAdapter *adapter,
                                        DmUsbQemuSubmit *submit,
                                        DmUsbQemuBusReset *reset,
                                        void *opaque);
void dm_usb_qemu_adapter_set_control_callback(
    DmUsbQemuAdapter *adapter, DmUsbQemuControlSubmit *control_submit);

#endif /* DM_USB_QEMU_ADAPTER_H */
