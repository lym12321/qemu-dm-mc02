/* Controller-independent fixed-capacity periodic USB endpoint registry. */
#include "dm_usb_host_periodic_registry.h"

void dm_usb_host_periodic_registry_init(DmUsbHostPeriodicRegistry *registry,
                                        DmUsbHostSpeed speed)
{
    registry->speed = speed;
    registry->count = 0;
}

bool dm_usb_host_periodic_registry_add(
    DmUsbHostPeriodicRegistry *registry,
    const DmUsbHostPeriodicRegistryEntry *entry)
{
    unsigned index;

    if (!registry || !entry || !entry->poller ||
        entry->poller->schedule.speed != registry->speed ||
        registry->count == DM_USB_HOST_PERIODIC_REGISTRY_CAPACITY) {
        return false;
    }
    for (index = 0; index < registry->count; ++index) {
        if (registry->entry[index].poller == entry->poller) {
            return false;
        }
    }
    registry->entry[registry->count++] = *entry;
    return true;
}

bool dm_usb_host_periodic_registry_remove(
    DmUsbHostPeriodicRegistry *registry, DmUsbHostPeriodicPoller *poller)
{
    unsigned index;

    if (!registry || !poller) {
        return false;
    }
    for (index = 0; index < registry->count; ++index) {
        if (registry->entry[index].poller != poller) {
            continue;
        }
        --registry->count;
        registry->entry[index] = registry->entry[registry->count];
        return true;
    }
    return false;
}

unsigned dm_usb_host_periodic_registry_dispatch(
    DmUsbHostPeriodicRegistry *registry, uint64_t timestamp_ns)
{
    unsigned submitted = 0;
    unsigned index = 0;

    while (index < registry->count) {
        DmUsbHostPeriodicRegistryEntry *entry = &registry->entry[index];
        DmUsbHostEndpointCompletion completion;
        DmUsbHostPeriodicPollResult result;

        result = dm_usb_host_periodic_poller_poll(
            entry->poller, timestamp_ns, entry->out_data, entry->out_length,
            entry->in_data, entry->in_capacity, &completion);
        if (result == DM_USB_HOST_PERIODIC_POLL_SUBMITTED ||
            result == DM_USB_HOST_PERIODIC_POLL_RETRY_EXHAUSTED ||
            result == DM_USB_HOST_PERIODIC_POLL_RETRY_TIMEOUT) {
            if (result == DM_USB_HOST_PERIODIC_POLL_SUBMITTED) {
                ++submitted;
            }
            if (entry->completion) {
                entry->completion(entry->completion_opaque, completion);
            }
            if (completion != DM_USB_HOST_ENDPOINT_STALL) {
                ++index;
                continue;
            }
        } else if (result == DM_USB_HOST_PERIODIC_POLL_NOT_DUE ||
                   result == DM_USB_HOST_PERIODIC_POLL_DEFERRED) {
            ++index;
            continue;
        }
        dm_usb_host_periodic_registry_remove(registry, entry->poller);
    }
    return submitted;
}
