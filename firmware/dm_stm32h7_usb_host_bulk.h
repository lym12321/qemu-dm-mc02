/* STM32H7 PIO adapter for a controller-independent USB bulk transfer. */
#ifndef DM_STM32H7_USB_HOST_BULK_H
#define DM_STM32H7_USB_HOST_BULK_H

#include "dm_stm32h7_usb_host_pipe.h"
#include "dm_usb_host_bulk.h"

typedef struct DmStm32H7UsbHostBulk {
    DmUsbHostBulkTransfer transfer;
    uintptr_t base;
    DmUsbHostChannelAllocator *allocator;
    DmStm32H7UsbHostPipeTransfer *submit_packet;
    void *submit_opaque;
} DmStm32H7UsbHostBulk;

/* Bind a bulk endpoint to a caller-supplied H723 single-packet adapter. */
bool dm_stm32h7_usb_host_bulk_init_with_transfer(
    DmStm32H7UsbHostBulk *bulk, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    DmStm32H7UsbHostPipeTransfer *submit_packet, void *submit_opaque,
    DmUsbHostEndpointState *endpoint_state);

/* Bind a bulk endpoint to the real H723 PIO packet client. */
bool dm_stm32h7_usb_host_bulk_init(
    DmStm32H7UsbHostBulk *bulk, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state);

/* Run a synchronous multi-packet bulk request. */
DmUsbHostBulkTransferResult dm_stm32h7_usb_host_bulk_run(
    DmStm32H7UsbHostBulk *bulk, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, size_t *actual_length);

/* Continue a bulk request with a caller-owned virtual-time retry policy. */
DmUsbHostBulkTransferResult dm_stm32h7_usb_host_bulk_run_with_retry(
    DmStm32H7UsbHostBulk *bulk, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, DmUsbHostRetryPolicy *retry_policy,
    uint64_t timestamp_ns, size_t *actual_length);

#endif /* DM_STM32H7_USB_HOST_BULK_H */
