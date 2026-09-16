/* Single-packet STM32H7 DWC2 host client driven by a generic endpoint pipe. */
#ifndef DM_STM32H7_USB_HOST_PIPE_H
#define DM_STM32H7_USB_HOST_PIPE_H

#include "dm_usb_host_channel_allocator.h"
#include "dm_usb_host_channel_completion.h"
#include "dm_usb_host_pipe.h"
#include "dm_usb_host_endpoint_state.h"

#include <stddef.h>
#include <stdint.h>

typedef enum DmStm32H7UsbHostPipeResult {
    DM_STM32H7_USB_HOST_PIPE_OK,
    DM_STM32H7_USB_HOST_PIPE_NAK,
    DM_STM32H7_USB_HOST_PIPE_STALL,
    DM_STM32H7_USB_HOST_PIPE_TRANSACTION_ERROR,
    DM_STM32H7_USB_HOST_PIPE_INVALID,
} DmStm32H7UsbHostPipeResult;

typedef enum DmStm32H7UsbHostPipePid {
    DM_STM32H7_USB_HOST_PIPE_PID_DATA0 = 0,
    DM_STM32H7_USB_HOST_PIPE_PID_DATA1 = 2,
} DmStm32H7UsbHostPipePid;

typedef struct DmStm32H7UsbHostPipeClient {
    uintptr_t base;
    DmUsbHostChannelAllocator *allocator;
    const DmUsbHostChannelLease *lease;
} DmStm32H7UsbHostPipeClient;

typedef DmStm32H7UsbHostPipeResult DmStm32H7UsbHostPipeTransfer(
    void *opaque, DmStm32H7UsbHostPipeClient *client,
    const DmUsbHostPipe *pipe, DmStm32H7UsbHostPipePid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length);

/* Validate and encode one pipe packet without touching controller registers. */
DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_pipe_encode(
    const DmUsbHostPipe *pipe, DmStm32H7UsbHostPipePid pid, size_t length,
    uint32_t *hcchar, uint32_t *hctsiz);

/* Bind one active generic lease to an H723 PIO client. */
bool dm_stm32h7_usb_host_pipe_client_init(
    DmStm32H7UsbHostPipeClient *client, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    const DmUsbHostChannelLease *lease);

/* Submit one PIO packet; callers retain endpoint scheduling and PID policy. */
DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_pipe_transfer(
    DmStm32H7UsbHostPipeClient *client, const DmUsbHostPipe *pipe,
    DmStm32H7UsbHostPipePid pid, const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, size_t *actual_length);

/* Submit one packet and apply its completion to a generic endpoint state. */
DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_endpoint_transfer(
    DmStm32H7UsbHostPipeClient *client, DmUsbHostEndpointState *state,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length);

typedef enum DmStm32H7UsbHostPipeAsyncStartResult {
    DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_DEFERRED,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID,
} DmStm32H7UsbHostPipeAsyncStartResult;

typedef enum DmStm32H7UsbHostPipeAsyncPollResult {
    DM_STM32H7_USB_HOST_PIPE_ASYNC_PENDING,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_OK,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_NAK,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_STALL,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_TRANSACTION_ERROR,
    DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID,
} DmStm32H7UsbHostPipeAsyncPollResult;

typedef struct DmStm32H7UsbHostPipeAsync {
    DmUsbHostChannelCompletion completion;
    DmUsbHostChannelAllocator *allocator;
    uintptr_t base;
    uint32_t channel_config;
    size_t requested_length;
    uint8_t *in_data;
    bool direction_in;
} DmStm32H7UsbHostPipeAsync;

/* Initialize a stable object used across controller start and IRQ events. */
bool dm_stm32h7_usb_host_pipe_async_init(
    DmStm32H7UsbHostPipeAsync *async);

/* Program one packet and return while the channel remains pending. */
DmStm32H7UsbHostPipeAsyncStartResult
dm_stm32h7_usb_host_pipe_async_start(
    DmStm32H7UsbHostPipeAsync *async, uintptr_t base,
    DmUsbHostChannelAllocator *allocator, const DmUsbHostPipe *pipe,
    DmStm32H7UsbHostPipePid pid, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    DmUsbHostChannelCompletionToken *token);

/* Consume one controller IRQ observation or return PENDING without mutation. */
DmStm32H7UsbHostPipeAsyncPollResult
dm_stm32h7_usb_host_pipe_async_poll(
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns);

/* Stop a pending channel and commit cancellation at the supplied virtual time. */
bool dm_stm32h7_usb_host_pipe_async_cancel(
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns);

#endif /* DM_STM32H7_USB_HOST_PIPE_H */
