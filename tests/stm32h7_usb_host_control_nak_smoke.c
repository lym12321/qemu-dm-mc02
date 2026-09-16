#include "dm_stm32h7_usb_host_control.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

enum {
    MMIO_SIZE = 0x2000,
    HCCHAR_OFFSET = 0x500,
    HCINT_OFFSET = 0x508,
    HCTSIZ_OFFSET = 0x510,
    HCTSIZ_PID_SHIFT = 29,
    HCTSIZ_PID_SETUP = 3u,
};

#define HCCHAR_CHENA UINT32_C(1) << 31
#define HCCHAR_CHDIS UINT32_C(1) << 30
#define HCINT_XFRC UINT32_C(1) << 0
#define HCINT_CHHLTD UINT32_C(1) << 1
#define HCINT_NAK UINT32_C(1) << 4
#define HCTSIZ_PKT_SHIFT 19

typedef union FakeMmio {
    uint32_t words[MMIO_SIZE / sizeof(uint32_t)];
    uint8_t bytes[MMIO_SIZE];
} FakeMmio;

typedef struct Fixture {
    FakeMmio mmio;
    unsigned poll_count;
    uint32_t interrupt;
    uint32_t remaining;
} Fixture;

static volatile uint32_t *reg(FakeMmio *mmio, unsigned offset)
{
    return &mmio->words[offset / sizeof(uint32_t)];
}

static void inject_completion(void *opaque,
                              DmStm32H7UsbHostControl *control)
{
    Fixture *fixture = opaque;

    ++fixture->poll_count;
    if (fixture->poll_count == 1 && control->channel == 0) {
        *reg(&fixture->mmio, HCINT_OFFSET) = fixture->interrupt;
        if (fixture->remaining) {
            *reg(&fixture->mmio, HCTSIZ_OFFSET) =
                fixture->remaining | (1u << HCTSIZ_PKT_SHIFT) |
                (HCTSIZ_PID_SETUP << HCTSIZ_PID_SHIFT);
        }
    } else if (fixture->poll_count == 2 &&
               fixture->interrupt == HCINT_NAK &&
               (*reg(&fixture->mmio, HCCHAR_OFFSET) & HCCHAR_CHDIS)) {
        *reg(&fixture->mmio, HCINT_OFFSET) = HCINT_CHHLTD;
    }
}

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
    static const uint8_t setup[] = {
        0x80, 0x00, 0, 0, 0, 0, 0, 0,
    };
    static const uint8_t setup_in_one[] = {
        0x80, 0x00, 0, 0, 0, 0, 1, 0,
    };
    static const uint8_t setup_out_one[] = {
        0x00, 0x00, 0, 0, 0, 0, 1, 0,
    };
    Fixture fixture = { .interrupt = HCINT_NAK };
    Fixture bad_remaining = {
        .interrupt = HCINT_XFRC | HCINT_CHHLTD,
        .remaining = 9,
    };
    Fixture invalid_input = { .interrupt = HCINT_XFRC | HCINT_CHHLTD };
    DmStm32H7UsbHostControl control;
    DmStm32H7UsbHostControlResult result;
    size_t actual_length = 99;
    uint32_t hcchar;
    uint32_t hctsiz;

    dm_stm32h7_usb_host_control_init(
        &control, (uintptr_t)fixture.mmio.bytes, 0, 64, 0);
    dm_stm32h7_usb_host_control_set_poll(
        &control, inject_completion, &fixture);
    result = dm_stm32h7_usb_host_control_transfer(
        &control, setup, NULL, 0, NULL, 0, &actual_length);

    hcchar = *reg(&fixture.mmio, HCCHAR_OFFSET);
    hctsiz = *reg(&fixture.mmio, HCTSIZ_OFFSET);
    if (expect(result == DM_STM32H7_USB_HOST_CONTROL_NAK &&
                   actual_length == 0,
               "NAK is returned as the control attempt result") ||
        expect(fixture.poll_count == 2,
               "SETUP NAK waits for channel halt before returning") ||
        expect(hcchar & HCCHAR_CHDIS,
               "NAK requests channel disable") ||
        expect((hctsiz >> HCTSIZ_PID_SHIFT) == HCTSIZ_PID_SETUP,
               "NAK is injected on the SETUP stage")) {
        return 1;
    }

    dm_stm32h7_usb_host_control_init(
        &control, (uintptr_t)bad_remaining.mmio.bytes, 0, 64, 0);
    dm_stm32h7_usb_host_control_set_poll(
        &control, inject_completion, &bad_remaining);
    actual_length = 99;
    result = dm_stm32h7_usb_host_control_transfer(
        &control, setup, NULL, 0, NULL, 0, &actual_length);
    if (expect(result == DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR &&
                   actual_length == 0 && bad_remaining.poll_count == 1,
               "completion with excessive remaining bytes is rejected")) {
        return 1;
    }

    dm_stm32h7_usb_host_control_init(
        &control, (uintptr_t)invalid_input.mmio.bytes, 0, 64, 0);
    dm_stm32h7_usb_host_control_set_poll(
        &control, inject_completion, &invalid_input);
    actual_length = 99;
    result = dm_stm32h7_usb_host_control_transfer(
        &control, setup_in_one, NULL, 0, NULL, 1, &actual_length);
    if (expect(result == DM_STM32H7_USB_HOST_CONTROL_INVALID &&
                   actual_length == 0 && invalid_input.poll_count == 0,
               "IN data length requires an IN buffer") ||
        expect(dm_stm32h7_usb_host_control_transfer(
                   &control, setup_out_one, NULL, 1, NULL, 0,
                   &actual_length) == DM_STM32H7_USB_HOST_CONTROL_INVALID &&
                   actual_length == 0 && invalid_input.poll_count == 0,
               "OUT data length requires an OUT buffer")) {
        return 1;
    }

    control.channel = 12;
    actual_length = 99;
    if (expect(dm_stm32h7_usb_host_control_transfer(
                   &control, setup, NULL, 0, NULL, 0, &actual_length) ==
                   DM_STM32H7_USB_HOST_CONTROL_INVALID && actual_length == 0,
               "channel outside the H723 range is rejected") ||
        expect(!dm_stm32h7_usb_host_control_port_reset(&control),
               "port reset rejects an invalid channel")) {
        return 1;
    }
    control.channel = 0;
    control.device_address = 128;
    actual_length = 99;
    if (expect(dm_stm32h7_usb_host_control_transfer(
                   &control, setup, NULL, 0, NULL, 0, &actual_length) ==
                   DM_STM32H7_USB_HOST_CONTROL_INVALID && actual_length == 0,
               "device address outside the HCCHAR field is rejected")) {
        return 1;
    }
    control.device_address = 0;
    control.ep0_max_packet_size = 0x800;
    actual_length = 99;
    if (expect(dm_stm32h7_usb_host_control_transfer(
                   &control, setup, NULL, 0, NULL, 0, &actual_length) ==
                   DM_STM32H7_USB_HOST_CONTROL_INVALID && actual_length == 0,
               "MPS outside the HCCHAR field is rejected")) {
        return 1;
    }

    puts("PASS: control NAK and input boundaries are enforced");
    return 0;
}
