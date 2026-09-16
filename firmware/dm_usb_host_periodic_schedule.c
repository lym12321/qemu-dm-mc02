/* Board- and controller-independent USB periodic endpoint scheduling. */
#include "dm_usb_host_periodic_schedule.h"

#define USB_FRAME_NS       1000000ull
#define USB_MICROFRAME_NS   125000ull
#define USB_HIGH_INTERVAL_MAX 16u
#define USB_LOW_INTERRUPT_INTERVAL_MIN 10u

static bool dm_usb_host_periodic_schedule_interval(
    const DmUsbHostPipe *pipe, DmUsbHostSpeed speed, uint64_t *interval_ns)
{
    if (!pipe || !interval_ns || !pipe->interval ||
        (pipe->transfer_type != DM_USB_HOST_PIPE_INTERRUPT &&
         pipe->transfer_type != DM_USB_HOST_PIPE_ISOCHRONOUS)) {
        return false;
    }

    if (speed == DM_USB_HOST_SPEED_HIGH) {
        if (pipe->interval > USB_HIGH_INTERVAL_MAX) {
            return false;
        }
        *interval_ns = USB_MICROFRAME_NS << (pipe->interval - 1u);
        return true;
    }

    if (pipe->transactions_per_microframe != 1u) {
        return false;
    }

    if (pipe->transfer_type == DM_USB_HOST_PIPE_ISOCHRONOUS) {
        if (speed != DM_USB_HOST_SPEED_FULL || pipe->interval != 1u) {
            return false;
        }
        *interval_ns = USB_FRAME_NS;
        return true;
    }

    if (speed == DM_USB_HOST_SPEED_FULL) {
        *interval_ns = USB_FRAME_NS * pipe->interval;
        return true;
    }
    if (speed == DM_USB_HOST_SPEED_LOW &&
        pipe->interval >= USB_LOW_INTERRUPT_INTERVAL_MIN) {
        *interval_ns = USB_FRAME_NS * pipe->interval;
        return true;
    }
    return false;
}

DmUsbHostPeriodicScheduleResult dm_usb_host_periodic_schedule_init(
    DmUsbHostPeriodicSchedule *schedule, const DmUsbHostPipe *pipe,
    DmUsbHostSpeed speed, uint64_t origin_ns)
{
    uint64_t interval_ns;

    if (!schedule || !dm_usb_host_periodic_schedule_interval(
                         pipe, speed, &interval_ns)) {
        return DM_USB_HOST_PERIODIC_SCHEDULE_INVALID;
    }

    *schedule = (DmUsbHostPeriodicSchedule) {
        .pipe = *pipe,
        .speed = speed,
        .interval_ns = interval_ns,
        .next_slot_ns = origin_ns,
    };
    return DM_USB_HOST_PERIODIC_SCHEDULE_OK;
}

bool dm_usb_host_periodic_schedule_eligible(
    const DmUsbHostPeriodicSchedule *schedule, uint64_t timestamp_ns)
{
    return schedule && timestamp_ns >= schedule->next_slot_ns;
}

bool dm_usb_host_periodic_schedule_advance(
    DmUsbHostPeriodicSchedule *schedule, uint64_t timestamp_ns)
{
    uint64_t elapsed_ns;
    uint64_t advance_ns;

    if (!dm_usb_host_periodic_schedule_eligible(schedule, timestamp_ns)) {
        return false;
    }

    elapsed_ns = timestamp_ns - schedule->next_slot_ns;
    advance_ns = schedule->interval_ns -
                 (elapsed_ns % schedule->interval_ns);
    if (timestamp_ns > UINT64_MAX - advance_ns) {
        return false;
    }
    schedule->next_slot_ns = timestamp_ns + advance_ns;
    return true;
}
