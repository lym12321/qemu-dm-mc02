/* Completion-driven STM32H7 PIO bulk transfer composition. */
#ifndef DM_STM32H7_USB_HOST_BULK_ASYNC_H
#define DM_STM32H7_USB_HOST_BULK_ASYNC_H

#include "dm_stm32h7_usb_host_pipe_async_dispatch.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum DmStm32H7UsbHostBulkAsyncState {
    DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_IDLE,
    /* The transfer has a current packet that can be submitted. */
    DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY,
    DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_PENDING,
    DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED,
    DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_CANCELLED,
} DmStm32H7UsbHostBulkAsyncState;

typedef enum DmStm32H7UsbHostBulkAsyncResult {
    /* A packet is pending; STARTED after completion means the next packet. */
    DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED,
    DM_STM32H7_USB_HOST_BULK_ASYNC_PENDING,
    DM_STM32H7_USB_HOST_BULK_ASYNC_OK,
    /* The current packet is ready for an explicit resume/retry. */
    DM_STM32H7_USB_HOST_BULK_ASYNC_NAK,
    DM_STM32H7_USB_HOST_BULK_ASYNC_STALL,
    DM_STM32H7_USB_HOST_BULK_ASYNC_TRANSACTION_ERROR,
    DM_STM32H7_USB_HOST_BULK_ASYNC_DEFERRED,
    DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID,
} DmStm32H7UsbHostBulkAsyncResult;

typedef struct DmStm32H7UsbHostBulkAsync {
    DmStm32H7UsbHostPipeAsync packet;
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch;
    DmUsbHostChannelAllocator *allocator;
    DmUsbHostEndpointState *endpoint_state;
    uintptr_t base;
    const uint8_t *out_data;
    size_t out_length;
    uint8_t *in_data;
    size_t in_capacity;
    size_t offset;
    size_t actual_length;
    size_t packet_requested;
    bool append_zero_packet;
    bool zero_packet_pending;
    bool packet_is_zero;
    bool initialized;
    DmStm32H7UsbHostBulkAsyncState state;
    DmStm32H7UsbHostBulkAsyncResult last_result;
    DmUsbHostChannelCompletionToken token;
} DmStm32H7UsbHostBulkAsync;

/* Zero-initialize before the first init; reinit is rejected mid-transfer. */
bool dm_stm32h7_usb_host_bulk_async_init(
    DmStm32H7UsbHostBulkAsync *bulk,
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    uintptr_t base, DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state);

/* Start a multi-packet request; empty requests complete synchronously. */
DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_start(
    DmStm32H7UsbHostBulkAsync *bulk, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, DmUsbHostChannelCompletionToken *token);

/* Submit a ready packet after channel exhaustion or a NAK. */
DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_resume(
    DmStm32H7UsbHostBulkAsync *bulk,
    DmUsbHostChannelCompletionToken *token);

/* Consume the current channel event and optionally start the next packet. */
DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_poll(
    DmStm32H7UsbHostBulkAsync *bulk,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns,
    size_t *actual_length, DmUsbHostChannelCompletionToken *next_token);

/* Cancel the current pending packet and release its channel lease. */
bool dm_stm32h7_usb_host_bulk_async_cancel(
    DmStm32H7UsbHostBulkAsync *bulk,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns);

DmStm32H7UsbHostBulkAsyncState dm_stm32h7_usb_host_bulk_async_state(
    const DmStm32H7UsbHostBulkAsync *bulk);

#endif /* DM_STM32H7_USB_HOST_BULK_ASYNC_H */
