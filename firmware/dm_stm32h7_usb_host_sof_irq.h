/* H723 SOF status IRQ handler adapter for a periodic endpoint registry. */
#ifndef DM_STM32H7_USB_HOST_SOF_IRQ_H
#define DM_STM32H7_USB_HOST_SOF_IRQ_H

#include "dm_stm32h7_usb_host_periodic_registry_sof.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct DmStm32H7UsbHostSofIrq {
    uintptr_t base;
    DmStm32H7UsbHostPeriodicRegistrySof *event;
} DmStm32H7UsbHostSofIrq;

bool dm_stm32h7_usb_host_sof_irq_init(
    DmStm32H7UsbHostSofIrq *irq, uintptr_t base,
    DmStm32H7UsbHostPeriodicRegistrySof *event);

/* W1C an observed SOF status and dispatch all due registered endpoints. */
unsigned dm_stm32h7_usb_host_sof_irq_handle(DmStm32H7UsbHostSofIrq *irq);

#endif /* DM_STM32H7_USB_HOST_SOF_IRQ_H */
