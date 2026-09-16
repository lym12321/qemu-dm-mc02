/* Controller-independent completion-driven USB host channel ownership. */
#ifndef DM_USB_HOST_CHANNEL_OPERATION_H
#define DM_USB_HOST_CHANNEL_OPERATION_H

#include "dm_usb_host_channel_allocator.h"

#include <stdbool.h>

typedef enum DmUsbHostChannelOperationState {
    DM_USB_HOST_CHANNEL_OPERATION_IDLE,
    DM_USB_HOST_CHANNEL_OPERATION_PENDING,
    DM_USB_HOST_CHANNEL_OPERATION_COMPLETED,
    DM_USB_HOST_CHANNEL_OPERATION_CANCELLED,
} DmUsbHostChannelOperationState;

typedef struct DmUsbHostChannelOperation {
    DmUsbHostChannelAllocator *allocator;
    DmUsbHostChannelLease lease;
    DmUsbHostChannelOperationState state;
    bool initialized;
} DmUsbHostChannelOperation;

/* Initialize an operation before its first begin; it must not be pending. */
bool dm_usb_host_channel_operation_init(
    DmUsbHostChannelOperation *operation);

/* Acquire a channel and keep its lease until complete() or cancel(). */
bool dm_usb_host_channel_operation_begin(
    DmUsbHostChannelOperation *operation,
    DmUsbHostChannelAllocator *allocator);

/* Query whether the operation still owns a live channel. */
bool dm_usb_host_channel_operation_pending(
    const DmUsbHostChannelOperation *operation);

/* Borrow the active lease for a controller transfer callback. */
DmUsbHostChannelLease *dm_usb_host_channel_operation_lease(
    DmUsbHostChannelOperation *operation);

/* Release the channel after the controller reports completion. */
bool dm_usb_host_channel_operation_complete(
    DmUsbHostChannelOperation *operation);

/* Release the channel when the controller or caller cancels the operation. */
bool dm_usb_host_channel_operation_cancel(
    DmUsbHostChannelOperation *operation);

#endif /* DM_USB_HOST_CHANNEL_OPERATION_H */
