/* Board-independent USB device transaction dispatcher. */
#include "hw/usb/dm_usb_transaction.h"

#include <string.h>

static DmUsbTransactionResult transaction_result(
    DmUsbTransactionStatus status, size_t actual_length,
    DmUsbTransactionPid next_pid)
{
    return (DmUsbTransactionResult) {
        .status = status,
        .actual_length = actual_length,
        .next_pid = next_pid,
    };
}

static DmUsbTransactionPid transaction_toggle(DmUsbTransactionPid pid)
{
    return pid == DM_USB_TRANSACTION_PID_DATA0 ?
           DM_USB_TRANSACTION_PID_DATA1 : DM_USB_TRANSACTION_PID_DATA0;
}

static bool transaction_direction_valid(DmUsbEndpointDirection direction)
{
    return direction == DM_USB_ENDPOINT_IN ||
           direction == DM_USB_ENDPOINT_OUT;
}

static DmUsbTransactionPid *transaction_next_pid(
    DmUsbTransactionEndpoint *endpoint, DmUsbEndpointDirection direction)
{
    return direction == DM_USB_ENDPOINT_IN ? &endpoint->next_in_pid :
                                               &endpoint->next_out_pid;
}

static bool *transaction_enabled(DmUsbTransactionEndpoint *endpoint,
                                 DmUsbEndpointDirection direction)
{
    return direction == DM_USB_ENDPOINT_IN ? &endpoint->in_enabled :
                                               &endpoint->out_enabled;
}

static bool *transaction_halted(DmUsbTransactionEndpoint *endpoint,
                                DmUsbEndpointDirection direction)
{
    return direction == DM_USB_ENDPOINT_IN ? &endpoint->in_halted :
                                               &endpoint->out_halted;
}

static DmUsbEndpointDirection transaction_direction(
    DmUsbTransactionToken token)
{
    return token == DM_USB_TRANSACTION_IN ? DM_USB_ENDPOINT_IN :
                                             DM_USB_ENDPOINT_OUT;
}

void dm_usb_transaction_init(DmUsbTransactionDevice *device,
                             DmUsbControlDevice *control,
                             const DmUsbTransactionOps *ops, void *opaque,
                             uint16_t ep0_max_packet_size)
{
    memset(device, 0, sizeof(*device));
    device->control = control;
    if (ops) {
        device->ops = *ops;
    }
    device->opaque = opaque;
    device->endpoint[0] = (DmUsbTransactionEndpoint) {
        .in_enabled = true,
        .out_enabled = true,
        .type = DM_USB_ENDPOINT_CONTROL,
        .max_packet_size = ep0_max_packet_size ? ep0_max_packet_size : 64,
        .next_in_pid = DM_USB_TRANSACTION_PID_DATA0,
        .next_out_pid = DM_USB_TRANSACTION_PID_DATA0,
    };
    dm_usb_transaction_reset(device);
}

void dm_usb_transaction_reset(DmUsbTransactionDevice *device)
{
    if (!device) {
        return;
    }
    for (unsigned endpoint = 0;
         endpoint < DM_USB_TRANSACTION_MAX_ENDPOINTS; ++endpoint) {
        DmUsbTransactionEndpoint *state = &device->endpoint[endpoint];

        state->in_halted = false;
        state->out_halted = false;
        state->next_in_pid = DM_USB_TRANSACTION_PID_DATA0;
        state->next_out_pid = DM_USB_TRANSACTION_PID_DATA0;
    }
}

DmUsbTransactionStatus dm_usb_transaction_configure_endpoint(
    DmUsbTransactionDevice *device, uint8_t endpoint,
    DmUsbEndpointDirection direction, DmUsbEndpointType type,
    uint16_t max_packet_size, bool enabled)
{
    DmUsbTransactionEndpoint *state;

    if (!device || endpoint >= DM_USB_TRANSACTION_MAX_ENDPOINTS ||
        !transaction_direction_valid(direction) ||
        type > DM_USB_ENDPOINT_INTERRUPT || max_packet_size == 0) {
        return DM_USB_TRANSACTION_INVALID;
    }
    state = &device->endpoint[endpoint];
    state->type = type;
    state->max_packet_size = max_packet_size;
    *transaction_enabled(state, direction) = enabled;
    *transaction_halted(state, direction) = false;
    *transaction_next_pid(state, direction) = DM_USB_TRANSACTION_PID_DATA0;
    return DM_USB_TRANSACTION_ACCEPTED;
}

