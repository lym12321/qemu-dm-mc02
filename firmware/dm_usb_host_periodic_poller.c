/* Controller-independent periodic USB endpoint poll composition. */
#include "dm_usb_host_periodic_poller.h"

DmUsbHostPeriodicScheduleResult dm_usb_host_periodic_poller_init_with_retry(
    DmUsbHostPeriodicPoller *poller, DmUsbHostEndpointState *endpoint_state,
    DmUsbHostSpeed speed, uint64_t origin_ns,
    const DmUsbHostRetryConfig *retry_config,
    DmUsbHostPeriodicSubmit *submit, void *submit_opaque)
{
    DmUsbHostPeriodicScheduleResult result;

    if (!poller || !endpoint_state || !submit) {
        return DM_USB_HOST_PERIODIC_SCHEDULE_INVALID;
    }
    result = dm_usb_host_periodic_schedule_init(
        &poller->schedule, &endpoint_state->pipe, speed, origin_ns);
    if (result != DM_USB_HOST_PERIODIC_SCHEDULE_OK) {
        return result;
    }
    poller->endpoint_state = endpoint_state;
    poller->submit = submit;
    poller->submit_opaque = submit_opaque;
    poller->retry_enabled = false;
    poller->retry_pending = false;
    poller->retry_out_data = NULL;
    poller->retry_out_length = 0;
    poller->retry_in_data = NULL;
    poller->retry_in_capacity = 0;
    if (retry_config && !dm_usb_host_retry_init(&poller->retry,
                                                 *retry_config)) {
        return DM_USB_HOST_PERIODIC_SCHEDULE_INVALID;
    }
    poller->retry_enabled = retry_config != NULL;
    return DM_USB_HOST_PERIODIC_SCHEDULE_OK;
}

DmUsbHostPeriodicScheduleResult dm_usb_host_periodic_poller_init(
    DmUsbHostPeriodicPoller *poller, DmUsbHostEndpointState *endpoint_state,
    DmUsbHostSpeed speed, uint64_t origin_ns,
    DmUsbHostPeriodicSubmit *submit, void *submit_opaque)
{
    return dm_usb_host_periodic_poller_init_with_retry(
        poller, endpoint_state, speed, origin_ns, NULL, submit,
        submit_opaque);
}

