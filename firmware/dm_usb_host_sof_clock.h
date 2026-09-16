/* Controller-independent reconstruction of USB SOF virtual time. */
#ifndef DM_USB_HOST_SOF_CLOCK_H
#define DM_USB_HOST_SOF_CLOCK_H

#include "dm_usb_host_speed.h"

#include <stdint.h>

typedef enum DmUsbHostSofClockResult {
    DM_USB_HOST_SOF_CLOCK_OK,
    DM_USB_HOST_SOF_CLOCK_INVALID,
} DmUsbHostSofClockResult;

typedef struct DmUsbHostSofClock {
    DmUsbHostSpeed speed;
    uint16_t last_frame_number;
    uint64_t timestamp_ns;
    uint64_t tick_ns;
} DmUsbHostSofClock;

/* Start a monotonic virtual-time reconstruction at one observed frame count. */
DmUsbHostSofClockResult dm_usb_host_sof_clock_init(
    DmUsbHostSofClock *clock, DmUsbHostSpeed speed, uint16_t frame_number,
    uint64_t origin_ns);

/* Advance by the unsigned 16-bit frame counter delta and return virtual time. */
uint64_t dm_usb_host_sof_clock_advance(DmUsbHostSofClock *clock,
                                       uint16_t frame_number);

#endif /* DM_USB_HOST_SOF_CLOCK_H */
