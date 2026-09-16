/* Board- and controller-independent USB endpoint data-toggle state. */
#include "dm_usb_host_endpoint_state.h"

static bool dm_usb_host_endpoint_state_uses_toggle(
    const DmUsbHostEndpointState *state)
{
    return state->pipe.transfer_type != DM_USB_HOST_PIPE_ISOCHRONOUS;
}

void dm_usb_host_endpoint_state_init(DmUsbHostEndpointState *state,
                                     const DmUsbHostPipe *pipe)
{
    *state = (DmUsbHostEndpointState) {
        .pipe = *pipe,
        .next_pid = DM_USB_HOST_ENDPOINT_PID_DATA0,
    };
}

void dm_usb_host_endpoint_state_reset(DmUsbHostEndpointState *state)
{
    state->next_pid = DM_USB_HOST_ENDPOINT_PID_DATA0;
    state->halted = false;
}

void dm_usb_host_endpoint_state_clear_halt(DmUsbHostEndpointState *state)
{
    dm_usb_host_endpoint_state_reset(state);
}

bool dm_usb_host_endpoint_request_validate(
    const DmUsbHostPipe *pipe, const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, size_t *requested_length)
{
    size_t length;

    if (!pipe || !requested_length || !pipe->max_packet_size) {
        return false;
    }
    if (pipe->direction_in) {
        if (out_data || out_length || (in_capacity && !in_data)) {
            return false;
        }
        length = in_capacity;
    } else {
        if (in_data || in_capacity || (out_length && !out_data)) {
            return false;
        }
        length = out_length;
    }
    if (length > pipe->max_packet_size) {
        return false;
    }
    *requested_length = length;
    return true;
}

bool dm_usb_host_endpoint_completion_validate(
    DmUsbHostEndpointCompletion completion, size_t requested_length,
    size_t actual_length)
{
    if (completion > DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR ||
        actual_length > requested_length) {
        return false;
    }
    return completion == DM_USB_HOST_ENDPOINT_ACCEPTED || actual_length == 0;
}

DmUsbHostEndpointStateResult dm_usb_host_endpoint_state_prepare(
    const DmUsbHostEndpointState *state, const DmUsbHostPipe **pipe,
    DmUsbHostEndpointDataPid *pid)
{
    if (!state || !pipe || !pid) {
        return DM_USB_HOST_ENDPOINT_STATE_INVALID;
    }
    if (state->halted) {
        return DM_USB_HOST_ENDPOINT_STATE_HALTED;
    }
    *pipe = &state->pipe;
    *pid = state->next_pid;
    return DM_USB_HOST_ENDPOINT_STATE_OK;
}

DmUsbHostEndpointStateResult dm_usb_host_endpoint_state_complete(
    DmUsbHostEndpointState *state, DmUsbHostEndpointCompletion completion,
    size_t requested_length, size_t actual_length)
{
    if (!state || !dm_usb_host_endpoint_completion_validate(
                      completion, requested_length, actual_length)) {
        return DM_USB_HOST_ENDPOINT_STATE_INVALID;
    }
    if (state->halted) {
        return DM_USB_HOST_ENDPOINT_STATE_HALTED;
    }
    if (completion == DM_USB_HOST_ENDPOINT_STALL) {
        state->halted = true;
    } else if (completion == DM_USB_HOST_ENDPOINT_ACCEPTED &&
               dm_usb_host_endpoint_state_uses_toggle(state)) {
        state->next_pid = state->next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 ?
                          DM_USB_HOST_ENDPOINT_PID_DATA1 :
                          DM_USB_HOST_ENDPOINT_PID_DATA0;
    }
    return DM_USB_HOST_ENDPOINT_STATE_OK;
}