DmUsbHostPeriodicPollResult dm_usb_host_periodic_poller_poll(
    DmUsbHostPeriodicPoller *poller, uint64_t timestamp_ns,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion)
{
    const DmUsbHostPipe *pipe;
    const uint8_t *submit_out_data = out_data;
    size_t submit_out_length = out_length;
    uint8_t *submit_in_data = in_data;
    size_t submit_in_capacity = in_capacity;
    DmUsbHostEndpointDataPid pid;
    DmUsbHostEndpointStateResult state_result;
    DmUsbHostPeriodicSubmitResult submit_result;
    DmUsbHostRetryResult retry_result;
    size_t actual_length;
    size_t requested_length;
    bool started_retry = false;

    if (!poller || !poller->endpoint_state || !poller->submit ||
        !completion) {
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }
    if (!dm_usb_host_periodic_schedule_eligible(&poller->schedule,
                                                 timestamp_ns)) {
        return DM_USB_HOST_PERIODIC_POLL_NOT_DUE;
    }

    state_result = dm_usb_host_endpoint_state_prepare(
        poller->endpoint_state, &pipe, &pid);
    if (state_result == DM_USB_HOST_ENDPOINT_STATE_HALTED) {
        if (poller->retry_enabled) {
            dm_usb_host_retry_reset(&poller->retry);
            poller->retry_pending = false;
        }
        return DM_USB_HOST_PERIODIC_POLL_HALTED;
    }
    if (state_result != DM_USB_HOST_ENDPOINT_STATE_OK) {
        if (poller->retry_enabled) {
            dm_usb_host_retry_reset(&poller->retry);
            poller->retry_pending = false;
        }
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }

    if (poller->retry_pending) {
        retry_result = dm_usb_host_retry_check(&poller->retry, timestamp_ns);
        if (retry_result == DM_USB_HOST_RETRY_TIMEOUT) {
            *completion = DM_USB_HOST_ENDPOINT_NAK;
            dm_usb_host_retry_finish(&poller->retry);
            poller->retry_pending = false;
            if (!dm_usb_host_periodic_schedule_advance(
                    &poller->schedule, timestamp_ns)) {
                return DM_USB_HOST_PERIODIC_POLL_INVALID;
            }
            return DM_USB_HOST_PERIODIC_POLL_RETRY_TIMEOUT;
        }
        if (retry_result != DM_USB_HOST_RETRY_RETRY) {
            dm_usb_host_retry_reset(&poller->retry);
            poller->retry_pending = false;
            return DM_USB_HOST_PERIODIC_POLL_INVALID;
        }
        submit_out_data = poller->retry_out_data;
        submit_out_length = poller->retry_out_length;
        submit_in_data = poller->retry_in_data;
        submit_in_capacity = poller->retry_in_capacity;
    } else if (poller->retry_enabled) {
        if (!dm_usb_host_retry_begin(&poller->retry, timestamp_ns)) {
            return DM_USB_HOST_PERIODIC_POLL_INVALID;
        }
        started_retry = true;
    }

    if (!dm_usb_host_endpoint_request_validate(
            pipe, submit_out_data, submit_out_length, submit_in_data,
            submit_in_capacity, &requested_length)) {
        if (poller->retry_enabled) {
            dm_usb_host_retry_reset(&poller->retry);
            poller->retry_pending = false;
        }
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }

    actual_length = 0;
    submit_result = poller->submit(
        poller->submit_opaque, pipe, pid, submit_out_data,
        submit_out_length, submit_in_data, submit_in_capacity, completion,
        &actual_length);
    if (submit_result == DM_USB_HOST_PERIODIC_SUBMIT_DEFERRED) {
        if (started_retry) {
            dm_usb_host_retry_reset(&poller->retry);
        }
        return DM_USB_HOST_PERIODIC_POLL_DEFERRED;
    }
    if (submit_result != DM_USB_HOST_PERIODIC_SUBMIT_OK) {
        if (poller->retry_enabled) {
            dm_usb_host_retry_reset(&poller->retry);
            poller->retry_pending = false;
        }
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }
    if (!dm_usb_host_endpoint_completion_validate(
            *completion, requested_length, actual_length)) {
        if (poller->retry_enabled) {
            dm_usb_host_retry_reset(&poller->retry);
            poller->retry_pending = false;
        }
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }
    state_result = dm_usb_host_endpoint_state_complete(
        poller->endpoint_state, *completion, requested_length, actual_length);
    if (state_result != DM_USB_HOST_ENDPOINT_STATE_OK) {
        if (poller->retry_enabled) {
            dm_usb_host_retry_reset(&poller->retry);
            poller->retry_pending = false;
        }
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }

    if (*completion == DM_USB_HOST_ENDPOINT_NAK && poller->retry_enabled) {
        retry_result = dm_usb_host_retry_on_nak(&poller->retry, timestamp_ns);
        if (retry_result == DM_USB_HOST_RETRY_RETRY) {
            poller->retry_pending = true;
            poller->retry_out_data = submit_out_data;
            poller->retry_out_length = submit_out_length;
            poller->retry_in_data = submit_in_data;
            poller->retry_in_capacity = submit_in_capacity;
            return DM_USB_HOST_PERIODIC_POLL_SUBMITTED;
        }
        if (retry_result == DM_USB_HOST_RETRY_EXHAUSTED) {
            dm_usb_host_retry_finish(&poller->retry);
            poller->retry_pending = false;
            if (!dm_usb_host_periodic_schedule_advance(
                    &poller->schedule, timestamp_ns)) {
                return DM_USB_HOST_PERIODIC_POLL_INVALID;
            }
            return DM_USB_HOST_PERIODIC_POLL_RETRY_EXHAUSTED;
        }
        if (retry_result == DM_USB_HOST_RETRY_TIMEOUT) {
            dm_usb_host_retry_finish(&poller->retry);
            poller->retry_pending = false;
            if (!dm_usb_host_periodic_schedule_advance(
                    &poller->schedule, timestamp_ns)) {
                return DM_USB_HOST_PERIODIC_POLL_INVALID;
            }
            return DM_USB_HOST_PERIODIC_POLL_RETRY_TIMEOUT;
        }
        dm_usb_host_retry_reset(&poller->retry);
        poller->retry_pending = false;
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }

    if (poller->retry_enabled) {
        dm_usb_host_retry_complete(&poller->retry);
        poller->retry_pending = false;
    }
    if (!dm_usb_host_periodic_schedule_advance(&poller->schedule,
                                                timestamp_ns)) {
        return DM_USB_HOST_PERIODIC_POLL_INVALID;
    }
    return DM_USB_HOST_PERIODIC_POLL_SUBMITTED;
}
