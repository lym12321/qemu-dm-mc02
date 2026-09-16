#include "dm_stm32h7_usb_host_bulk_async.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

enum {
    GAHBCFG_OFFSET = 0x008,
    GINTSTS_OFFSET = 0x014,
    GINTMSK_OFFSET = 0x018,
    HAINT_OFFSET = 0x414,
    HAINTMSK_OFFSET = 0x418,
    HCCHAR_OFFSET = 0x500,
    HCINT_OFFSET = 0x508,
    HCTSIZ_OFFSET = 0x510,
    CHANNEL_STRIDE = 0x20,
    GAHBCFG_GINT = 1u << 0,
    GINTSTS_HCINT = 1u << 25,
    HCINT_XFRC = 1u << 0,
    HCINT_CHHLTD = 1u << 1,
    HCINT_NAK = 1u << 4,
    HCCHAR_CHDIS = 1u << 30,
    HCTSIZ_PID_SHIFT = 29,
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

static void enable_irq(FakeMmio *mmio, unsigned channel)
{
    *reg(mmio, GAHBCFG_OFFSET) = GAHBCFG_GINT;
    *reg(mmio, GINTSTS_OFFSET) = GINTSTS_HCINT;
    *reg(mmio, GINTMSK_OFFSET) = GINTSTS_HCINT;
    *reg(mmio, HAINT_OFFSET) = 1u << channel;
    *reg(mmio, HAINTMSK_OFFSET) = 1u << channel;
}

static void complete_packet(FakeMmio *mmio, unsigned channel,
                            size_t remaining, unsigned interrupt)
{
    *channel_reg(mmio, channel, HCTSIZ_OFFSET) =
        (1u << 19) | (uint32_t)remaining;
    *channel_reg(mmio, channel, HCINT_OFFSET) = interrupt;
}

int main(void)
{
    const uint8_t channels[] = { 1 };
    uint8_t out_data[130] = { 0 };
    FakeMmio mmio = { 0 };
    DmUsbHostChannelAllocator allocator;
    DmStm32H7UsbHostPipeAsyncDispatch dispatch;
    DmStm32H7UsbHostBulkAsync bulk = { 0 };
    DmUsbHostEndpointState endpoint_state;
    DmUsbHostPipe pipe = make_pipe(false);
    DmUsbHostChannelLease occupied;
    DmUsbHostChannelCompletionToken token;
    DmUsbHostChannelCompletionToken next_token;
    DmStm32H7UsbHostBulkAsyncResult poll_result;
    size_t actual_length;
    int owner;

    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, channels, sizeof(channels)) &&
                   dm_stm32h7_usb_host_pipe_async_dispatch_init(
                       &dispatch, (uintptr_t)mmio.words, NULL, NULL, NULL) &&
                   dm_stm32h7_usb_host_bulk_async_init(
                       &bulk, &dispatch, (uintptr_t)mmio.words, &allocator,
                       &endpoint_state),
               "initialize async bulk composition") ||
        expect(dm_stm32h7_usb_host_bulk_async_start(
                   &bulk, out_data, sizeof(out_data), NULL, 0, false,
                   &token) == DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED &&
                   *channel_reg(&mmio, 1, HCCHAR_OFFSET) & (1u << 31) &&
                   (*channel_reg(&mmio, 1, HCTSIZ_OFFSET) & 0x7ffffu) == 64,
               "start first packet") ) {
        return 1;
    }

    enable_irq(&mmio, 1);
    complete_packet(&mmio, 1, 0, HCINT_XFRC | HCINT_CHHLTD);
    if (expect(dm_stm32h7_usb_host_bulk_async_poll(
                   &bulk, &token, 10, &actual_length, &next_token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED &&
                   actual_length == 64 && next_token.generation !=
                   token.generation &&
                   ((*channel_reg(&mmio, 1, HCTSIZ_OFFSET) >>
                     HCTSIZ_PID_SHIFT) & 3u) == 2,
               "accepted packet starts the second packet with DATA1") ) {
        return 1;
    }
    token = next_token;
    complete_packet(&mmio, 1, 0, HCINT_XFRC | HCINT_CHHLTD);
    if (expect(dm_stm32h7_usb_host_bulk_async_poll(
                   &bulk, &token, 11, &actual_length, &next_token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED &&
                   actual_length == 128,
               "accepted second packet continues the request") ) {
        return 1;
    }
    token = next_token;
    complete_packet(&mmio, 1, 0, HCINT_XFRC | HCINT_CHHLTD);
    poll_result = dm_stm32h7_usb_host_bulk_async_poll(
        &bulk, &token, 12, &actual_length, &next_token);
    if (expect(poll_result == DM_STM32H7_USB_HOST_BULK_ASYNC_OK &&
                   actual_length == sizeof(out_data) &&
                   dm_stm32h7_usb_host_bulk_async_state(&bulk) ==
                       DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED &&
                   !allocator.entry[0].owner,
               "final short packet completes and releases the channel") ) {
        return 1;
    }

    pipe = make_pipe(false);
    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    if (expect(dm_stm32h7_usb_host_bulk_async_start(
                   &bulk, out_data, 1, NULL, 0, false, &token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED,
               "start packet for IRQ-first consumer") ) {
        return 1;
    }
    enable_irq(&mmio, 1);
    complete_packet(&mmio, 1, 0, HCINT_XFRC | HCINT_CHHLTD);
    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_handle(
                   &dispatch, 13) == 1 && dispatch.async[1] == NULL,
               "global IRQ consumer completes the bulk packet first") ||
        expect(dm_stm32h7_usb_host_bulk_async_poll(
                   &bulk, &token, 14, &actual_length, &next_token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_OK &&
                   actual_length == 1 &&
                   dm_stm32h7_usb_host_bulk_async_state(&bulk) ==
                       DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED,
               "bulk poll consumes an already completed IRQ record")) {
        return 1;
    }

    pipe = make_pipe(false);
    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    if (expect(dm_usb_host_channel_allocator_acquire(
                   &allocator, &owner, &occupied) &&
                   dm_stm32h7_usb_host_bulk_async_start(
                       &bulk, out_data, 1, NULL, 0, false, &token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_DEFERRED &&
                   dm_stm32h7_usb_host_bulk_async_state(&bulk) ==
                       DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY &&
                   dm_usb_host_channel_allocator_release(&allocator,
                                                         &occupied),
               "channel exhaustion defers without endpoint mutation") ) {
        return 1;
    }
    if (expect(dm_stm32h7_usb_host_bulk_async_resume(&bulk, &token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED,
               "resume deferred packet") ) {
        return 1;
    }
    complete_packet(&mmio, 1, 0, HCINT_NAK);
    if (expect(dm_stm32h7_usb_host_bulk_async_poll(
                   &bulk, &token, 20, &actual_length, &next_token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_NAK &&
                   actual_length == 0 &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   dm_stm32h7_usb_host_bulk_async_state(&bulk) ==
                       DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY,
               "NAK preserves packet offset and DATA PID") ) {
        return 1;
    }
    if (expect(dm_stm32h7_usb_host_bulk_async_resume(&bulk, &token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED,
               "explicitly retry NAK packet") ) {
        return 1;
    }
    complete_packet(&mmio, 1, 0, HCINT_XFRC | HCINT_CHHLTD);
    if (expect(dm_stm32h7_usb_host_bulk_async_poll(
                   &bulk, &token, 21, &actual_length, &next_token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_OK && actual_length == 1,
               "retried packet completes without replaying data") ) {
        return 1;
    }

    if (expect(dm_stm32h7_usb_host_bulk_async_start(
                   &bulk, out_data, 1, NULL, 0, false, &token) ==
                   DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED &&
                   dm_stm32h7_usb_host_bulk_async_cancel(
                       &bulk, &token, 22) && !allocator.entry[0].owner &&
                   dm_stm32h7_usb_host_bulk_async_state(&bulk) ==
                       DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_CANCELLED,
               "cancel pending packet releases its channel") ) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host async bulk smoke passed");
    return 0;
}
