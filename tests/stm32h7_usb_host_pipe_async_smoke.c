#include "dm_stm32h7_usb_host_pipe.h"

#include <stdbool.h>
#include <stdio.h>

#define HCCHAR_CHENA (UINT32_C(1) << 31)
#define HCCHAR_CHDIS (UINT32_C(1) << 30)

enum {
    HCCHAR_OFFSET = 0x520,
    HCINT_OFFSET = 0x528,
    HCTSIZ_OFFSET = 0x530,
    HCFIFO_OFFSET = 0x2000,
    HCINT_XFRC = 1u << 0,
    HCINT_CHHLTD = 1u << 1,
    HCINT_NAK = 1u << 4,
};

typedef union FakeMmio {
    uint32_t words[0x3000 / sizeof(uint32_t)];
    uint8_t bytes[0x3000];
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

static volatile uint8_t *fifo(FakeMmio *mmio)
{
    return &mmio->bytes[HCFIFO_OFFSET];
}

static DmUsbHostPipe make_pipe(bool direction_in)
{
    return (DmUsbHostPipe) {
        .device_address = 5,
        .endpoint_number = 2,
        .attributes = DM_USB_HOST_PIPE_BULK,
        .direction_in = direction_in,
        .transfer_type = DM_USB_HOST_PIPE_BULK,
        .max_packet_size = 64,
        .transactions_per_microframe = 1,
    };
}

int main(void)
{
    const uint8_t channels[] = { 1 };
    const uint8_t out_data[] = { 0x11, 0x22, 0x33 };
    DmUsbHostChannelAllocator allocator;
    DmStm32H7UsbHostPipeAsync async = { 0 };
    DmStm32H7UsbHostPipeAsync other = { 0 };
    DmUsbHostChannelCompletionToken first_token;
    DmUsbHostChannelCompletionToken nak_token;
    DmUsbHostChannelCompletionToken cancel_token;
    DmUsbHostChannelCompletionToken in_token;
    DmUsbHostChannelCompletionToken stale_token;
    DmUsbHostPipe pipe = make_pipe(false);
    FakeMmio mmio = { 0 };
    uint8_t in_data[3] = { 0 };
    DmUsbHostEndpointCompletion completion_result;
    size_t actual_length;
    uint64_t timestamp_ns;

    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, channels, 1),
               "initialize one-channel allocator") ||
        expect(dm_stm32h7_usb_host_pipe_async_init(&async) &&
                   dm_stm32h7_usb_host_pipe_async_init(&other),
               "initialize asynchronous operations") ||
        expect(dm_stm32h7_usb_host_pipe_async_start(
                   &async, (uintptr_t)mmio.words, &allocator, &pipe,
                   DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data,
                   sizeof(out_data), NULL, 0, &first_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED,
               "start programs one packet and retains lease") ||
        expect((*reg(&mmio, HCCHAR_OFFSET) & HCCHAR_CHENA) &&
                   (*reg(&mmio, HCCHAR_OFFSET) & 0x7ffu) == 64 &&
                   *reg(&mmio, HCTSIZ_OFFSET) ==
                       (sizeof(out_data) | (1u << 19)),
               "start writes H723 channel fields") ||
        expect(dm_stm32h7_usb_host_pipe_async_start(
                   &other, (uintptr_t)mmio.words, &allocator, &pipe,
                   DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data,
                   sizeof(out_data), NULL, 0, &stale_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_DEFERRED,
               "second operation defers while channel is pending")) {
        return 1;
    }

    *reg(&mmio, HCINT_OFFSET) = 0;
    if (expect(dm_stm32h7_usb_host_pipe_async_poll(
                   &async, &first_token, 10) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_PENDING &&
                   dm_usb_host_channel_completion_pending(&async.completion),
               "no controller event leaves operation pending") ||
        expect((*reg(&mmio, HCCHAR_OFFSET) & HCCHAR_CHENA) != 0,
               "pending poll does not stop channel") ) {
        return 1;
    }

    *reg(&mmio, HCTSIZ_OFFSET) = (1u << 19) | 1u;
    *reg(&mmio, HCINT_OFFSET) = HCINT_XFRC | HCINT_CHHLTD;
    if (expect(dm_stm32h7_usb_host_pipe_async_poll(
                   &async, &first_token, 11) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_OK &&
                   dm_usb_host_channel_completion_actual_length(
                       &async.completion) == 2 &&
                   dm_usb_host_channel_completion_timestamp_ns(
                       &async.completion) == 11 &&
                   !dm_usb_host_channel_completion_pending(&async.completion) &&
                   !allocator.entry[0].owner,
               "accepted IRQ completion computes length and releases lease")) {
        return 1;
    }

    stale_token = first_token;
    if (expect(dm_stm32h7_usb_host_pipe_async_start(
                   &async, (uintptr_t)mmio.words, &allocator, &pipe,
                   DM_STM32H7_USB_HOST_PIPE_PID_DATA1, out_data, 1,
                   NULL, 0, &nak_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED,
               "reuse operation for a new packet") ||
        expect(stale_token.generation != nak_token.generation &&
                   dm_stm32h7_usb_host_pipe_async_poll(
                       &async, &stale_token, 12) ==
                       DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID &&
                   *reg(&mmio, HCCHAR_OFFSET) & HCCHAR_CHENA,
               "stale IRQ token cannot inspect the new channel") ) {
        return 1;
    }

    *reg(&mmio, HCINT_OFFSET) = HCINT_NAK;
    if (expect(dm_stm32h7_usb_host_pipe_async_poll(
                   &async, &nak_token, 13) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_NAK &&
                   dm_usb_host_channel_completion_result(
                       &async.completion, &completion_result,
                       &actual_length, &timestamp_ns) &&
                   completion_result == DM_USB_HOST_ENDPOINT_NAK &&
                   actual_length == 0 && timestamp_ns == 13 &&
                   (*reg(&mmio, HCCHAR_OFFSET) & HCCHAR_CHDIS) &&
                   !(*reg(&mmio, HCCHAR_OFFSET) & HCCHAR_CHENA) &&
                   !allocator.entry[0].owner,
               "NAK stops channel before releasing its lease") ||
        expect(dm_stm32h7_usb_host_pipe_async_start(
                   &async, (uintptr_t)mmio.words, &allocator, &pipe,
                   DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data, 1,
                   NULL, 0, &cancel_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED,
               "start another packet for cancellation") ||
        expect(dm_stm32h7_usb_host_pipe_async_cancel(
                   &async, &cancel_token, 14) &&
                   dm_usb_host_channel_completion_state(&async.completion) ==
                       DM_USB_HOST_CHANNEL_COMPLETION_CANCELLED &&
                   (*reg(&mmio, HCCHAR_OFFSET) & HCCHAR_CHDIS) &&
                   !allocator.entry[0].owner,
               "cancel stops channel and releases its lease")) {
        return 1;
    }

    pipe = make_pipe(true);
    *fifo(&mmio) = 0xa5;
    *reg(&mmio, HCINT_OFFSET) = 0;
    if (expect(dm_stm32h7_usb_host_pipe_async_start(
                   &async, (uintptr_t)mmio.words, &allocator, &pipe,
                   DM_STM32H7_USB_HOST_PIPE_PID_DATA0, NULL, 0,
                   in_data, sizeof(in_data), &in_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED,
               "start an IN packet") ||
        expect(*reg(&mmio, HCTSIZ_OFFSET) ==
                   (sizeof(in_data) | (1u << 19)),
               "IN start uses requested receive capacity") ) {
        return 1;
    }
    *reg(&mmio, HCTSIZ_OFFSET) = (1u << 19) | 1u;
    *reg(&mmio, HCINT_OFFSET) = HCINT_XFRC | HCINT_CHHLTD;
    if (expect(dm_stm32h7_usb_host_pipe_async_poll(
                   &async, &in_token, 15) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_OK &&
                   dm_usb_host_channel_completion_actual_length(
                       &async.completion) == 2 &&
                   in_data[0] == 0xa5 && in_data[1] == 0xa5,
               "IN completion reads the accepted FIFO bytes") ||
        expect(dm_stm32h7_usb_host_pipe_async_start(
                   &async, (uintptr_t)mmio.words, &allocator, &pipe,
                   DM_STM32H7_USB_HOST_PIPE_PID_DATA0, NULL, 0,
                   NULL, sizeof(in_data), &in_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID,
               "invalid IN buffer is rejected before leasing a channel") ||
        expect(!allocator.entry[0].owner,
               "invalid start leaves channel free")) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host async pipe smoke passed");
    return 0;
}
