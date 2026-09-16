/* H723 host-channel IRQ dispatch for stable asynchronous PIO operations. */
#ifndef DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_H
#define DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_H

#include "dm_stm32h7_usb_host_pipe.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY 12u

typedef void DmStm32H7UsbHostPipeAsyncDispatchLock(void *opaque);

typedef enum DmStm32H7UsbHostPipeAsyncDispatchResult {
    DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_NO_EVENT,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_PENDING,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_COMPLETED,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_INVALID,
} DmStm32H7UsbHostPipeAsyncDispatchResult;

typedef struct DmStm32H7UsbHostPipeAsyncDispatch {
    uintptr_t base;
    DmStm32H7UsbHostPipeAsync *async[
        DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY];
    DmUsbHostChannelCompletionToken token[
        DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY];
    DmStm32H7UsbHostPipeAsyncDispatchLock *enter;
    DmStm32H7UsbHostPipeAsyncDispatchLock *leave;
    void *lock_opaque;
    bool initialized;
} DmStm32H7UsbHostPipeAsyncDispatch;

/* Zero-initialize before first init(). A matching enter/leave pair is
 * optional for serialized fixtures.
 * Reinitialization is rejected while any dispatch slot is occupied. */
bool dm_stm32h7_usb_host_pipe_async_dispatch_init(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, uintptr_t base,
    DmStm32H7UsbHostPipeAsyncDispatchLock *enter,
    DmStm32H7UsbHostPipeAsyncDispatchLock *leave, void *lock_opaque);

/* Track an already-started pending operation for its leased channel. */
bool dm_stm32h7_usb_host_pipe_async_dispatch_register(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token);

/* Start and register atomically with respect to the supplied IRQ boundary. */
DmStm32H7UsbHostPipeAsyncStartResult
dm_stm32h7_usb_host_pipe_async_dispatch_start(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    DmStm32H7UsbHostPipeAsync *async, uintptr_t base,
    DmUsbHostChannelAllocator *allocator, const DmUsbHostPipe *pipe,
    DmStm32H7UsbHostPipePid pid, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    DmUsbHostChannelCompletionToken *token);

/* Consume only the matching channel event for one pending token. */
DmStm32H7UsbHostPipeAsyncDispatchResult
dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, unsigned channel,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns);

/* Dispatch terminal channel IRQ observations; return terminal count. */
unsigned dm_stm32h7_usb_host_pipe_async_dispatch_handle(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, uint64_t timestamp_ns);

/* Cancel a tracked operation and remove it from the dispatch table. */
bool dm_stm32h7_usb_host_pipe_async_dispatch_cancel(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns);

#endif /* DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_H */
