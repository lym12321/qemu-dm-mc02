/* Controller-independent completion-driven USB host channel ownership. */
#include "dm_usb_host_channel_operation.h"

#include <stddef.h>

static bool dm_usb_host_channel_operation_end(
    DmUsbHostChannelOperation *operation,
    DmUsbHostChannelOperationState state)
{
    if (!dm_usb_host_channel_operation_pending(operation)) {
        return false;
    }
    if (!dm_usb_host_channel_allocator_release(operation->allocator,
                                               &operation->lease)) {
        return false;
    }
    operation->allocator = NULL;
    operation->state = state;
    return true;
}

bool dm_usb_host_channel_operation_init(
    DmUsbHostChannelOperation *operation)
{
    if (!operation) {
        return false;
    }
    *operation = (DmUsbHostChannelOperation) {
        .state = DM_USB_HOST_CHANNEL_OPERATION_IDLE,
        .initialized = true,
    };
    return true;
}

bool dm_usb_host_channel_operation_begin(
    DmUsbHostChannelOperation *operation,
    DmUsbHostChannelAllocator *allocator)
{
    if (!operation || !operation->initialized || !allocator ||
        operation->state == DM_USB_HOST_CHANNEL_OPERATION_PENDING ||
        (operation->state != DM_USB_HOST_CHANNEL_OPERATION_IDLE &&
         operation->state != DM_USB_HOST_CHANNEL_OPERATION_COMPLETED &&
         operation->state != DM_USB_HOST_CHANNEL_OPERATION_CANCELLED)) {
        return false;
    }
    if (!dm_usb_host_channel_allocator_acquire(
            allocator, operation, &operation->lease)) {
        return false;
    }
    operation->allocator = allocator;
    operation->state = DM_USB_HOST_CHANNEL_OPERATION_PENDING;
    return true;
}

bool dm_usb_host_channel_operation_pending(
    const DmUsbHostChannelOperation *operation)
{
    return operation && operation->initialized &&
           operation->state == DM_USB_HOST_CHANNEL_OPERATION_PENDING &&
           operation->allocator &&
           dm_usb_host_channel_allocator_lease_active(operation->allocator,
                                                       &operation->lease);
}

DmUsbHostChannelLease *dm_usb_host_channel_operation_lease(
    DmUsbHostChannelOperation *operation)
{
    return dm_usb_host_channel_operation_pending(operation) ?
           &operation->lease : NULL;
}

bool dm_usb_host_channel_operation_complete(
    DmUsbHostChannelOperation *operation)
{
    return dm_usb_host_channel_operation_end(
        operation, DM_USB_HOST_CHANNEL_OPERATION_COMPLETED);
}

bool dm_usb_host_channel_operation_cancel(
    DmUsbHostChannelOperation *operation)
{
    return dm_usb_host_channel_operation_end(
        operation, DM_USB_HOST_CHANNEL_OPERATION_CANCELLED);
}
