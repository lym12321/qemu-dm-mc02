/* STM32H7 PIO adapter for a controller-independent periodic USB poller. */
#include "dm_stm32h7_usb_host_periodic.h"

#include "dm_usb_host_channel_operation.h"

static DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_periodic_pipe_transfer(
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

static DmUsbHostPeriodicSubmitResult dm_stm32h7_usb_host_periodic_submit(
    void *opaque, const DmUsbHostPipe *pipe, DmUsbHostEndpointDataPid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion,
    size_t *actual_length)
{
    DmStm32H7UsbHostPeriodic *periodic = opaque;
    DmStm32H7UsbHostPipeClient pipe_client;
    DmUsbHostChannelOperation operation;
    DmStm32H7UsbHostPipeResult result;

    if (!dm_usb_host_channel_operation_init(&operation) ||
        !dm_usb_host_channel_operation_begin(
            &operation, periodic->allocator)) {
        return DM_USB_HOST_PERIODIC_SUBMIT_DEFERRED;
    }
    if (!dm_stm32h7_usb_host_pipe_client_init(
            &pipe_client, periodic->base, periodic->allocator,
            dm_usb_host_channel_operation_lease(&operation))) {
        dm_usb_host_channel_operation_cancel(&operation);
        return DM_USB_HOST_PERIODIC_SUBMIT_INVALID;
    }
    result = periodic->transfer(
        periodic->transfer_opaque,
        &pipe_client, pipe,
        pid == DM_USB_HOST_ENDPOINT_PID_DATA0 ?
        DM_STM32H7_USB_HOST_PIPE_PID_DATA0 :
        DM_STM32H7_USB_HOST_PIPE_PID_DATA1,
        out_data, out_length, in_data, in_capacity, actual_length);
    if (!dm_usb_host_channel_operation_complete(&operation)) {
        return DM_USB_HOST_PERIODIC_SUBMIT_INVALID;
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
        return DM_USB_HOST_PERIODIC_SUBMIT_INVALID;
    }
    return DM_USB_HOST_PERIODIC_SUBMIT_OK;
}

DmUsbHostPeriodicScheduleResult dm_stm32h7_usb_host_periodic_init_with_transfer(
    DmStm32H7UsbHostPeriodic *periodic,
    uintptr_t base, DmUsbHostChannelAllocator *allocator,
    DmStm32H7UsbHostPipeTransfer *transfer, void *transfer_opaque,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns)
{
    return dm_stm32h7_usb_host_periodic_init_with_transfer_and_retry(
        periodic, base, allocator, transfer, transfer_opaque, endpoint_state,
        speed, origin_ns, NULL);
}

DmUsbHostPeriodicScheduleResult
dm_stm32h7_usb_host_periodic_init_with_transfer_and_retry(
    DmStm32H7UsbHostPeriodic *periodic,
    uintptr_t base, DmUsbHostChannelAllocator *allocator,
    DmStm32H7UsbHostPipeTransfer *transfer, void *transfer_opaque,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns, const DmUsbHostRetryConfig *retry_config)
{
    if (!periodic || !allocator || !transfer) {
        return DM_USB_HOST_PERIODIC_SCHEDULE_INVALID;
    }
    periodic->base = base;
    periodic->allocator = allocator;
    periodic->transfer = transfer;
    periodic->transfer_opaque = transfer_opaque;
    return dm_usb_host_periodic_poller_init_with_retry(
        &periodic->poller, endpoint_state, speed, origin_ns,
        retry_config, dm_stm32h7_usb_host_periodic_submit, periodic);
}

DmUsbHostPeriodicScheduleResult dm_stm32h7_usb_host_periodic_init_with_retry(
    DmStm32H7UsbHostPeriodic *periodic,
    uintptr_t base, DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns, const DmUsbHostRetryConfig *retry_config)
{
    return dm_stm32h7_usb_host_periodic_init_with_transfer_and_retry(
        periodic, base, allocator, dm_stm32h7_usb_host_periodic_pipe_transfer,
        NULL, endpoint_state, speed, origin_ns, retry_config);
}

DmUsbHostPeriodicScheduleResult dm_stm32h7_usb_host_periodic_init(
    DmStm32H7UsbHostPeriodic *periodic,
    uintptr_t base, DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns)
{
    return dm_stm32h7_usb_host_periodic_init_with_transfer(
        periodic, base, allocator, dm_stm32h7_usb_host_periodic_pipe_transfer,
        NULL, endpoint_state, speed, origin_ns);
}

DmUsbHostPeriodicPollResult dm_stm32h7_usb_host_periodic_poll(
    DmStm32H7UsbHostPeriodic *periodic, uint64_t timestamp_ns,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion)
{
    if (!periodic) {
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }
    return dm_usb_host_periodic_poller_poll(
        &periodic->poller, timestamp_ns, out_data, out_length, in_data,
        in_capacity, completion);
}
