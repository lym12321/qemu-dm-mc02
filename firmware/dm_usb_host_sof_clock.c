/* Controller-independent reconstruction of USB SOF virtual time. */
#include "dm_usb_host_sof_clock.h"

#define USB_FRAME_NS      1000000ull
#define USB_MICROFRAME_NS  125000ull

DmUsbHostSofClockResult dm_usb_host_sof_clock_init(
    DmUsbHostSofClock *clock, DmUsbHostSpeed speed, uint16_t frame_number,
    uint64_t origin_ns)
{
    uint64_t tick_ns;

    if (!clock) {
        return DM_USB_HOST_SOF_CLOCK_INVALID;
    }
    switch (speed) {
    case DM_USB_HOST_SPEED_HIGH:
        tick_ns = USB_MICROFRAME_NS;
        break;
    case DM_USB_HOST_SPEED_LOW:
    case DM_USB_HOST_SPEED_FULL:
        tick_ns = USB_FRAME_NS;
        break;
    default:
        return DM_USB_HOST_SOF_CLOCK_INVALID;
    }
    *clock = (DmUsbHostSofClock) {
        .speed = speed,
        .last_frame_number = frame_number,
        .timestamp_ns = origin_ns,
        .tick_ns = tick_ns,
    };
    return DM_USB_HOST_SOF_CLOCK_OK;
}

uint64_t dm_usb_host_sof_clock_advance(DmUsbHostSofClock *clock,
                                       uint16_t frame_number)
{
    uint16_t elapsed_frames = frame_number - clock->last_frame_number;

    clock->timestamp_ns += (uint64_t)elapsed_frames * clock->tick_ns;
    clock->last_frame_number = frame_number;
    return clock->timestamp_ns;
}
