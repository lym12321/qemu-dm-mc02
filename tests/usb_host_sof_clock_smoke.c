#include "dm_usb_host_sof_clock.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define USB_FRAME_NS      1000000ull
#define USB_MICROFRAME_NS  125000ull

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

int main(void)
{
    DmUsbHostSofClock clock = {
        .timestamp_ns = 7,
        .tick_ns = 8,
    };

    if (expect(dm_usb_host_sof_clock_init(
                   &clock, DM_USB_HOST_SPEED_HIGH, 10, 1000) ==
                   DM_USB_HOST_SOF_CLOCK_OK &&
                   clock.tick_ns == USB_MICROFRAME_NS &&
                   dm_usb_host_sof_clock_advance(&clock, 10) == 1000 &&
                   dm_usb_host_sof_clock_advance(&clock, 11) ==
                   1000 + USB_MICROFRAME_NS &&
                   dm_usb_host_sof_clock_advance(&clock, 19) ==
                   1000 + 9 * USB_MICROFRAME_NS,
               "high-speed microframe reconstruction") ||
        expect(dm_usb_host_sof_clock_init(
                   &clock, DM_USB_HOST_SPEED_FULL, 0xfffeu, 0) ==
                   DM_USB_HOST_SOF_CLOCK_OK &&
                   clock.tick_ns == USB_FRAME_NS &&
                   dm_usb_host_sof_clock_advance(&clock, 1) ==
                   3 * USB_FRAME_NS,
               "full-speed counter wrap reconstruction") ||
        expect(dm_usb_host_sof_clock_init(
                   &clock, DM_USB_HOST_SPEED_LOW, 3, 11) ==
                   DM_USB_HOST_SOF_CLOCK_OK &&
                   dm_usb_host_sof_clock_advance(&clock, 5) ==
                   11 + 2 * USB_FRAME_NS,
               "low-speed frame reconstruction")) {
        return 1;
    }

    clock.timestamp_ns = 7;
    clock.tick_ns = 8;
    if (expect(dm_usb_host_sof_clock_init(
                   &clock, (DmUsbHostSpeed)3, 0, 0) ==
                   DM_USB_HOST_SOF_CLOCK_INVALID &&
                   clock.timestamp_ns == 7 && clock.tick_ns == 8,
               "reject unknown speed without mutation")) {
        return 1;
    }

    puts("RESULT: USB host SOF clock smoke passed");
    return 0;
}
