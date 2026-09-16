/* Completion-driven STM32H7 PIO bulk transfer composition. */
#include "dm_stm32h7_usb_host_bulk_async.h"

static DmStm32H7UsbHostBulkAsyncResult
dm_stm32h7_usb_host_bulk_async_submit_current(
    DmStm32H7UsbHostBulkAsync *bulk,
    DmUsbHostChannelCompletionToken *token)
{
    const DmUsbHostPipe *pipe;
    DmUsbHostEndpointDataPid pid;
    DmUsbHostEndpointStateResult state_result;
    const uint8_t *packet_out = NULL;
    uint8_t *packet_in = NULL;
    size_t requested_length;
    DmStm32H7UsbHostPipeAsyncStartResult start_result;

    if (!bulk || !bulk->initialized || !bulk->dispatch ||
        !bulk->allocator || !bulk->endpoint_state || !token ||
        bulk->state != DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY) {
        return DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
    }
    pipe = &bulk->endpoint_state->pipe;
    state_result = dm_usb_host_endpoint_state_prepare(
        bulk->endpoint_state, &pipe, &pid);
    if (state_result == DM_USB_HOST_ENDPOINT_STATE_HALTED) {
        bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED;
        bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_STALL;
        return bulk->last_result;
    }
    if (state_result != DM_USB_HOST_ENDPOINT_STATE_OK) {
        bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED;
        bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
        return bulk->last_result;
    }

    if (pipe->direction_in) {
        requested_length = bulk->in_capacity - bulk->actual_length;
        if (requested_length > pipe->max_packet_size) {
            requested_length = pipe->max_packet_size;
        }
        packet_in = bulk->in_data + bulk->actual_length;
    } else {
        requested_length = bulk->out_length - bulk->offset;
        if (requested_length > pipe->max_packet_size) {
            requested_length = pipe->max_packet_size;
        }
        if (bulk->offset == bulk->out_length) {
            requested_length = 0;
            bulk->packet_is_zero = true;
        }
        if (bulk->out_data) {
            packet_out = bulk->out_data + bulk->offset;
        }
    }
    if (!dm_usb_host_endpoint_request_validate(
            pipe, packet_out, pipe->direction_in ? 0 : requested_length,
            packet_in, pipe->direction_in ? requested_length : 0,
            &requested_length)) {
        bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED;
        bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
        return bulk->last_result;
    }

    start_result = dm_stm32h7_usb_host_pipe_async_dispatch_start(
        bulk->dispatch, &bulk->packet, bulk->base, bulk->allocator, pipe,
        pid == DM_USB_HOST_ENDPOINT_PID_DATA0 ?
        DM_STM32H7_USB_HOST_PIPE_PID_DATA0 :
        DM_STM32H7_USB_HOST_PIPE_PID_DATA1,
        packet_out, pipe->direction_in ? 0 : requested_length,
        packet_in, pipe->direction_in ? requested_length : 0, token);
    if (start_result == DM_STM32H7_USB_HOST_PIPE_ASYNC_DEFERRED) {
        return DM_STM32H7_USB_HOST_BULK_ASYNC_DEFERRED;
    }
    if (start_result != DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED) {
        bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED;
        bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
        return bulk->last_result;
    }
    bulk->packet_requested = requested_length;
    bulk->token = *token;
    bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_PENDING;
    return DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED;
}

static DmStm32H7UsbHostBulkAsyncResult
dm_stm32h7_usb_host_bulk_async_finish_terminal(
    DmStm32H7UsbHostBulkAsync *bulk,
    DmStm32H7UsbHostBulkAsyncResult result)
{
    bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED;
    bulk->last_result = result;
    return result;
}