DmUsbTransactionStatus dm_usb_transaction_clear_halt(
    DmUsbTransactionDevice *device, uint8_t endpoint,
    DmUsbEndpointDirection direction)
{
    if (!device || endpoint >= DM_USB_TRANSACTION_MAX_ENDPOINTS ||
        !transaction_direction_valid(direction)) {
        return DM_USB_TRANSACTION_INVALID;
    }
    *transaction_halted(&device->endpoint[endpoint], direction) = false;
    *transaction_next_pid(&device->endpoint[endpoint], direction) =
        DM_USB_TRANSACTION_PID_DATA0;
    return DM_USB_TRANSACTION_ACCEPTED;
}

bool dm_usb_transaction_halted(const DmUsbTransactionDevice *device,
                               uint8_t endpoint,
                               DmUsbEndpointDirection direction)
{
    if (!device || endpoint >= DM_USB_TRANSACTION_MAX_ENDPOINTS ||
        !transaction_direction_valid(direction)) {
        return false;
    }
    return direction == DM_USB_ENDPOINT_IN ?
           device->endpoint[endpoint].in_halted :
           device->endpoint[endpoint].out_halted;
}

DmUsbTransactionPid dm_usb_transaction_next_pid(
    const DmUsbTransactionDevice *device, uint8_t endpoint,
    DmUsbEndpointDirection direction)
{
    if (!device || endpoint >= DM_USB_TRANSACTION_MAX_ENDPOINTS ||
        !transaction_direction_valid(direction)) {
        return DM_USB_TRANSACTION_PID_AUTO;
    }
    return direction == DM_USB_ENDPOINT_IN ?
           device->endpoint[endpoint].next_in_pid :
           device->endpoint[endpoint].next_out_pid;
}

