/* Component-only VMState contract for the board-independent USB control core. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_control.h"
#include "hw/usb/dm_usb_vmstate.h"
#include "migration/qemu-file.h"
#include "migration/vmstate.h"

static int get_dm_usb_control_phase(QEMUFile *f, void *pv, size_t size,
                                    const VMStateField *field)
{
    uint32_t value;

    (void)size;
    (void)field;
    value = qemu_get_be32(f);
    if (value > DM_USB_CONTROL_STALLED) {
        return -EINVAL;
    }
    *(DmUsbControlPhase *)pv = (DmUsbControlPhase)value;
    return 0;
}

static int put_dm_usb_control_phase(QEMUFile *f, void *pv, size_t size,
                                    const VMStateField *field,
                                    JSONWriter *vmdesc)
{
    (void)size;
    (void)field;
    (void)vmdesc;
    qemu_put_be32(f, *(const DmUsbControlPhase *)pv);
    return 0;
}

static const VMStateInfo vmstate_info_dm_usb_control_phase = {
    .name = "dm-usb-control-phase",
    .get = get_dm_usb_control_phase,
    .put = put_dm_usb_control_phase,
};

#define VMSTATE_DM_USB_CONTROL_PHASE(_field, _state) \
    VMSTATE_SINGLE(_field, _state, 0, vmstate_info_dm_usb_control_phase, \
                   DmUsbControlPhase)

#define VMSTATE_DM_USB_SIZE_T(_field, _state) \
    VMSTATE_SINGLE(_field, _state, 0, dm_usb_vmstate_info_size_t, size_t)

static const VMStateDescription vmstate_dm_usb_control_request = {
    .name = "dm-usb-control-request",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8(request_type, DmUsbControlRequest),
        VMSTATE_UINT8(request, DmUsbControlRequest),
        VMSTATE_UINT16(value, DmUsbControlRequest),
        VMSTATE_UINT16(index, DmUsbControlRequest),
        VMSTATE_UINT16(length, DmUsbControlRequest),
        VMSTATE_END_OF_LIST()
    },
};

bool dm_usb_control_state_valid(const DmUsbControlDevice *state)
{
    const DmUsbControlRequest *request;

    if (!state) {
        return false;
    }
    request = &state->request;
    uint8_t request_type = request->request_type & 0x60;
    bool in_request = request->request_type & 0x80;

    if (!state->max_packet_size ||
        state->data_length > DM_USB_CONTROL_MAX_DATA ||
        state->data_offset > DM_USB_CONTROL_MAX_DATA ||
        (state->phase != DM_USB_CONTROL_DATA_OUT &&
         state->phase != DM_USB_CONTROL_STALLED &&
         state->data_offset > state->data_length) ||
        (state->phase == DM_USB_CONTROL_DATA_OUT &&
         (in_request || !request->length ||
          request->length > DM_USB_CONTROL_MAX_DATA ||
          state->data_offset >= request->length || state->data_length)) ||
        (state->phase == DM_USB_CONTROL_DATA_IN &&
         (!in_request || (request_type != 0 && request_type != 0x20) ||
          state->data_length > request->length ||
          (!state->data_length && !state->zero_length_packet))) ||
        (state->zero_length_packet &&
         (state->phase != DM_USB_CONTROL_DATA_IN ||
          state->data_length >= request->length ||
          state->data_length % state->max_packet_size != 0)) ||
        (state->address_pending && state->configuration_pending) ||
        ((state->address_pending || state->configuration_pending) &&
         state->phase != DM_USB_CONTROL_STATUS_IN) ||
        (state->phase == DM_USB_CONTROL_STATUS_OUT &&
         (!in_request || state->data_offset != state->data_length ||
          state->zero_length_packet || state->address_pending ||
          state->configuration_pending)) ||
        (state->phase == DM_USB_CONTROL_STATUS_IN &&
         (in_request || state->data_offset != state->data_length))) {
        return false;
    }

    if (state->phase == DM_USB_CONTROL_STATUS_IN) {
        if (state->address_pending) {
            if (request_type != 0 || request->request != 5 ||
                request->length || request->value > 0x7f ||
                state->pending_address != request->value ||
                state->data_length || state->data_offset) {
                return false;
            }
        } else if (state->configuration_pending) {
            if (request_type != 0 || request->request != 9 ||
                request->length ||
                state->pending_configuration != request->value ||
                state->data_length || state->data_offset) {
                return false;
            }
        } else if (request_type != 0x20 ||
                   state->data_length != request->length ||
                   state->data_offset != request->length) {
            return false;
        }
    } else if (state->phase == DM_USB_CONTROL_STALLED &&
               (state->zero_length_packet || state->address_pending ||
                state->configuration_pending)) {
        return false;
    }

    return request->request_type == state->setup[0] &&
           request->request == state->setup[1] &&
           request->value == ((uint16_t)state->setup[2] |
                              ((uint16_t)state->setup[3] << 8)) &&
           request->index == ((uint16_t)state->setup[4] |
                              ((uint16_t)state->setup[5] << 8)) &&
           request->length == ((uint16_t)state->setup[6] |
                               ((uint16_t)state->setup[7] << 8));
}

static int dm_usb_control_post_load(void *opaque, int version_id)
{
    DmUsbControlDevice *state = opaque;

    if (!state || version_id != 1 || !dm_usb_control_state_valid(state)) {
        return -EINVAL;
    }
    return 0;
}

static const VMStateField vmstate_dm_usb_control_fields[] = {
    VMSTATE_UINT16_EQUAL(max_packet_size, DmUsbControlDevice, NULL),
    VMSTATE_STRUCT(request, DmUsbControlDevice, 0,
                   vmstate_dm_usb_control_request,
                   DmUsbControlRequest),
    VMSTATE_UINT8_ARRAY(setup, DmUsbControlDevice, 8),
    VMSTATE_UINT8_ARRAY(data, DmUsbControlDevice,
                        DM_USB_CONTROL_MAX_DATA),
    VMSTATE_DM_USB_CONTROL_PHASE(phase, DmUsbControlDevice),
    VMSTATE_DM_USB_SIZE_T(data_length, DmUsbControlDevice),
    VMSTATE_DM_USB_SIZE_T(data_offset, DmUsbControlDevice),
    VMSTATE_BOOL(zero_length_packet, DmUsbControlDevice),
    VMSTATE_BOOL(address_pending, DmUsbControlDevice),
    VMSTATE_BOOL(configuration_pending, DmUsbControlDevice),
    VMSTATE_UINT8(address, DmUsbControlDevice),
    VMSTATE_UINT8(pending_address, DmUsbControlDevice),
    VMSTATE_UINT8(configuration, DmUsbControlDevice),
    VMSTATE_UINT8(pending_configuration, DmUsbControlDevice),
    VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_usb_control_raw = {
    .name = "dm-usb-control-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_usb_control_fields,
};

static const VMStateDescription vmstate_dm_usb_control = {
    .name = "dm-usb-control-device",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_usb_control_post_load,
    .fields = vmstate_dm_usb_control_fields,
};

const VMStateDescription *dm_usb_control_vmstate(void)
{
    return &vmstate_dm_usb_control;
}

const VMStateDescription *dm_usb_control_vmstate_raw(void)
{
    return &vmstate_dm_usb_control_raw;
}
