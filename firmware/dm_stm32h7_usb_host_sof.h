/* STM32H7 register source for controller-independent USB SOF virtual time. */
#ifndef DM_STM32H7_USB_HOST_SOF_H
#define DM_STM32H7_USB_HOST_SOF_H

#include "dm_usb_host_sof_clock.h"

#include <stdint.h>

typedef struct DmStm32H7UsbHostSof {
    uintptr_t base;
    DmUsbHostSofClock clock;
} DmStm32H7UsbHostSof;

/* Snapshot HPRT0 speed and HFNUM at a caller-defined virtual-time origin. */
DmUsbHostSofClockResult dm_stm32h7_usb_host_sof_init(
    DmStm32H7UsbHostSof *source, uintptr_t base, uint64_t origin_ns);

/* Read HFNUM and return reconstructed time; reinitialize after port reset. */
uint64_t dm_stm32h7_usb_host_sof_timestamp(DmStm32H7UsbHostSof *source);

#endif /* DM_STM32H7_USB_HOST_SOF_H */