DmUsbTransactionResult dm_usb_transaction_submit(
    DmUsbTransactionDevice *device, const DmUsbTransaction *transaction)
{
    DmUsbTransactionEndpoint *endpoint;
    DmUsbEndpointDirection direction;
    DmUsbTransactionPid *next_pid;
    bool *enabled;
    bool *halted;
    DmUsbTransactionPid expected_pid;
    DmUsbTransactionStatus status;
    DmUsbControlResult control_status;
    size_t actual_length = 0;

    if (!device || !transaction ||
        transaction->endpoint >= DM_USB_TRANSACTION_MAX_ENDPOINTS ||
        transaction->token < DM_USB_TRANSACTION_SETUP ||
        transaction->token > DM_USB_TRANSACTION_OUT ||
        transaction->pid < DM_USB_TRANSACTION_PID_AUTO ||
        transaction->pid > DM_USB_TRANSACTION_PID_DATA1) {
        return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                  DM_USB_TRANSACTION_PID_AUTO);
    }

    if (transaction->token == DM_USB_TRANSACTION_SETUP) {
        if (transaction->endpoint != 0 || !device->control ||
            (transaction->pid != DM_USB_TRANSACTION_PID_AUTO &&
             transaction->pid != DM_USB_TRANSACTION_PID_DATA0) ||
            transaction->length != 8 || !transaction->out_data) {
            return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                      DM_USB_TRANSACTION_PID_AUTO);
        }
        control_status = dm_usb_control_setup(device->control,
                                              transaction->out_data);
        if (control_status == DM_USB_CONTROL_ACCEPTED) {
            device->endpoint[0].in_halted = false;
            device->endpoint[0].out_halted = false;
            device->endpoint[0].next_in_pid = DM_USB_TRANSACTION_PID_DATA1;
            device->endpoint[0].next_out_pid = DM_USB_TRANSACTION_PID_DATA1;
            return transaction_result(DM_USB_TRANSACTION_ACCEPTED, 8,
                                      DM_USB_TRANSACTION_PID_DATA1);
        }
        if (control_status == DM_USB_CONTROL_STALL) {
            device->endpoint[0].in_halted = true;
            device->endpoint[0].out_halted = true;
        }
        return transaction_result(control_status == DM_USB_CONTROL_STALL ?
                                      DM_USB_TRANSACTION_STALL :
                                  DM_USB_TRANSACTION_INVALID, 0,
                                  DM_USB_TRANSACTION_PID_AUTO);
    }

    direction = transaction_direction(transaction->token);
    endpoint = &device->endpoint[transaction->endpoint];
    if (transaction->endpoint == 0) {
        if (!device->control) {
            return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                      DM_USB_TRANSACTION_PID_AUTO);
        }
        if (transaction->pid != DM_USB_TRANSACTION_PID_AUTO &&
            transaction->pid != *transaction_next_pid(endpoint, direction)) {
            return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                      *transaction_next_pid(endpoint,
                                                            direction));
        }
        if (direction == DM_USB_ENDPOINT_IN) {
            if (transaction->length ||
                (transaction->capacity && !transaction->in_data)) {
                return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                          endpoint->next_in_pid);
            }
            control_status = dm_usb_control_in(
                device->control, transaction->in_data, transaction->capacity,
                &actual_length);
        } else {
            if (transaction->capacity ||
                (transaction->length && !transaction->out_data)) {
                return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                          endpoint->next_out_pid);
            }
            control_status = dm_usb_control_out(
                device->control, transaction->out_data, transaction->length);
        }
        if (control_status != DM_USB_CONTROL_ACCEPTED) {
            if (control_status == DM_USB_CONTROL_STALL) {
                *transaction_halted(endpoint, direction) = true;
            }
            return transaction_result(control_status == DM_USB_CONTROL_STALL ?
                                      DM_USB_TRANSACTION_STALL :
                                      DM_USB_TRANSACTION_INVALID,
                                      0, *transaction_next_pid(endpoint,
                                                               direction));
        }
        next_pid = transaction_next_pid(endpoint, direction);
        *next_pid = transaction_toggle(*next_pid);
        return transaction_result(DM_USB_TRANSACTION_ACCEPTED,
                                  transaction->token == DM_USB_TRANSACTION_IN ?
                                  actual_length : transaction->length,
                                  *next_pid);
    }

    if (transaction->token == DM_USB_TRANSACTION_SETUP ||
        (direction == DM_USB_ENDPOINT_IN && transaction->out_data) ||
        (direction == DM_USB_ENDPOINT_OUT && transaction->in_data) ||
        (direction == DM_USB_ENDPOINT_IN && transaction->length) ||
        (direction == DM_USB_ENDPOINT_OUT && transaction->capacity)) {
        return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                  DM_USB_TRANSACTION_PID_AUTO);
    }
    if (endpoint->max_packet_size == 0) {
        return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                  DM_USB_TRANSACTION_PID_AUTO);
    }
    enabled = transaction_enabled(endpoint, direction);
    halted = transaction_halted(endpoint, direction);
    next_pid = transaction_next_pid(endpoint, direction);
    expected_pid = *next_pid;
    if (!*enabled) {
        return transaction_result(DM_USB_TRANSACTION_NAK, 0, expected_pid);
    }
    if (*halted) {
        return transaction_result(DM_USB_TRANSACTION_STALL, 0, expected_pid);
    }
    if (transaction->pid != DM_USB_TRANSACTION_PID_AUTO &&
        transaction->pid != expected_pid) {
        return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                  expected_pid);
    }
    if (direction == DM_USB_ENDPOINT_OUT) {
        if (transaction->length > endpoint->max_packet_size ||
            (transaction->length && !transaction->out_data)) {
            return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                      expected_pid);
        }
        if (!device->ops.out) {
            return transaction_result(DM_USB_TRANSACTION_NAK, 0,
                                      expected_pid);
        }
        status = device->ops.out(device->opaque, transaction->endpoint,
                                 transaction->out_data, transaction->length,
                                 transaction->timestamp_ns);
    } else {
        if (transaction->capacity > 0 && !transaction->in_data) {
            return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                      expected_pid);
        }
        if (!device->ops.in) {
            return transaction_result(DM_USB_TRANSACTION_NAK, 0,
                                      expected_pid);
        }
        status = device->ops.in(device->opaque, transaction->endpoint,
                                transaction->in_data, transaction->capacity,
                                &actual_length, transaction->timestamp_ns);
        if (status == DM_USB_TRANSACTION_ACCEPTED &&
            (actual_length > transaction->capacity ||
             actual_length > endpoint->max_packet_size)) {
            return transaction_result(DM_USB_TRANSACTION_INVALID, 0,
                                      expected_pid);
        }
    }
    if (status == DM_USB_TRANSACTION_STALL) {
        *halted = true;
    } else if (status == DM_USB_TRANSACTION_ACCEPTED) {
        *next_pid = transaction_toggle(*next_pid);
        if (direction == DM_USB_ENDPOINT_OUT) {
            actual_length = transaction->length;
        }
    } else if (status != DM_USB_TRANSACTION_NAK &&
               status != DM_USB_TRANSACTION_INVALID) {
        status = DM_USB_TRANSACTION_INVALID;
    }
    return transaction_result(status, actual_length, *next_pid);
}
