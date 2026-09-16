/* Controller-independent fixed-capacity USB host channel leases. */
#include "dm_usb_host_channel_allocator.h"

#include <stddef.h>

bool dm_usb_host_channel_allocator_init(
    DmUsbHostChannelAllocator *allocator, const uint8_t *channels,
    unsigned channel_count)
{
    unsigned index;
    unsigned prior;

    if (!allocator || channel_count > DM_USB_HOST_CHANNEL_ALLOCATOR_CAPACITY ||
        (channel_count && !channels)) {
        return false;
    }
    for (index = 0; index < channel_count; ++index) {
        for (prior = 0; prior < index; ++prior) {
            if (channels[prior] == channels[index]) {
                return false;
            }
        }
    }
    for (index = 0; index < channel_count; ++index) {
        allocator->entry[index] = (DmUsbHostChannelAllocatorEntry) {
            .channel = channels[index],
            .generation = 0,
            .owner = NULL,
        };
    }
    allocator->count = channel_count;
    return true;
}

bool dm_usb_host_channel_allocator_acquire(
    DmUsbHostChannelAllocator *allocator, const void *owner,
    DmUsbHostChannelLease *lease)
{
    unsigned index;

    if (!allocator || !owner || !lease) {
        return false;
    }
    for (index = 0; index < allocator->count; ++index) {
        DmUsbHostChannelAllocatorEntry *entry = &allocator->entry[index];

        if (entry->owner) {
            continue;
        }
        ++entry->generation;
        if (!entry->generation) {
            ++entry->generation;
        }
        entry->owner = owner;
        *lease = (DmUsbHostChannelLease) {
            .channel = entry->channel,
            .generation = entry->generation,
            .owner = owner,
        };
        return true;
    }
    return false;
}

bool dm_usb_host_channel_allocator_lease_active(
    const DmUsbHostChannelAllocator *allocator,
    const DmUsbHostChannelLease *lease)
{
    unsigned index;

    if (!allocator || !lease || !lease->owner || !lease->generation) {
        return false;
    }
    for (index = 0; index < allocator->count; ++index) {
        const DmUsbHostChannelAllocatorEntry *entry = &allocator->entry[index];

        if (entry->channel == lease->channel && entry->owner == lease->owner &&
            entry->generation == lease->generation) {
            return true;
        }
    }
    return false;
}

bool dm_usb_host_channel_allocator_release(
    DmUsbHostChannelAllocator *allocator, DmUsbHostChannelLease *lease)
{
    unsigned index;

    if (!dm_usb_host_channel_allocator_lease_active(allocator, lease)) {
        return false;
    }
    for (index = 0; index < allocator->count; ++index) {
        DmUsbHostChannelAllocatorEntry *entry = &allocator->entry[index];

        if (entry->channel != lease->channel || entry->owner != lease->owner ||
            entry->generation != lease->generation) {
            continue;
        }
        entry->owner = NULL;
        *lease = (DmUsbHostChannelLease) { 0 };
        return true;
    }
    return false;
}
