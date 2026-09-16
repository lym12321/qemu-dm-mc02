/* STM32H7 PIO adapter for a controller-independent USB bulk transfer. */
#include "dm_stm32h7_usb_host_bulk.h"

#include "dm_usb_host_channel_operation.h"

static DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_bulk_pio_packet(
    void *opaque, DmStm32H7UsbHostPipeClient *client,
    const DmUsbHostPipe *pipe, DmStm32H7UsbHostPipePid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length)
{
    (void)opaque;
    return dm_stm32h7_usb_host_pipe_transfer(
        client, pipe, pid, out_data, out_length, in_data, in_capacity,
        actual_length);
}

static DmUsbHostBulkSubmitResult dm_stm32h7_usb_host_bulk_submit(
    void *opaque, const DmUsbHostPipe *pipe, DmUsbHostEndpointDataPid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion,
    size_t *actual_length)
{
    DmStm32H7UsbHostBulk *bulk = opaque;
    DmUsbHostChannelOperation operation;
    DmStm32H7UsbHostPipeClient client;
    DmStm32H7UsbHostPipeResult result;

    if (!dm_usb_host_channel_operation_init(&operation) ||
        !dm_usb_host_channel_operation_begin(&operation, bulk->allocator)) {
        return DM_USB_HOST_BULK_SUBMIT_DEFERRED;
    }
    if (!dm_stm32h7_usb_host_pipe_client_init(
            &client, bulk->base, bulk->allocator,
            dm_usb_host_channel_operation_lease(&operation))) {
        dm_usb_host_channel_operation_cancel(&operation);
        return DM_USB_HOST_BULK_SUBMIT_INVALID;
    }
    result = bulk->submit_packet(
        bulk->submit_opaque, &client, pipe,
        pid == DM_USB_HOST_ENDPOINT_PID_DATA0 ?
        DM_STM32H7_USB_HOST_PIPE_PID_DATA0 :
        DM_STM32H7_USB_HOST_PIPE_PID_DATA1,
        out_data, out_length, in_data, in_capacity, actual_length);
    if (!dm_usb_host_channel_operation_complete(&operation)) {
        return DM_USB_HOST_BULK_SUBMIT_INVALID;
    }
    switch (result) {
    case DM_STM32H7_USB_HOST_PIPE_OK:
        *completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
        break;
    case DM_STM32H7_USB_HOST_PIPE_NAK:
        *completion = DM_USB_HOST_ENDPOINT_NAK;
        break;
    case DM_STM32H7_USB_HOST_PIPE_STALL:
        *completion = DM_USB_HOST_ENDPOINT_STALL;
        break;
    case DM_STM32H7_USB_HOST_PIPE_TRANSACTION_ERROR:
        *completion = DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR;
        break;
    default:
        return DM_USB_HOST_BULK_SUBMIT_INVALID;
    }
    return DM_USB_HOST_BULK_SUBMIT_OK;
}

bool dm_stm32h7_usb_host_bulk_init_with_transfer(
    DmStm32H7UsbHostBulk *bulk, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    DmStm32H7UsbHostPipeTransfer *submit_packet, void *submit_opaque,
    DmUsbHostEndpointState *endpoint_state)
{
    if (!bulk || !allocator || !submit_packet || !endpoint_state) {
        return false;
    }
    bulk->base = base;
    bulk->allocator = allocator;
    bulk->submit_packet = submit_packet;
    bulk->submit_opaque = submit_opaque;
    return dm_usb_host_bulk_transfer_init(
        &bulk->transfer, endpoint_state, dm_stm32h7_usb_host_bulk_submit,
        bulk);
}

bool dm_stm32h7_usb_host_bulk_init(
    DmStm32H7UsbHostBulk *bulk, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state)
{
    return dm_stm32h7_usb_host_bulk_init_with_transfer(
        bulk, base, allocator, dm_stm32h7_usb_host_bulk_pio_packet, NULL,
        endpoint_state);
}

DmUsbHostBulkTransferResult dm_stm32h7_usb_host_bulk_run(
    DmStm32H7UsbHostBulk *bulk, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, size_t *actual_length)
{
    if (!bulk) {
        return DM_USB_HOST_BULK_INVALID;
    }
    return dm_usb_host_bulk_transfer_run(
        &bulk->transfer, out_data, out_length, in_data, in_capacity,
        append_zero_packet, actual_length);
}

DmUsbHostBulkTransferResult dm_stm32h7_usb_host_bulk_run_with_retry(
    DmStm32H7UsbHostBulk *bulk, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, DmUsbHostRetryPolicy *retry_policy,
    uint64_t timestamp_ns, size_t *actual_length)
{
    if (!bulk) {
        return DM_USB_HOST_BULK_INVALID;
    }
    return dm_usb_host_bulk_transfer_run_with_retry(
        &bulk->transfer, out_data, out_length, in_data, in_capacity,
        append_zero_packet, retry_policy, timestamp_ns, actual_length);
}
