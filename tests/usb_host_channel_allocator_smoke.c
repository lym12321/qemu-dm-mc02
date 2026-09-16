#include "dm_usb_host_channel_allocator.h"

#include <stdbool.h>
#include <stdint.h>
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
    uint8_t channels[] = { 9, 0, 255 };
    uint8_t duplicate_channels[] = { 3, 3 };
    DmUsbHostChannelAllocator allocator = {
        .count = 1,
        .entry = { {
            .channel = 99,
            .generation = 7,
            .owner = (const void *)(uintptr_t)1,
        } },
    };
    DmUsbHostChannelLease first = { 0 };
    DmUsbHostChannelLease second = { 0 };
    DmUsbHostChannelLease third = { 0 };
    DmUsbHostChannelLease full = {
        .channel = 42,
        .generation = 9,
        .owner = (const void *)(uintptr_t)1,
    };
    DmUsbHostChannelLease stale;
    DmUsbHostChannelLease forged;
    int owner_a;
    int owner_b;
    int owner_c;
    int owner_d;

    if (expect(!dm_usb_host_channel_allocator_init(
                   &allocator, duplicate_channels, 2) &&
                   allocator.count == 1 && allocator.entry[0].channel == 99 &&
                   allocator.entry[0].generation == 7,
               "reject duplicate IDs without mutating allocator") ||
        expect(!dm_usb_host_channel_allocator_init(
                   &allocator, NULL, 1) && allocator.count == 1,
               "reject missing nonempty channel list") ||
        expect(!dm_usb_host_channel_allocator_init(
                   &allocator, channels,
                   DM_USB_HOST_CHANNEL_ALLOCATOR_CAPACITY + 1) &&
                   allocator.count == 1,
               "reject oversized channel list") ||
        expect(dm_usb_host_channel_allocator_init(&allocator, channels, 3) &&
                   allocator.count == 3 && allocator.entry[0].channel == 9 &&
                   allocator.entry[1].channel == 0 &&
                   allocator.entry[2].channel == 255,
               "copy arbitrary unique channel IDs in caller order")) {
        return 1;
    }

    if (expect(dm_usb_host_channel_allocator_acquire(
                   &allocator, &owner_a, &first) && first.channel == 9 &&
                   dm_usb_host_channel_allocator_acquire(
                       &allocator, &owner_b, &second) && second.channel == 0 &&
                   dm_usb_host_channel_allocator_acquire(
                       &allocator, &owner_c, &third) && third.channel == 255 &&
                   !dm_usb_host_channel_allocator_acquire(
                       &allocator, &owner_d, &full) && full.channel == 42 &&
                   full.generation == 9,
               "allocate deterministically and preserve output when full")) {
        return 1;
    }

    stale = first;
    forged = first;
    forged.owner = &owner_d;
    if (expect(dm_usb_host_channel_allocator_lease_active(&allocator, &first) &&
                   !dm_usb_host_channel_allocator_release(&allocator, &forged) &&
                   dm_usb_host_channel_allocator_release(&allocator, &first) &&
                   !dm_usb_host_channel_allocator_lease_active(&allocator, &first) &&
                   !first.owner && !first.generation,
               "release verifies owner and invalidates handle")) {
        return 1;
    }

    if (expect(dm_usb_host_channel_allocator_acquire(
                   &allocator, &owner_a, &first) && first.channel == 9 &&
                   first.generation != stale.generation &&
                   !dm_usb_host_channel_allocator_lease_active(&allocator, &stale) &&
                   !dm_usb_host_channel_allocator_release(&allocator, &stale) &&
                   dm_usb_host_channel_allocator_release(&allocator, &first) &&
                   dm_usb_host_channel_allocator_release(&allocator, &second) &&
                   dm_usb_host_channel_allocator_release(&allocator, &third),
               "reject stale lease after a channel is reused")) {
        return 1;
    }

    puts("RESULT: USB host channel allocator smoke passed");
    return 0;
}
