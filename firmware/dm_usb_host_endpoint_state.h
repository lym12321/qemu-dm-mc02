/* Board- and controller-independent USB endpoint data-toggle state. */
#ifndef DM_USB_HOST_ENDPOINT_STATE_H
#define DM_USB_HOST_ENDPOINT_STATE_H

#include "dm_usb_host_pipe.h"

#include <stdbool.h>
#include <stddef.h>

typedef enum DmUsbHostEndpointDataPid {
    DM_USB_HOST_ENDPOINT_PID_DATA0,
    DM_USB_HOST_ENDPOINT_PID_DATA1,
} DmUsbHostEndpointDataPid;

typedef enum DmUsbHostEndpointCompletion {
    DM_USB_HOST_ENDPOINT_ACCEPTED,
    DM_USB_HOST_ENDPOINT_NAK,
    DM_USB_HOST_ENDPOINT_STALL,
    DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR,
} DmUsbHostEndpointCompletion;

typedef enum DmUsbHostEndpointStateResult {
    DM_USB_HOST_ENDPOINT_STATE_OK,
    DM_USB_HOST_ENDPOINT_STATE_HALTED,
    DM_USB_HOST_ENDPOINT_STATE_INVALID,
} DmUsbHostEndpointStateResult;

typedef struct DmUsbHostEndpointState {
    DmUsbHostPipe pipe;
    DmUsbHostEndpointDataPid next_pid;
    bool halted;
} DmUsbHostEndpointState;

void dm_usb_host_endpoint_state_init(DmUsbHostEndpointState *state,
                                     const DmUsbHostPipe *pipe);
void dm_usb_host_endpoint_state_reset(DmUsbHostEndpointState *state);
void dm_usb_host_endpoint_state_clear_halt(DmUsbHostEndpointState *state);

/* Validate one controller-independent packet request against its pipe. */
bool dm_usb_host_endpoint_request_validate(
    const DmUsbHostPipe *pipe, const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, size_t *requested_length);

/* Validate completion length and the no-data contract of non-accepted results. */
bool dm_usb_host_endpoint_completion_validate(
    DmUsbHostEndpointCompletion completion, size_t requested_length,
    size_t actual_length);

/* Return the next packet's immutable pipe and DATA PID when not halted. */
DmUsbHostEndpointStateResult dm_usb_host_endpoint_state_prepare(
    const DmUsbHostEndpointState *state, const DmUsbHostPipe **pipe,
    DmUsbHostEndpointDataPid *pid);

/* Apply a single packet completion without choosing short-packet policy. */
DmUsbHostEndpointStateResult dm_usb_host_endpoint_state_complete(
    DmUsbHostEndpointState *state, DmUsbHostEndpointCompletion completion,
    size_t requested_length, size_t actual_length);

#endif /* DM_USB_HOST_ENDPOINT_STATE_H */
