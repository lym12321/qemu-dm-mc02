/* Controller-independent multi-packet USB bulk transfer composition. */
#include "dm_usb_host_bulk.h"

static DmUsbHostBulkTransferResult dm_usb_host_bulk_completion_result(
    DmUsbHostEndpointCompletion completion)
{
    switch (completion) {
    case DM_USB_HOST_ENDPOINT_NAK:
        return DM_USB_HOST_BULK_NAK;
    case DM_USB_HOST_ENDPOINT_STALL:
        return DM_USB_HOST_BULK_STALL;
    case DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR:
        return DM_USB_HOST_BULK_TRANSACTION_ERROR;
    default:
        return DM_USB_HOST_BULK_INVALID;
    }
}

bool dm_usb_host_bulk_transfer_init(
    DmUsbHostBulkTransfer *transfer, DmUsbHostEndpointState *endpoint_state,
    DmUsbHostBulkSubmit *submit, void *submit_opaque)
{
    if (!transfer || !endpoint_state || !submit ||
        endpoint_state->pipe.transfer_type != DM_USB_HOST_PIPE_BULK ||
        (endpoint_state->pipe.attributes & 3u) != DM_USB_HOST_PIPE_BULK ||
        !endpoint_state->pipe.max_packet_size ||
        endpoint_state->pipe.transactions_per_microframe != 1) {
        return false;
    }
    *transfer = (DmUsbHostBulkTransfer) {
        .endpoint_state = endpoint_state,
        .submit = submit,
        .submit_opaque = submit_opaque,
    };
    return true;
}