bool dm_stm32h7_usb_host_bulk_async_init(
    DmStm32H7UsbHostBulkAsync *bulk,
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    uintptr_t base, DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state)
{
    if (!bulk || !dispatch || !allocator || !endpoint_state ||
        endpoint_state->pipe.transfer_type != DM_USB_HOST_PIPE_BULK ||
        (endpoint_state->pipe.attributes & 3u) != DM_USB_HOST_PIPE_BULK ||
        !endpoint_state->pipe.max_packet_size ||
        endpoint_state->pipe.transactions_per_microframe != 1 ||
        (bulk->initialized &&
         (bulk->state == DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY ||
          bulk->state == DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_PENDING))) {
        return false;
    }
    if (!dm_stm32h7_usb_host_pipe_async_init(&bulk->packet)) {
        return false;
    }
    bulk->dispatch = dispatch;
    bulk->allocator = allocator;
    bulk->endpoint_state = endpoint_state;
    bulk->base = base;
    bulk->out_data = NULL;
    bulk->out_length = 0;
    bulk->in_data = NULL;
    bulk->in_capacity = 0;
    bulk->offset = 0;
    bulk->actual_length = 0;
    bulk->packet_requested = 0;
    bulk->append_zero_packet = false;
    bulk->zero_packet_pending = false;
    bulk->packet_is_zero = false;
    bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_IDLE;
    bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
    bulk->token = (DmUsbHostChannelCompletionToken) { 0 };
    bulk->initialized = true;
    return true;
}

DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_start(
    DmStm32H7UsbHostBulkAsync *bulk, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, DmUsbHostChannelCompletionToken *token)
{
    const DmUsbHostPipe *pipe;

    if (!bulk || !bulk->initialized || !token ||
        (bulk->state != DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_IDLE &&
         bulk->state != DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_COMPLETED &&
         bulk->state != DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_CANCELLED)) {
        return DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
    }
    pipe = &bulk->endpoint_state->pipe;
    if (pipe->direction_in) {
        if (out_data || out_length || (in_capacity && !in_data)) {
            return DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
        }
    } else if (in_data || in_capacity || (out_length && !out_data)) {
        return DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
    }

    bulk->out_data = out_data;
    bulk->out_length = out_length;
    bulk->in_data = in_data;
    bulk->in_capacity = in_capacity;
    bulk->offset = 0;
    bulk->actual_length = 0;
    bulk->append_zero_packet = append_zero_packet;
    bulk->zero_packet_pending = !pipe->direction_in && append_zero_packet &&
                                !(out_length % pipe->max_packet_size);
    bulk->packet_is_zero = false;
    bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY;
    bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_PENDING;

    if ((!pipe->direction_in && !out_length &&
         !bulk->zero_packet_pending) ||
        (pipe->direction_in && !in_capacity)) {
        return dm_stm32h7_usb_host_bulk_async_finish_terminal(
            bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_OK);
    }
    return dm_stm32h7_usb_host_bulk_async_submit_current(bulk, token);
}

DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_resume(
    DmStm32H7UsbHostBulkAsync *bulk,
    DmUsbHostChannelCompletionToken *token)
{
    if (!bulk || !bulk->initialized || bulk->state !=
            DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY) {
        return DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
    }
    return dm_stm32h7_usb_host_bulk_async_submit_current(bulk, token);
}

DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_poll(
    DmStm32H7UsbHostBulkAsync *bulk,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns,
    size_t *actual_length, DmUsbHostChannelCompletionToken *next_token)
{
    DmUsbHostEndpointCompletion completion;
    DmUsbHostChannelLease lease;
    size_t packet_actual;
    uint64_t completion_timestamp;
    DmUsbHostEndpointStateResult state_result;
    DmStm32H7UsbHostPipeAsyncDispatchResult dispatch_result;
    DmStm32H7UsbHostBulkAsyncResult result;

    if (!bulk || !bulk->initialized || !token || !actual_length ||
        !next_token || bulk->state !=
            DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_PENDING ||
        token->completion != bulk->token.completion ||
        token->generation != bulk->token.generation) {
        return DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
    }
    *actual_length = bulk->actual_length;
    *next_token = (DmUsbHostChannelCompletionToken) { 0 };
    if (dm_usb_host_channel_completion_pending(&bulk->packet.completion)) {
        if (!dm_usb_host_channel_completion_lease(
                &bulk->packet.completion, &lease)) {
            return dm_stm32h7_usb_host_bulk_async_finish_terminal(
                bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID);
        }
        dispatch_result = dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel(
            bulk->dispatch, lease.channel, token, timestamp_ns);
        if (dispatch_result ==
                DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_PENDING) {
            return DM_STM32H7_USB_HOST_BULK_ASYNC_PENDING;
        }
        if (dispatch_result ==
                DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_INVALID &&
            dm_usb_host_channel_completion_pending(&bulk->packet.completion)) {
            if (!dm_stm32h7_usb_host_pipe_async_cancel(
                    &bulk->packet, token, timestamp_ns)) {
                dm_usb_host_channel_completion_cancel(
                    &bulk->packet.completion, token, timestamp_ns);
            }
            return dm_stm32h7_usb_host_bulk_async_finish_terminal(
                bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID);
        }
        if (dm_usb_host_channel_completion_pending(
                &bulk->packet.completion)) {
            return DM_STM32H7_USB_HOST_BULK_ASYNC_PENDING;
        }
    }
    if (!dm_usb_host_channel_completion_result(
            &bulk->packet.completion, &completion, &packet_actual,
            &completion_timestamp)) {
        return dm_stm32h7_usb_host_bulk_async_finish_terminal(
            bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID);
    }
    state_result = dm_usb_host_endpoint_state_complete(
        bulk->endpoint_state, completion, bulk->packet_requested,
        packet_actual);
    if (state_result != DM_USB_HOST_ENDPOINT_STATE_OK) {
        return dm_stm32h7_usb_host_bulk_async_finish_terminal(
            bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID);
    }
    if (completion == DM_USB_HOST_ENDPOINT_NAK) {
        bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY;
        bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_NAK;
        return bulk->last_result;
    }
    if (completion == DM_USB_HOST_ENDPOINT_STALL) {
        return dm_stm32h7_usb_host_bulk_async_finish_terminal(
            bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_STALL);
    }
    if (completion == DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR) {
        return dm_stm32h7_usb_host_bulk_async_finish_terminal(
            bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_TRANSACTION_ERROR);
    }

    bulk->actual_length += packet_actual;
    if (!bulk->endpoint_state->pipe.direction_in) {
        bulk->offset += packet_actual;
    }
    if (bulk->packet_is_zero) {
        bulk->zero_packet_pending = false;
    }
    *actual_length = bulk->actual_length;
    if (packet_actual < bulk->packet_requested ||
        (bulk->endpoint_state->pipe.direction_in &&
         bulk->actual_length == bulk->in_capacity) ||
        (!bulk->endpoint_state->pipe.direction_in &&
         bulk->offset == bulk->out_length &&
         !bulk->zero_packet_pending)) {
        return dm_stm32h7_usb_host_bulk_async_finish_terminal(
            bulk, DM_STM32H7_USB_HOST_BULK_ASYNC_OK);
    }

    bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_READY;
    result = dm_stm32h7_usb_host_bulk_async_submit_current(
        bulk, next_token);
    if (result == DM_STM32H7_USB_HOST_BULK_ASYNC_DEFERRED) {
        return result;
    }
    if (result != DM_STM32H7_USB_HOST_BULK_ASYNC_STARTED) {
        return result;
    }
    return result;
}

bool dm_stm32h7_usb_host_bulk_async_cancel(
    DmStm32H7UsbHostBulkAsync *bulk,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns)
{
    if (!bulk || !bulk->initialized || bulk->state !=
            DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_PENDING || !token ||
        !dm_usb_host_channel_completion_token_active(
            &bulk->packet.completion, token) ||
        !dm_stm32h7_usb_host_pipe_async_dispatch_cancel(
            bulk->dispatch, &bulk->packet, token, timestamp_ns)) {
        return false;
    }
    bulk->state = DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_CANCELLED;
    bulk->last_result = DM_STM32H7_USB_HOST_BULK_ASYNC_INVALID;
    return true;
}

DmStm32H7UsbHostBulkAsyncState dm_stm32h7_usb_host_bulk_async_state(
    const DmStm32H7UsbHostBulkAsync *bulk)
{
    return bulk ? bulk->state : DM_STM32H7_USB_HOST_BULK_ASYNC_STATE_IDLE;
}
