#include "dm_stm32h7_usb_host_pipe_async_dispatch.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

enum {
    GAHBCFG_OFFSET = 0x008,
    GINTSTS_OFFSET = 0x014,
    GINTMSK_OFFSET = 0x018,
    HAINT_OFFSET = 0x414,
    HAINTMSK_OFFSET = 0x418,
    HCINT_OFFSET = 0x508,
    HCTSIZ_OFFSET = 0x510,
    CHANNEL_STRIDE = 0x20,
    GAHBCFG_GINT = 1u << 0,
    GINTSTS_HCINT = 1u << 25,
    HCINT_XFRC = 1u << 0,
    HCINT_CHHLTD = 1u << 1,
};

typedef union FakeMmio {
    uint32_t words[0x9000 / sizeof(uint32_t)];
    uint8_t bytes[0x9000];
} FakeMmio;

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static volatile uint32_t *reg(FakeMmio *mmio, unsigned offset)
{
    return &mmio->words[offset / sizeof(uint32_t)];
}

static volatile uint32_t *channel_reg(
    FakeMmio *mmio, unsigned channel, unsigned offset)
{
    return reg(mmio, offset + CHANNEL_STRIDE * channel);
}

static DmUsbHostPipe make_pipe(void)
{
    return (DmUsbHostPipe) {
        .device_address = 5,
        .endpoint_number = 2,
        .attributes = DM_USB_HOST_PIPE_BULK,
        .direction_in = false,
        .transfer_type = DM_USB_HOST_PIPE_BULK,
        .max_packet_size = 64,
        .transactions_per_microframe = 1,
    };
}

static void enable_irq(FakeMmio *mmio, unsigned channels)
{
    *reg(mmio, GAHBCFG_OFFSET) = GAHBCFG_GINT;
    *reg(mmio, GINTSTS_OFFSET) = GINTSTS_HCINT;
    *reg(mmio, GINTMSK_OFFSET) = GINTSTS_HCINT;
    *reg(mmio, HAINT_OFFSET) = channels;
    *reg(mmio, HAINTMSK_OFFSET) = channels;
}

static bool all_allocator_entries_free(
    const DmUsbHostChannelAllocator *allocator)
{
    unsigned index;

    for (index = 0; index < allocator->count; ++index) {
        if (allocator->entry[index].owner) {
            return false;
        }
    }
    return true;
}

int main(void)
{
    const uint8_t channels[] = { 2, 5 };
    const uint8_t out_data[] = { 0x11, 0x22, 0x33, 0x44 };
    FakeMmio mmio = { 0 };
    DmUsbHostChannelAllocator allocator;
    DmStm32H7UsbHostPipeAsyncDispatch dispatch;
    DmStm32H7UsbHostPipeAsync first = { 0 };
    DmStm32H7UsbHostPipeAsync second = { 0 };
    DmStm32H7UsbHostPipeAsync malformed = { 0 };
    DmUsbHostChannelCompletionToken first_token;
    DmUsbHostChannelCompletionToken second_token;
    DmUsbHostChannelCompletionToken malformed_token;
    DmUsbHostPipe pipe = make_pipe();
    unsigned completed;

    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, channels, sizeof(channels)) &&
                   dm_stm32h7_usb_host_pipe_async_init(&first) &&
                   dm_stm32h7_usb_host_pipe_async_init(&second) &&
                   dm_stm32h7_usb_host_pipe_async_init(&malformed) &&
                   dm_stm32h7_usb_host_pipe_async_dispatch_init(
                       &dispatch, (uintptr_t)mmio.words, NULL, NULL, NULL),
               "initialize allocator, async records and dispatcher")) {
        return 1;
    }

    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_start(
                   &dispatch, &first, (uintptr_t)mmio.words, &allocator,
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data,
                   sizeof(out_data), NULL, 0, &first_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
                   dm_stm32h7_usb_host_pipe_async_dispatch_start(
                       &dispatch, &second, (uintptr_t)mmio.words, &allocator,
                       &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data, 2,
                       NULL, 0, &second_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
                   dispatch.async[2] == &first && dispatch.async[5] == &second,
               "start two operations on distinct channels")) {
        return 1;
    }
    enable_irq(&mmio, (1u << 2) | (1u << 5));
    *channel_reg(&mmio, 2, HCTSIZ_OFFSET) = 0;
    *channel_reg(&mmio, 2, HCINT_OFFSET) = HCINT_XFRC | HCINT_CHHLTD;
    *channel_reg(&mmio, 5, HCTSIZ_OFFSET) = 0;
    *channel_reg(&mmio, 5, HCINT_OFFSET) = 0;
    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel(
                   &dispatch, 2, &first_token, 10) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_COMPLETED &&
                   dispatch.async[2] == NULL && dispatch.async[5] == &second &&
                   !dm_usb_host_channel_completion_pending(&first.completion) &&
                   dm_usb_host_channel_completion_pending(&second.completion),
               "handle_channel consumes only the selected channel token")) {
        return 1;
    }
    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_cancel(
                   &dispatch, &second, &second_token, 11),
               "cancel the untouched channel operation")) {
        return 1;
    }

    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_start(
                   &dispatch, &malformed, (uintptr_t)mmio.words, &allocator,
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data,
                   sizeof(out_data), NULL, 0, &malformed_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED,
               "start malformed-completion operation")) {
        return 1;
    }
    enable_irq(&mmio, 1u << 2);
    *channel_reg(&mmio, 2, HCTSIZ_OFFSET) = sizeof(out_data) + 1u;
    *channel_reg(&mmio, 2, HCINT_OFFSET) = HCINT_XFRC | HCINT_CHHLTD;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(&dispatch, 20);
    if (expect(completed == 0 && dispatch.async[2] == NULL &&
                   !dm_usb_host_channel_completion_pending(
                       &malformed.completion) &&
                   all_allocator_entries_free(&allocator),
               "invalid remaining length is cleaned up without completion")) {
        return 1;
    }

    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_start(
                   &dispatch, &first, (uintptr_t)mmio.words + 4u, &allocator,
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data, 1,
                   NULL, 0, &first_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID &&
                   dispatch.async[2] == NULL && dispatch.async[5] == NULL &&
                   !dm_usb_host_channel_completion_pending(&first.completion) &&
                   all_allocator_entries_free(&allocator),
               "wrong base does not start or acquire a channel")) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host async dispatch contract smoke passed");
    return 0;
}
