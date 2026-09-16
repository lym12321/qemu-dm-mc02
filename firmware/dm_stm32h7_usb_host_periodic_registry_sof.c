/* H723 SOF-event adapter for a periodic endpoint registry. */
#include "dm_stm32h7_usb_host_periodic_registry_sof.h"

bool dm_stm32h7_usb_host_periodic_registry_sof_init(
    DmStm32H7UsbHostPeriodicRegistrySof *event,
    DmStm32H7UsbHostSof *source,
    DmUsbHostPeriodicRegistry *registry)
{
    if (!event || !source || !registry ||
        source->clock.speed != registry->speed) {
        return false;
    }
    *event = (DmStm32H7UsbHostPeriodicRegistrySof) {
        .source = source,
        .registry = registry,
    };
    return true;
}

unsigned dm_stm32h7_usb_host_periodic_registry_sof_on_event(
    DmStm32H7UsbHostPeriodicRegistrySof *event)
{
    return dm_usb_host_periodic_registry_dispatch(
        event->registry, dm_stm32h7_usb_host_sof_timestamp(event->source));
}
