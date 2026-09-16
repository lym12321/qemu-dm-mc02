/* Controller-independent fixed-capacity USB host channel leases. */
#ifndef DM_USB_HOST_CHANNEL_ALLOCATOR_H
#define DM_USB_HOST_CHANNEL_ALLOCATOR_H

#include <stdbool.h>
#include <stdint.h>

#define DM_USB_HOST_CHANNEL_ALLOCATOR_CAPACITY 16u

typedef struct DmUsbHostChannelLease {
    uint8_t channel;
    uint32_t generation;
    const void *owner;
} DmUsbHostChannelLease;

typedef struct DmUsbHostChannelAllocatorEntry {
    uint8_t channel;
    uint32_t generation;
    const void *owner;
} DmUsbHostChannelAllocatorEntry;

typedef struct DmUsbHostChannelAllocator {
    unsigned count;
    DmUsbHostChannelAllocatorEntry
        entry[DM_USB_HOST_CHANNEL_ALLOCATOR_CAPACITY];
} DmUsbHostChannelAllocator;

/* Copy a unique, caller-defined channel-ID set. Reinitialization invalidates leases. */
bool dm_usb_host_channel_allocator_init(
    DmUsbHostChannelAllocator *allocator, const uint8_t *channels,
    unsigned channel_count);

/* Lease the first available channel in initialization order for one owner. */
bool dm_usb_host_channel_allocator_acquire(
    DmUsbHostChannelAllocator *allocator, const void *owner,
    DmUsbHostChannelLease *lease);

/* Check that a lease still identifies its active allocator entry. */
bool dm_usb_host_channel_allocator_lease_active(
    const DmUsbHostChannelAllocator *allocator,
    const DmUsbHostChannelLease *lease);

/* Return one active lease and invalidate the caller's handle. */
bool dm_usb_host_channel_allocator_release(
    DmUsbHostChannelAllocator *allocator, DmUsbHostChannelLease *lease);

#endif /* DM_USB_HOST_CHANNEL_ALLOCATOR_H */
