/* Board-independent USB device transaction dispatcher. */
#ifndef DM_USB_TRANSACTION_H
#define DM_USB_TRANSACTION_H

#include "hw/usb/dm_usb_control.h"
#include "hw/usb/dm_usb_endpoint_queue.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_USB_TRANSACTION_MAX_ENDPOINTS 16

typedef enum DmUsbTransactionToken {
    DM_USB_TRANSACTION_SETUP,
    DM_USB_TRANSACTION_IN,
    DM_USB_TRANSACTION_OUT,
} DmUsbTransactionToken;

typedef enum DmUsbTransactionPid {
    DM_USB_TRANSACTION_PID_AUTO,
    DM_USB_TRANSACTION_PID_DATA0,
    DM_USB_TRANSACTION_PID_DATA1,
} DmUsbTransactionPid;

typedef enum DmUsbTransactionStatus {
    DM_USB_TRANSACTION_ACCEPTED,
    DM_USB_TRANSACTION_NAK,
    DM_USB_TRANSACTION_STALL,
    DM_USB_TRANSACTION_INVALID,
    /* The following values are transport-facing terminal results.  Device
     * endpoint callbacks should continue to use the first four values. */
    DM_USB_TRANSACTION_NO_DEVICE,
    DM_USB_TRANSACTION_BABBLE,
    DM_USB_TRANSACTION_IO_ERROR,
    /* QEMU retained the packet for asynchronous completion. */
    DM_USB_TRANSACTION_DEFERRED,
} DmUsbTransactionStatus;

typedef struct DmUsbTransaction {
    DmUsbTransactionToken token;
    DmUsbTransactionPid pid;
    uint8_t endpoint;
    const uint8_t *out_data;
    size_t length;
    uint8_t *in_data;
    size_t capacity;
    uint64_t timestamp_ns;
} DmUsbTransaction;

typedef struct DmUsbTransactionResult {
    DmUsbTransactionStatus status;
    size_t actual_length;
    DmUsbTransactionPid next_pid;
} DmUsbTransactionResult;

typedef DmUsbTransactionStatus DmUsbTransactionIn(
    void *opaque, uint8_t endpoint, uint8_t *data, size_t capacity,
    size_t *length, uint64_t timestamp_ns);
typedef DmUsbTransactionStatus DmUsbTransactionOut(
    void *opaque, uint8_t endpoint, const uint8_t *data, size_t length,
    uint64_t timestamp_ns);

typedef struct DmUsbTransactionOps {
    DmUsbTransactionIn *in;
    DmUsbTransactionOut *out;
} DmUsbTransactionOps;

typedef struct DmUsbTransactionEndpoint {
    bool in_enabled;
    bool out_enabled;
    bool in_halted;
    bool out_halted;
    DmUsbEndpointType type;
    uint16_t max_packet_size;
    DmUsbTransactionPid next_in_pid;
    DmUsbTransactionPid next_out_pid;
} DmUsbTransactionEndpoint;

typedef struct DmUsbTransactionDevice {
    DmUsbControlDevice *control;
    DmUsbTransactionOps ops;
    void *opaque;
    DmUsbTransactionEndpoint endpoint[DM_USB_TRANSACTION_MAX_ENDPOINTS];
} DmUsbTransactionDevice;

void dm_usb_transaction_init(DmUsbTransactionDevice *device,
                             DmUsbControlDevice *control,
                             const DmUsbTransactionOps *ops, void *opaque,
                             uint16_t ep0_max_packet_size);
void dm_usb_transaction_reset(DmUsbTransactionDevice *device);

DmUsbTransactionStatus dm_usb_transaction_configure_endpoint(
    DmUsbTransactionDevice *device, uint8_t endpoint,
    DmUsbEndpointDirection direction, DmUsbEndpointType type,
    uint16_t max_packet_size, bool enabled);
DmUsbTransactionStatus dm_usb_transaction_clear_halt(
    DmUsbTransactionDevice *device, uint8_t endpoint,
    DmUsbEndpointDirection direction);
bool dm_usb_transaction_halted(const DmUsbTransactionDevice *device,
                               uint8_t endpoint,
                               DmUsbEndpointDirection direction);
DmUsbTransactionPid dm_usb_transaction_next_pid(
    const DmUsbTransactionDevice *device, uint8_t endpoint,
    DmUsbEndpointDirection direction);

DmUsbTransactionResult dm_usb_transaction_submit(
    DmUsbTransactionDevice *device, const DmUsbTransaction *transaction);

#endif /* DM_USB_TRANSACTION_H */
