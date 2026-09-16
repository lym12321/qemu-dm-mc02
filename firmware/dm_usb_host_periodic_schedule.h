/* Board- and controller-independent USB periodic endpoint scheduling. */
#ifndef DM_USB_HOST_PERIODIC_SCHEDULE_H
#define DM_USB_HOST_PERIODIC_SCHEDULE_H

#include "dm_usb_host_pipe.h"
#include "dm_usb_host_speed.h"

#include <stdbool.h>
#include <stdint.h>

typedef enum DmUsbHostPeriodicScheduleResult {
    DM_USB_HOST_PERIODIC_SCHEDULE_OK,
    DM_USB_HOST_PERIODIC_SCHEDULE_INVALID,
} DmUsbHostPeriodicScheduleResult;

typedef struct DmUsbHostPeriodicSchedule {
    DmUsbHostPipe pipe;
    DmUsbHostSpeed speed;
    uint64_t interval_ns;
    uint64_t next_slot_ns;
} DmUsbHostPeriodicSchedule;

/* Initialize a periodic endpoint schedule at the caller's virtual-time origin. */
DmUsbHostPeriodicScheduleResult dm_usb_host_periodic_schedule_init(
    DmUsbHostPeriodicSchedule *schedule, const DmUsbHostPipe *pipe,
    DmUsbHostSpeed speed, uint64_t origin_ns);

/* Return whether the endpoint may be polled at this virtual timestamp. */
bool dm_usb_host_periodic_schedule_eligible(
    const DmUsbHostPeriodicSchedule *schedule, uint64_t timestamp_ns);

/* Advance one completed poll to the first eligible slot strictly after timestamp. */
bool dm_usb_host_periodic_schedule_advance(
    DmUsbHostPeriodicSchedule *schedule, uint64_t timestamp_ns);

#endif /* DM_USB_HOST_PERIODIC_SCHEDULE_H */
