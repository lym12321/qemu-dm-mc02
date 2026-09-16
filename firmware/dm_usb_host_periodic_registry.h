/* Controller-independent fixed-capacity periodic USB endpoint registry. */
#ifndef DM_USB_HOST_PERIODIC_REGISTRY_H
#define DM_USB_HOST_PERIODIC_REGISTRY_H

#include "dm_usb_host_periodic_poller.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_USB_HOST_PERIODIC_REGISTRY_CAPACITY 16u

typedef void DmUsbHostPeriodicRegistryCompletion(
    void *opaque, DmUsbHostEndpointCompletion completion);

typedef struct DmUsbHostPeriodicRegistryEntry {
    DmUsbHostPeriodicPoller *poller;
    const uint8_t *out_data;
    size_t out_length;
    uint8_t *in_data;
    size_t in_capacity;
    DmUsbHostPeriodicRegistryCompletion *completion;
    void *completion_opaque;
} DmUsbHostPeriodicRegistryEntry;

typedef struct DmUsbHostPeriodicRegistry {
    DmUsbHostSpeed speed;
    unsigned count;
    DmUsbHostPeriodicRegistryEntry entry[DM_USB_HOST_PERIODIC_REGISTRY_CAPACITY];
} DmUsbHostPeriodicRegistry;

void dm_usb_host_periodic_registry_init(DmUsbHostPeriodicRegistry *registry,
                                        DmUsbHostSpeed speed);

/* Copy one caller-owned endpoint binding into the active periodic registry. */
bool dm_usb_host_periodic_registry_add(
    DmUsbHostPeriodicRegistry *registry,
    const DmUsbHostPeriodicRegistryEntry *entry);

bool dm_usb_host_periodic_registry_remove(
    DmUsbHostPeriodicRegistry *registry, DmUsbHostPeriodicPoller *poller);

/* Dispatch each due endpoint once for an observed shared virtual timestamp. */
unsigned dm_usb_host_periodic_registry_dispatch(
    DmUsbHostPeriodicRegistry *registry, uint64_t timestamp_ns);

#endif /* DM_USB_HOST_PERIODIC_REGISTRY_H */
