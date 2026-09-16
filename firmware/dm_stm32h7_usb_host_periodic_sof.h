/* H723 SOF-event adapter for a controller-independent periodic poller. */
#ifndef DM_STM32H7_USB_HOST_PERIODIC_SOF_H
#define DM_STM32H7_USB_HOST_PERIODIC_SOF_H

#include "dm_stm32h7_usb_host_sof.h"
#include "dm_usb_host_periodic_poller.h"

#include <stdbool.h>

typedef struct DmStm32H7UsbHostPeriodicSof {
    DmStm32H7UsbHostSof *source;
    DmUsbHostPeriodicPoller *poller;
} DmStm32H7UsbHostPeriodicSof;

/* Bind an initialized H723 SOF source to one periodic endpoint poller. */
bool dm_stm32h7_usb_host_periodic_sof_init(
    DmStm32H7UsbHostPeriodicSof *event, DmStm32H7UsbHostSof *source,
    DmUsbHostPeriodicPoller *poller);

/* Process one observed SOF event and submit at most one due packet. */
DmUsbHostPeriodicPollResult dm_stm32h7_usb_host_periodic_sof_on_event(
    DmStm32H7UsbHostPeriodicSof *event, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    DmUsbHostEndpointCompletion *completion);

#endif /* DM_STM32H7_USB_HOST_PERIODIC_SOF_H */
