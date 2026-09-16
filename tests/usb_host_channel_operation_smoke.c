#include "dm_usb_host_channel_operation.h"

#include <stdbool.h>
#include <stdio.h>

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

int main(void)
{
    uint8_t channels[] = { 4, 7 };
    DmUsbHostChannelAllocator allocator;
    DmUsbHostChannelOperation first = { 0 };
    DmUsbHostChannelOperation second = { 0 };
    DmUsbHostChannelOperation third = { 0 };

    if (expect(!dm_usb_host_channel_operation_init(NULL),
               "reject null operation init") ||
        expect(dm_usb_host_channel_allocator_init(
                   &allocator, channels, 2),
               "initialize allocator") ||
        expect(dm_usb_host_channel_operation_init(&first) &&
                   dm_usb_host_channel_operation_init(&second) &&
                   dm_usb_host_channel_operation_init(&third),
               "initialize operations") ||
        expect(!dm_usb_host_channel_operation_pending(&first) &&
                   !dm_usb_host_channel_operation_lease(&first) &&
                   !dm_usb_host_channel_operation_complete(&first) &&
                   !dm_usb_host_channel_operation_cancel(&first),
               "idle operation has no lease lifecycle") ||
        expect(dm_usb_host_channel_operation_begin(&first, &allocator) &&
                   dm_usb_host_channel_operation_pending(&first) &&
                   first.lease.channel == 4 &&
                   dm_usb_host_channel_operation_lease(&first) ==
                       &first.lease &&
                   !dm_usb_host_channel_operation_begin(&first, &allocator),
               "begin holds the first channel until terminal event") ||
        expect(dm_usb_host_channel_operation_begin(&second, &allocator) &&
                   second.lease.channel == 7 &&
                   dm_usb_host_channel_operation_pending(&second),
               "independent operation owns a second channel") ||
        expect(!dm_usb_host_channel_operation_begin(&third, &allocator) &&
                   !dm_usb_host_channel_operation_pending(&third),
               "resource exhaustion leaves idle operation unchanged")) {
        return 1;
    }

    if (expect(dm_usb_host_channel_operation_complete(&first) &&
                   first.state == DM_USB_HOST_CHANNEL_OPERATION_COMPLETED &&
                   !dm_usb_host_channel_operation_pending(&first) &&
                   !first.lease.owner && !allocator.entry[0].owner &&
                   !dm_usb_host_channel_operation_complete(&first),
               "completion releases and terminalizes the operation") ||
        expect(dm_usb_host_channel_operation_cancel(&second) &&
                   second.state == DM_USB_HOST_CHANNEL_OPERATION_CANCELLED &&
                   !dm_usb_host_channel_operation_pending(&second) &&
                   !second.lease.owner && !allocator.entry[1].owner &&
                   !dm_usb_host_channel_operation_cancel(&second),
               "cancellation releases and terminalizes the operation") ||
        expect(dm_usb_host_channel_operation_begin(&first, &allocator) &&
                   first.lease.channel == 4 &&
                   dm_usb_host_channel_operation_complete(&first),
               "completed operation can be reused") ||
        expect(dm_usb_host_channel_operation_begin(&second, &allocator) &&
                   second.lease.channel == 4 &&
                   dm_usb_host_channel_operation_cancel(&second),
               "cancelled operation can be reused")) {
        return 1;
    }

    puts("RESULT: USB host channel operation smoke passed");
    return 0;
}
