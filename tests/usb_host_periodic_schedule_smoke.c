#include "dm_usb_host_periodic_schedule.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define USB_FRAME_NS 1000000ull
#define USB_MICROFRAME_NS 125000ull

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static DmUsbHostPipe make_pipe(DmUsbHostPipeTransferType transfer_type,
                               uint8_t interval,
                               uint8_t transactions_per_microframe)
{
    return (DmUsbHostPipe) {
        .device_address = 5,
        .endpoint_number = 1,
        .attributes = transfer_type,
        .direction_in = true,
        .transfer_type = transfer_type,
        .max_packet_size = 8,
        .transactions_per_microframe = transactions_per_microframe,
        .interval = interval,
    };
}

int main(void)
{
    DmUsbHostPeriodicSchedule schedule = {
        .interval_ns = 7,
        .next_slot_ns = 8,
    };
    DmUsbHostPipe interrupt = make_pipe(DM_USB_HOST_PIPE_INTERRUPT, 1, 1);
    DmUsbHostPipe isochronous = make_pipe(DM_USB_HOST_PIPE_ISOCHRONOUS, 1, 1);
    DmUsbHostPipe bulk = make_pipe(DM_USB_HOST_PIPE_BULK, 1, 1);

    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_HIGH,
                   USB_FRAME_NS) == DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   schedule.interval_ns == USB_MICROFRAME_NS &&
                   schedule.next_slot_ns == USB_FRAME_NS &&
                   schedule.pipe.endpoint_number == 1,
               "high-speed interrupt interval and pipe copy") ||
        expect(!dm_usb_host_periodic_schedule_eligible(
                   &schedule, USB_FRAME_NS - 1) &&
                   dm_usb_host_periodic_schedule_eligible(
                       &schedule, USB_FRAME_NS),
               "origin is the first eligible high-speed slot") ||
        expect(!dm_usb_host_periodic_schedule_advance(
                   &schedule, USB_FRAME_NS - 1) &&
                   schedule.next_slot_ns == USB_FRAME_NS,
               "early poll does not advance") ||
        expect(dm_usb_host_periodic_schedule_advance(
                   &schedule, USB_FRAME_NS) &&
                   schedule.next_slot_ns == USB_FRAME_NS + USB_MICROFRAME_NS,
               "completed high-speed poll advances one microframe")) {
        return 1;
    }

    interrupt.interval = 16;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_HIGH, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   schedule.interval_ns == 4096000000ull,
               "high-speed bInterval exponent")) {
        return 1;
    }

    interrupt.interval = 10;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_FULL,
                   USB_FRAME_NS) == DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   schedule.interval_ns == 10 * USB_FRAME_NS,
               "full-speed interrupt frame interval") ||
        expect(dm_usb_host_periodic_schedule_advance(
                   &schedule, 31 * USB_FRAME_NS + USB_MICROFRAME_NS) &&
                   schedule.next_slot_ns == 41 * USB_FRAME_NS,
               "late poll skips missed slots") ||
        expect(!dm_usb_host_periodic_schedule_eligible(
                   &schedule, 41 * USB_FRAME_NS - 1) &&
                   dm_usb_host_periodic_schedule_eligible(
                       &schedule, 41 * USB_FRAME_NS),
               "late poll next slot is strictly in the future")) {
        return 1;
    }

    interrupt.transactions_per_microframe = 2;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_FULL, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_INVALID &&
                   dm_usb_host_periodic_schedule_init(
                       &schedule, &interrupt, DM_USB_HOST_SPEED_LOW, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_INVALID,
               "reject high-speed multi-transaction pipe at full and low speed")) {
        return 1;
    }
    interrupt.transactions_per_microframe = 1;

    interrupt.interval = 255;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_FULL, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   schedule.interval_ns == 255 * USB_FRAME_NS,
               "full-speed maximum interrupt interval") ||
        expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_LOW, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   schedule.interval_ns == 255 * USB_FRAME_NS,
               "low-speed interrupt frame interval")) {
        return 1;
    }

    isochronous.interval = 1;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &isochronous, DM_USB_HOST_SPEED_FULL, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   schedule.interval_ns == USB_FRAME_NS,
               "full-speed isochronous one-frame interval") ||
        expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &isochronous, DM_USB_HOST_SPEED_HIGH, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   schedule.interval_ns == USB_MICROFRAME_NS,
               "high-speed isochronous microframe interval")) {
        return 1;
    }

    schedule.interval_ns = 7;
    schedule.next_slot_ns = 8;
    interrupt.interval = 17;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_HIGH, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_INVALID &&
                   schedule.interval_ns == 7 && schedule.next_slot_ns == 8,
               "reject high-speed interval above specification without mutation")) {
        return 1;
    }

    interrupt.interval = 9;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_LOW, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_INVALID,
               "reject low-speed interrupt interval below ten frames")) {
        return 1;
    }

    isochronous.interval = 2;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &isochronous, DM_USB_HOST_SPEED_FULL, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_INVALID &&
                   dm_usb_host_periodic_schedule_init(
                       &schedule, &isochronous, DM_USB_HOST_SPEED_LOW, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_INVALID &&
                   dm_usb_host_periodic_schedule_init(
                       &schedule, &bulk, DM_USB_HOST_SPEED_HIGH, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_INVALID,
               "reject invalid periodic speed and transfer combinations")) {
        return 1;
    }

    interrupt.interval = 1;
    if (expect(dm_usb_host_periodic_schedule_init(
                   &schedule, &interrupt, DM_USB_HOST_SPEED_HIGH,
                   UINT64_MAX) == DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   !dm_usb_host_periodic_schedule_eligible(
                       &schedule, UINT64_MAX - 1) &&
                   !dm_usb_host_periodic_schedule_advance(
                       &schedule, UINT64_MAX) &&
                   schedule.next_slot_ns == UINT64_MAX,
               "reject periodic deadline overflow without mutation")) {
        return 1;
    }

    puts("RESULT: USB host periodic schedule smoke passed");
    return 0;
}
