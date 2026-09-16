/* H723 SOF-event adapter for a periodic endpoint registry. */
#ifndef DM_STM32H7_USB_HOST_PERIODIC_REGISTRY_SOF_H
#define DM_STM32H7_USB_HOST_PERIODIC_REGISTRY_SOF_H

#include "dm_stm32h7_usb_host_sof.h"
#include "dm_usb_host_periodic_registry.h"

#include <stdbool.h>

typedef struct DmStm32H7UsbHostPeriodicRegistrySof {
    DmStm32H7UsbHostSof *source;
    DmUsbHostPeriodicRegistry *registry;
} DmStm32H7UsbHostPeriodicRegistrySof;

bool dm_stm32h7_usb_host_periodic_registry_sof_init(
    DmStm32H7UsbHostPeriodicRegistrySof *event,
    DmStm32H7UsbHostSof *source,
    DmUsbHostPeriodicRegistry *registry);

/* Process one observed SOF and return the number of packets submitted. */
unsigned dm_stm32h7_usb_host_periodic_registry_sof_on_event(
    DmStm32H7UsbHostPeriodicRegistrySof *event);

#endif /* DM_STM32H7_USB_HOST_PERIODIC_REGISTRY_SOF_H */