DmUsbHostBulkTransferResult dm_usb_host_bulk_transfer_run_with_retry(
    DmUsbHostBulkTransfer *transfer, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, DmUsbHostRetryPolicy *retry_policy,
    uint64_t timestamp_ns, size_t *actual_length)
{
    const DmUsbHostPipe *pipe;
    DmUsbHostEndpointDataPid pid;
    DmUsbHostEndpointStateResult state_result;
    size_t total = 0;
    size_t offset = 0;
    bool zero_packet_pending;

    if (!transfer || !transfer->endpoint_state || !transfer->submit ||
        !actual_length || (retry_policy && !retry_policy->initialized)) {
        return DM_USB_HOST_BULK_INVALID;
    }
    pipe = &transfer->endpoint_state->pipe;
    if (pipe->direction_in) {
        if (out_data || out_length || (!in_data && in_capacity)) {
            if (retry_policy) {
                dm_usb_host_retry_reset(retry_policy);
            }
            return DM_USB_HOST_BULK_INVALID;
        }
    } else if (in_data || in_capacity || (!out_data && out_length)) {
        if (retry_policy) {
            dm_usb_host_retry_reset(retry_policy);
        }
        return DM_USB_HOST_BULK_INVALID;
    }
    zero_packet_pending = !pipe->direction_in && append_zero_packet &&
                          !(out_length % pipe->max_packet_size);
    *actual_length = 0;

    if (!pipe->direction_in && !out_length && !zero_packet_pending) {
        return DM_USB_HOST_BULK_OK;
    }
    if (pipe->direction_in && !in_capacity) {
        return DM_USB_HOST_BULK_OK;
    }
    for (;;) {
        const uint8_t *packet_out;
        uint8_t *packet_in;
        size_t requested_length;
        size_t packet_actual;
        DmUsbHostEndpointCompletion completion;
        DmUsbHostBulkSubmitResult submit_result;
        DmUsbHostRetryResult retry_result;
        bool started_retry = false;

        state_result = dm_usb_host_endpoint_state_prepare(
            transfer->endpoint_state, &pipe, &pid);
        if (state_result == DM_USB_HOST_ENDPOINT_STATE_HALTED) {
            if (retry_policy) {
                dm_usb_host_retry_reset(retry_policy);
            }
            return DM_USB_HOST_BULK_STALL;
        }
        if (state_result != DM_USB_HOST_ENDPOINT_STATE_OK) {
            if (retry_policy) {
                dm_usb_host_retry_reset(retry_policy);
            }
            return DM_USB_HOST_BULK_INVALID;
        }
        if (pipe->direction_in) {
            requested_length = in_capacity - total;
            if (requested_length > pipe->max_packet_size) {
                requested_length = pipe->max_packet_size;
            }
            packet_out = NULL;
            packet_in = in_data + total;
        } else {
            requested_length = out_length - offset;
            if (requested_length > pipe->max_packet_size) {
                requested_length = pipe->max_packet_size;
            }
            if (offset == out_length) {
                requested_length = 0;
                zero_packet_pending = false;
            }
            packet_out = out_data ? out_data + offset : NULL;
            packet_in = NULL;
        }
        if (!dm_usb_host_endpoint_request_validate(
                pipe, packet_out,
                pipe->direction_in ? 0 : requested_length, packet_in,
                pipe->direction_in ? requested_length : 0,
                &requested_length)) {
            if (retry_policy) {
                dm_usb_host_retry_reset(retry_policy);
            }
            *actual_length = total;
            return DM_USB_HOST_BULK_INVALID;
        }
        if (retry_policy) {
            if (retry_policy->active) {
                retry_result = dm_usb_host_retry_check(
                    retry_policy, timestamp_ns);
                if (retry_result == DM_USB_HOST_RETRY_TIMEOUT) {
                    *actual_length = total;
                    dm_usb_host_retry_finish(retry_policy);
                    return DM_USB_HOST_BULK_RETRY_TIMEOUT;
                }
                if (retry_result != DM_USB_HOST_RETRY_RETRY) {
                    *actual_length = total;
                    dm_usb_host_retry_reset(retry_policy);
                    return DM_USB_HOST_BULK_INVALID;
                }
            } else if (!dm_usb_host_retry_begin(
                           retry_policy, timestamp_ns)) {
                return DM_USB_HOST_BULK_INVALID;
            } else {
                started_retry = true;
            }
        }
        packet_actual = 0;
        submit_result = transfer->submit(
            transfer->submit_opaque, pipe, pid, packet_out,
            pipe->direction_in ? 0 : requested_length, packet_in,
            pipe->direction_in ? requested_length : 0,
            &completion, &packet_actual);
        if (submit_result == DM_USB_HOST_BULK_SUBMIT_DEFERRED) {
            if (retry_policy && started_retry) {
                dm_usb_host_retry_reset(retry_policy);
            }
            *actual_length = total;
            return DM_USB_HOST_BULK_DEFERRED;
        }
        if (submit_result != DM_USB_HOST_BULK_SUBMIT_OK) {
            if (retry_policy) {
                dm_usb_host_retry_reset(retry_policy);
            }
            *actual_length = total;
            return DM_USB_HOST_BULK_INVALID;
        }
        if (!dm_usb_host_endpoint_completion_validate(
                completion, requested_length, packet_actual)) {
            if (retry_policy) {
                dm_usb_host_retry_reset(retry_policy);
            }
            *actual_length = total;
            return DM_USB_HOST_BULK_INVALID;
        }
        state_result = dm_usb_host_endpoint_state_complete(
            transfer->endpoint_state, completion, requested_length,
            packet_actual);
        if (state_result != DM_USB_HOST_ENDPOINT_STATE_OK) {
            if (retry_policy) {
                dm_usb_host_retry_reset(retry_policy);
            }
            *actual_length = total;
            return DM_USB_HOST_BULK_INVALID;
        }
        if (completion == DM_USB_HOST_ENDPOINT_NAK && retry_policy) {
            retry_result = dm_usb_host_retry_on_nak(
                retry_policy, timestamp_ns);
            *actual_length = total;
            if (retry_result == DM_USB_HOST_RETRY_RETRY) {
                return DM_USB_HOST_BULK_RETRY;
            }
            if (retry_result == DM_USB_HOST_RETRY_EXHAUSTED) {
                dm_usb_host_retry_finish(retry_policy);
                return DM_USB_HOST_BULK_RETRY_EXHAUSTED;
            }
            if (retry_result == DM_USB_HOST_RETRY_TIMEOUT) {
                dm_usb_host_retry_finish(retry_policy);
                return DM_USB_HOST_BULK_RETRY_TIMEOUT;
            }
            dm_usb_host_retry_reset(retry_policy);
            return DM_USB_HOST_BULK_INVALID;
        }
        if (retry_policy) {
            if (completion == DM_USB_HOST_ENDPOINT_ACCEPTED) {
                dm_usb_host_retry_complete(retry_policy);
            } else {
                dm_usb_host_retry_reset(retry_policy);
            }
        }
        if (completion != DM_USB_HOST_ENDPOINT_ACCEPTED) {
            *actual_length = total;
            return dm_usb_host_bulk_completion_result(completion);
        }
        total += packet_actual;
        offset += pipe->direction_in ? 0 : packet_actual;
        *actual_length = total;
        if (packet_actual < requested_length) {
            return DM_USB_HOST_BULK_OK;
        }
        if (pipe->direction_in) {
            if (total == in_capacity) {
                return DM_USB_HOST_BULK_OK;
            }
        } else if (offset == out_length) {
            if (!zero_packet_pending) {
                return DM_USB_HOST_BULK_OK;
            }
            zero_packet_pending = false;
        }
    }
}

DmUsbHostBulkTransferResult dm_usb_host_bulk_transfer_run(
    DmUsbHostBulkTransfer *transfer, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    bool append_zero_packet, size_t *actual_length)
{
    return dm_usb_host_bulk_transfer_run_with_retry(
        transfer, out_data, out_length, in_data, in_capacity,
        append_zero_packet, NULL, 0, actual_length);
}
