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
};

typedef union FakeMmio {
    uint32_t words[0x9000 / sizeof(uint32_t)];
    uint8_t bytes[0x9000];
} FakeMmio;

typedef struct TestLock {
    unsigned enters;
    unsigned leaves;
} TestLock;

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

static void lock_enter(void *opaque)
{
    ++((TestLock *)opaque)->enters;
}

static void lock_leave(void *opaque)
{
    ++((TestLock *)opaque)->leaves;
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

static void enable_channel_irq(FakeMmio *mmio, unsigned channel)
{
    *reg(mmio, GAHBCFG_OFFSET) = GAHBCFG_GINT;
    *reg(mmio, GINTSTS_OFFSET) = GINTSTS_HCINT;
    *reg(mmio, GINTMSK_OFFSET) = GINTSTS_HCINT;
    *reg(mmio, HAINT_OFFSET) = 1u << channel;
    *reg(mmio, HAINTMSK_OFFSET) = 1u << channel;
}

int main(void)
{
    const uint8_t channels[] = { 1, 3, 7 };
    const uint8_t out_data[] = { 0x10, 0x20, 0x30 };
    FakeMmio mmio = { 0 };
    DmUsbHostChannelAllocator allocator;
    DmStm32H7UsbHostPipeAsyncDispatch dispatch;
    DmStm32H7UsbHostPipeAsync first = { 0 };
    DmStm32H7UsbHostPipeAsync second = { 0 };
    DmStm32H7UsbHostPipeAsync third = { 0 };
    DmUsbHostChannelCompletionToken first_token;
    DmUsbHostChannelCompletionToken second_token;
    DmUsbHostChannelCompletionToken third_token;
    DmUsbHostEndpointCompletion result;
    TestLock lock = { 0 };
    DmUsbHostPipe pipe = make_pipe(false);
    size_t actual_length;
    uint64_t timestamp_ns;
    unsigned completed;

    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, channels, sizeof(channels)) &&
                   dm_stm32h7_usb_host_pipe_async_init(&first) &&
                   dm_stm32h7_usb_host_pipe_async_init(&second) &&
                   dm_stm32h7_usb_host_pipe_async_init(&third) &&
                   dm_stm32h7_usb_host_pipe_async_dispatch_init(
                       &dispatch, (uintptr_t)mmio.words,
                       lock_enter, lock_leave, &lock),
               "initialize allocator, async records and dispatcher") ||
        expect(dm_stm32h7_usb_host_pipe_async_dispatch_start(
                   &dispatch, &first, (uintptr_t)mmio.words, &allocator,
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data,
                   sizeof(out_data), NULL, 0, &first_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
                   dispatch.async[1] == &first &&
                   dm_usb_host_channel_completion_pending(&first.completion),
               "start atomically registers channel one")) {
        return 1;
    }
    if (expect(!dm_stm32h7_usb_host_pipe_async_dispatch_init(
                   &dispatch, (uintptr_t)mmio.words,
                   lock_enter, lock_leave, &lock) && dispatch.async[1] == &first,
               "active dispatch cannot be reinitialized")) {
        return 1;
    }

    *channel_reg(&mmio, 1, HCINT_OFFSET) = 0;
    enable_channel_irq(&mmio, 1);
    *reg(&mmio, GAHBCFG_OFFSET) = 0;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 9);
    if (expect(completed == 0 &&
                   dm_usb_host_channel_completion_pending(&first.completion),
               "disabled global interrupt gate does not dispatch")) {
        return 1;
    }
    *reg(&mmio, GAHBCFG_OFFSET) = GAHBCFG_GINT;
    *reg(&mmio, GINTSTS_OFFSET) = 0;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 9);
    if (expect(completed == 0 &&
                   dm_usb_host_channel_completion_pending(&first.completion),
               "inactive global status does not dispatch")) {
        return 1;
    }
    *reg(&mmio, GINTSTS_OFFSET) = GINTSTS_HCINT;
    *reg(&mmio, GINTMSK_OFFSET) = 0;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 9);
    if (expect(completed == 0 &&
                   dm_usb_host_channel_completion_pending(&first.completion),
               "masked global status does not dispatch")) {
        return 1;
    }
    *reg(&mmio, GINTMSK_OFFSET) = GINTSTS_HCINT;
    *reg(&mmio, HAINTMSK_OFFSET) = 0;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 10);
    if (expect(completed == 0 &&
                   dm_usb_host_channel_completion_pending(&first.completion) &&
                   *channel_reg(&mmio, 1, HCINT_OFFSET) == 0,
               "masked channel does not mutate pending operation") ||
        expect(*reg(&mmio, HAINTMSK_OFFSET) == 0,
               "channel mask remains disabled")) {
        return 1;
    }

    *reg(&mmio, HAINTMSK_OFFSET) = 1u << 1;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 11);
    if (expect(completed == 0 &&
                   dm_usb_host_channel_completion_pending(&first.completion),
               "pending channel event does not mutate without HCINT")) {
        return 1;
    }

    *channel_reg(&mmio, 1, HCTSIZ_OFFSET) = 1u << 19;
    *channel_reg(&mmio, 1, HCINT_OFFSET) = HCINT_XFRC | HCINT_CHHLTD;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 12);
    if (expect(completed == 1 &&
                   dispatch.async[1] == NULL &&
                   !dm_usb_host_channel_completion_pending(&first.completion) &&
                   dm_usb_host_channel_completion_result(
                       &first.completion, &result, &actual_length,
                       &timestamp_ns) &&
                   result == DM_USB_HOST_ENDPOINT_ACCEPTED &&
                   actual_length == sizeof(out_data) && timestamp_ns == 12 &&
                   !allocator.entry[0].owner,
               "accepted channel IRQ completes and releases its slot")) {
        return 1;
    }

    *reg(&mmio, HAINT_OFFSET) = (1u << 1) | (1u << 3) | (1u << 7);
    *reg(&mmio, HAINTMSK_OFFSET) = (1u << 1) | (1u << 3) | (1u << 7);
    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_start(
                   &dispatch, &first, (uintptr_t)mmio.words, &allocator,
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA1, out_data, 1,
                   NULL, 0, &first_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
                   dm_stm32h7_usb_host_pipe_async_dispatch_start(
                       &dispatch, &second, (uintptr_t)mmio.words, &allocator,
                       &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data, 2,
                       NULL, 0, &second_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
                   dm_stm32h7_usb_host_pipe_async_dispatch_start(
                       &dispatch, &third, (uintptr_t)mmio.words, &allocator,
                       &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data, 1,
                       NULL, 0, &third_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
                   dispatch.async[1] == &first && dispatch.async[3] == &second &&
                   dispatch.async[7] == &third,
               "register three independent channel operations")) {
        return 1;
    }
    *channel_reg(&mmio, 1, HCTSIZ_OFFSET) = 1u << 19;
    *channel_reg(&mmio, 3, HCTSIZ_OFFSET) = 1u << 19;
    *channel_reg(&mmio, 7, HCTSIZ_OFFSET) = 1u << 19;
    *channel_reg(&mmio, 1, HCINT_OFFSET) = HCINT_XFRC | HCINT_CHHLTD;
    *channel_reg(&mmio, 3, HCINT_OFFSET) = HCINT_XFRC | HCINT_CHHLTD;
    *channel_reg(&mmio, 7, HCINT_OFFSET) = HCINT_NAK;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 20);
    if (expect(completed == 3 && dispatch.async[1] == NULL &&
                   dispatch.async[3] == NULL && dispatch.async[7] == NULL &&
                   dm_usb_host_channel_completion_result(
                       &third.completion, &result, &actual_length,
                       &timestamp_ns) &&
                   result == DM_USB_HOST_ENDPOINT_NAK &&
                   actual_length == 0 && timestamp_ns == 20 &&
                   (*channel_reg(&mmio, 7, HCCHAR_OFFSET) & HCCHAR_CHDIS) &&
                   !allocator.entry[0].owner && !allocator.entry[1].owner &&
                   !allocator.entry[2].owner,
               "one IRQ dispatch routes accepted and NAK channels")) {
        return 1;
    }

    if (expect(dm_stm32h7_usb_host_pipe_async_start(
                   &first, (uintptr_t)mmio.words, &allocator, &pipe,
                   DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data, 1,
                   NULL, 0, &first_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
                   dm_stm32h7_usb_host_pipe_async_dispatch_register(
                       &dispatch, &first, &first_token) &&
                   dispatch.async[1] == &first,
               "register an already-started channel") ||
        expect(dm_stm32h7_usb_host_pipe_async_dispatch_cancel(
                   &dispatch, &first, &first_token, 40) &&
                   dispatch.async[1] == NULL &&
                   !dm_usb_host_channel_completion_pending(&first.completion) &&
                   !allocator.entry[0].owner,
               "explicit cancellation removes the dispatch slot")) {
        return 1;
    }

    if (expect(dm_stm32h7_usb_host_pipe_async_dispatch_start(
                   &dispatch, &second, (uintptr_t)mmio.words, &allocator,
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, out_data, 1,
                   NULL, 0, &second_token) ==
                   DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED,
               "start an operation for external cancellation") ||
        expect(dm_stm32h7_usb_host_pipe_async_cancel(
                   &second, &second_token, 50) && dispatch.async[1] == &second,
               "external cancellation leaves stale dispatch state observable") ) {
        return 1;
    }
    enable_channel_irq(&mmio, 1);
    *channel_reg(&mmio, 1, HCINT_OFFSET) = 0;
    completed = dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 51);
    if (expect(completed == 0 && dispatch.async[1] == NULL,
               "IRQ dispatch clears an externally cancelled slot")) {
        return 1;
    }

    if (expect(lock.enters == lock.leaves && lock.enters >= 10,
               "every dispatcher operation pairs lock callbacks")) {
        return 1;
    }
    puts("RESULT: STM32H7 USB host async dispatch smoke passed");
    return 0;
}
