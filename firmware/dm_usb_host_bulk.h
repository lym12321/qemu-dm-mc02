/* Controller-independent multi-packet USB bulk transfer composition. */
#ifndef DM_USB_HOST_BULK_H
#define DM_USB_HOST_BULK_H

#include "dm_usb_host_endpoint_state.h"
#include "dm_usb_host_retry.h"

#include <stdbool.h>
#include <stddef.h>

typedef enum DmUsbHostBulkSubmitResult {
    DM_USB_HOST_BULK_SUBMIT_OK,
    /* No packet was submitted; the caller may retry the same packet. */
    DM_USB_HOST_BULK_SUBMIT_DEFERRED,
    DM_USB_HOST_BULK_SUBMIT_INVALID,
} DmUsbHostBulkSubmitResult;

typedef enum DmUsbHostBulkTransferResult {
    DM_USB_HOST_BULK_OK,
    DM_USB_HOST_BULK_NAK,
    DM_USB_HOST_BULK_STALL,
    DM_USB_HOST_BULK_TRANSACTION_ERROR,
    DM_USB_HOST_BULK_DEFERRED,
    /* A NAK consumed retry budget; call again at a later virtual timestamp. */
    DM_USB_HOST_BULK_RETRY,
    DM_USB_HOST_BULK_RETRY_EXHAUSTED,
    DM_USB_HOST_BULK_RETRY_TIMEOUT,
    DM_USB_HOST_BULK_INVALID,
} DmUsbHostBulkTransferResult;

typedef DmUsbHostBulkSubmitResult DmUsbHostBulkSubmit(
    void *opaque, const DmUsbHostPipe *pipe, DmUsbHostEndpointDataPid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion,
    size_t *actual_length);

typedef struct DmUsbHostBulkTransfer {
    DmUsbHostEndpointState *endpoint_state;
    DmUsbHostBulkSubmit *submit;
    void *submit_opaque;
} DmUsbHostBulkTransfer;

/* Bind a bulk endpoint state to a controller-neutral one-packet submitter. */
bool dm_usb_host_bulk_transfer_init(
    DmUsbHostBulkTransfer *transfer, DmUsbHostEndpointState *endpoint_state,
    DmUsbHostBulkSubmit *submit, void *submit_opaque);

/* Execute a synchronous multi-packet bulk request without retry. */
DmUsbHostBulkTransferResult dm_usb_host_bulk_transfer_run(
    DmUsbHostBulkTransfer *transfer, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, size_t *actual_length);

/* Continue a bulk request with virtual-time NAK retry and no busy wait. */
DmUsbHostBulkTransferResult dm_usb_host_bulk_transfer_run_with_retry(
    DmUsbHostBulkTransfer *transfer, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, DmUsbHostRetryPolicy *retry_policy,
    uint64_t timestamp_ns, size_t *actual_length);

#endif /* DM_USB_HOST_BULK_H */
