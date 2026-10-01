/* Board-independent USB device control-transfer core. */
#include "hw/usb/dm_usb_control.h"

#include <string.h>

static size_t min_size(size_t left, size_t right)
{
    return left < right ? left : right;
}

static DmUsbControlResult dm_usb_control_stall(DmUsbControlDevice *device)
{
    if (device) {
        device->phase = DM_USB_CONTROL_STALLED;
    }
    return DM_USB_CONTROL_STALL;
}

static void dm_usb_control_clear_transfer(DmUsbControlDevice *device)
{
    device->data_length = 0;
    device->data_offset = 0;
    device->zero_length_packet = false;
    device->address_pending = false;
    device->configuration_pending = false;
    device->phase = DM_USB_CONTROL_IDLE;
}

static void dm_usb_control_commit_status_in(DmUsbControlDevice *device)
{
    if (device->address_pending) {
        device->address = device->pending_address;
        device->address_pending = false;
        if (device->ops.set_address) {
            device->ops.set_address(device->opaque, device->address);
        }
    }
    if (device->configuration_pending) {
        device->configuration = device->pending_configuration;
        device->configuration_pending = false;
        if (device->ops.set_configuration) {
            device->ops.set_configuration(device->opaque,
                                          device->configuration);
        }
    }
}

static DmUsbControlResult dm_usb_control_prepare_in(
    DmUsbControlDevice *device, const uint8_t *data, size_t length,
    uint16_t requested_length)
{
    if (length > sizeof(device->data)) {
        return dm_usb_control_stall(device);
    }
    if (length && data != device->data) {
        memcpy(device->data, data, length);
    }
    device->data_length = length;
    device->data_offset = 0;
    device->zero_length_packet = length < requested_length &&
                                 (length % device->max_packet_size) == 0;
    device->phase = (length || device->zero_length_packet) ?
                    DM_USB_CONTROL_DATA_IN : DM_USB_CONTROL_STATUS_OUT;
    return DM_USB_CONTROL_ACCEPTED;
}

static DmUsbControlResult dm_usb_control_standard_setup(
    DmUsbControlDevice *device)
{
    const DmUsbControlRequest *request = &device->request;
    const uint8_t *descriptor;
    size_t descriptor_length;

    if ((request->request_type & 0x60) != 0) {
        return DM_USB_CONTROL_INVALID;
    }
    switch (request->request) {
    case 0: /* GET_STATUS */
        if (!(request->request_type & 0x80) || request->length < 2) {
            return dm_usb_control_stall(device);
        }
        device->data[0] = 0;
        device->data[1] = 0;
        return dm_usb_control_prepare_in(device, device->data, 2,
                                         request->length);
    case 5: /* SET_ADDRESS */
        if ((request->request_type & 0x80) || request->length != 0 ||
            request->value > 0x7f) {
            return dm_usb_control_stall(device);
        }
        device->pending_address = request->value;
        device->address_pending = true;
        device->phase = DM_USB_CONTROL_STATUS_IN;
        return DM_USB_CONTROL_ACCEPTED;
    case 6: /* GET_DESCRIPTOR */
        if (!(request->request_type & 0x80) || !device->ops.get_descriptor ||
            !device->ops.get_descriptor(device->opaque, request->value >> 8,
                                        request->value & 0xff, &descriptor,
                                        &descriptor_length)) {
            return dm_usb_control_stall(device);
        }
        descriptor_length = min_size(descriptor_length,
                                     (size_t)request->length);
        return dm_usb_control_prepare_in(device, descriptor,
                                         descriptor_length, request->length);
    case 8: /* GET_CONFIGURATION */
        if (!(request->request_type & 0x80) || request->length < 1) {
            return dm_usb_control_stall(device);
        }
        device->data[0] = device->configuration;
        return dm_usb_control_prepare_in(device, device->data, 1,
                                         request->length);
    case 9: /* SET_CONFIGURATION */
        if ((request->request_type & 0x80) || request->length != 0 ||
            request->value > 0xff) {
            return dm_usb_control_stall(device);
        }
        device->pending_configuration = request->value;
        device->configuration_pending = true;
        device->phase = DM_USB_CONTROL_STATUS_IN;
        return DM_USB_CONTROL_ACCEPTED;
    default:
        return DM_USB_CONTROL_INVALID;
    }
}

static DmUsbControlResult dm_usb_control_class_setup(
    DmUsbControlDevice *device)
{
    DmUsbControlResult result;
    size_t in_length = 0;

    if (!device->ops.class_request) {
        return dm_usb_control_stall(device);
    }
    if (device->request.request_type & 0x80) {
        result = device->ops.class_request(
            device->opaque, &device->request, NULL, 0, device->data,
            min_size((size_t)device->request.length, sizeof(device->data)),
            &in_length);
        if (result != DM_USB_CONTROL_ACCEPTED ||
            in_length > device->request.length ||
            in_length > sizeof(device->data)) {
            return dm_usb_control_stall(device);
        }
        return dm_usb_control_prepare_in(device, device->data, in_length,
                                         device->request.length);
    }

    result = device->ops.class_request(device->opaque, &device->request,
                                       NULL, 0, NULL, 0, NULL);
    if (result != DM_USB_CONTROL_ACCEPTED) {
        return dm_usb_control_stall(device);
    }
    if (device->request.length > sizeof(device->data)) {
        return dm_usb_control_stall(device);
    }
    device->data_offset = 0;
    device->phase = device->request.length ? DM_USB_CONTROL_DATA_OUT :
                                               DM_USB_CONTROL_STATUS_IN;
    return DM_USB_CONTROL_ACCEPTED;
}

void dm_usb_control_init(DmUsbControlDevice *device,
                         const DmUsbControlOps *ops, void *opaque,
                         uint16_t max_packet_size)
{
    memset(device, 0, sizeof(*device));
    if (ops) {
        device->ops = *ops;
    }
    device->opaque = opaque;
    device->max_packet_size = max_packet_size ? max_packet_size : 64;
    dm_usb_control_reset(device);
}

void dm_usb_control_reset(DmUsbControlDevice *device)
{
    DmUsbControlOps ops;
    void *opaque;
    uint16_t max_packet_size;

    if (!device) {
        return;
    }
    ops = device->ops;
    opaque = device->opaque;
    max_packet_size = device->max_packet_size;
    memset(device, 0, sizeof(*device));
    device->ops = ops;
    device->opaque = opaque;
    device->max_packet_size = max_packet_size ? max_packet_size : 64;
}

void dm_usb_control_bus_reset(DmUsbControlDevice *device)
{
    if (!device) {
        return;
    }
    dm_usb_control_reset(device);
    if (device->ops.set_address) {
        device->ops.set_address(device->opaque, 0);
    }
    if (device->ops.set_configuration) {
        device->ops.set_configuration(device->opaque, 0);
    }
}

DmUsbControlResult dm_usb_control_setup(DmUsbControlDevice *device,
                                        const uint8_t setup[8])
{
    if (!device || !setup || !device->max_packet_size) {
        return DM_USB_CONTROL_INVALID;
    }
    dm_usb_control_clear_transfer(device);
    memcpy(device->setup, setup, sizeof(device->setup));
    device->request = (DmUsbControlRequest) {
        .request_type = setup[0],
        .request = setup[1],
        .value = setup[2] | ((uint16_t)setup[3] << 8),
        .index = setup[4] | ((uint16_t)setup[5] << 8),
        .length = setup[6] | ((uint16_t)setup[7] << 8),
    };
    switch (device->request.request_type & 0x60) {
    case 0x00:
        if (dm_usb_control_standard_setup(device) !=
            DM_USB_CONTROL_INVALID) {
            return device->phase == DM_USB_CONTROL_STALLED ?
                   DM_USB_CONTROL_STALL : DM_USB_CONTROL_ACCEPTED;
        }
        break;
    case 0x20:
        return dm_usb_control_class_setup(device);
    default:
        break;
    }
    return dm_usb_control_stall(device);
}

DmUsbControlResult dm_usb_control_in(DmUsbControlDevice *device,
                                     uint8_t *data, size_t capacity,
                                     size_t *length)
{
    size_t packet_length;

    if (length) {
        *length = 0;
    }
    if (!device || !length || (capacity && !data)) {
        return DM_USB_CONTROL_INVALID;
    }
    switch (device->phase) {
    case DM_USB_CONTROL_DATA_IN:
        if (device->data_offset < device->data_length) {
            packet_length = min_size((size_t)device->max_packet_size,
                                     device->data_length - device->data_offset);
            if (packet_length > capacity) {
                return DM_USB_CONTROL_INVALID;
            }
            memcpy(data, device->data + device->data_offset, packet_length);
            device->data_offset += packet_length;
            *length = packet_length;
        } else if (device->zero_length_packet) {
            device->zero_length_packet = false;
        } else {
            return DM_USB_CONTROL_INVALID;
        }
        if (device->data_offset == device->data_length &&
            !device->zero_length_packet) {
            device->phase = DM_USB_CONTROL_STATUS_OUT;
        }
        return DM_USB_CONTROL_ACCEPTED;
    case DM_USB_CONTROL_STATUS_IN:
        dm_usb_control_commit_status_in(device);
        device->phase = DM_USB_CONTROL_IDLE;
        return DM_USB_CONTROL_ACCEPTED;
    case DM_USB_CONTROL_STALLED:
        return DM_USB_CONTROL_STALL;
    default:
        return DM_USB_CONTROL_INVALID;
    }
}

DmUsbControlResult dm_usb_control_out(DmUsbControlDevice *device,
                                      const uint8_t *data, size_t length)
{
    if (!device || (length && !data)) {
        return DM_USB_CONTROL_INVALID;
    }
    switch (device->phase) {
    case DM_USB_CONTROL_DATA_OUT:
        if (length > device->request.length - device->data_offset ||
            device->data_offset + length > sizeof(device->data)) {
            return dm_usb_control_stall(device);
        }
        if (length) {
            memcpy(device->data + device->data_offset, data, length);
        }
        device->data_offset += length;
        if (device->data_offset != device->request.length) {
            return DM_USB_CONTROL_ACCEPTED;
        }
        if (!device->ops.class_request ||
            device->ops.class_request(device->opaque, &device->request,
                                      device->data, device->data_length =
                                      device->data_offset, NULL, 0, NULL) !=
                DM_USB_CONTROL_ACCEPTED) {
            return dm_usb_control_stall(device);
        }
        device->phase = DM_USB_CONTROL_STATUS_IN;
        return DM_USB_CONTROL_ACCEPTED;
    case DM_USB_CONTROL_STATUS_OUT:
        if (length) {
            return dm_usb_control_stall(device);
        }
        device->phase = DM_USB_CONTROL_IDLE;
        return DM_USB_CONTROL_ACCEPTED;
    case DM_USB_CONTROL_STALLED:
        return DM_USB_CONTROL_STALL;
    default:
        return DM_USB_CONTROL_INVALID;
    }
}

DmUsbControlResult dm_usb_control_execute_request(
    DmUsbControlDevice *device, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length)
{
    uint8_t setup[8];
    size_t total = 0;
    bool in_request;
    DmUsbControlResult result;

    if (actual_length) {
        *actual_length = 0;
    }
    if (!device || !request || !actual_length || !device->max_packet_size ||
        request->length > DM_USB_CONTROL_MAX_DATA) {
        return DM_USB_CONTROL_INVALID;
    }

    in_request = request->request_type & 0x80;
    if (in_request) {
        if (out_data || out_length ||
            (in_capacity && !in_data) || in_capacity < request->length) {
            return DM_USB_CONTROL_INVALID;
        }
    } else if (in_data || in_capacity ||
               (out_length && !out_data) ||
               out_length != request->length) {
        return DM_USB_CONTROL_INVALID;
    }

    setup[0] = request->request_type;
    setup[1] = request->request;
    setup[2] = request->value;
    setup[3] = request->value >> 8;
    setup[4] = request->index;
    setup[5] = request->index >> 8;
    setup[6] = request->length;
    setup[7] = request->length >> 8;

    result = dm_usb_control_setup(device, setup);
    if (result != DM_USB_CONTROL_ACCEPTED) {
        return result;
    }

    if (in_request) {
        while (dm_usb_control_phase(device) == DM_USB_CONTROL_DATA_IN) {
            size_t packet_length = 0;
            size_t remaining = in_capacity - total;

            result = dm_usb_control_in(device,
                                       in_data ? in_data + total : NULL,
                                       remaining, &packet_length);
            if (result != DM_USB_CONTROL_ACCEPTED ||
                packet_length > remaining) {
                return result == DM_USB_CONTROL_ACCEPTED ?
                       DM_USB_CONTROL_INVALID : result;
            }
            total += packet_length;
            if (!packet_length &&
                dm_usb_control_phase(device) == DM_USB_CONTROL_DATA_IN) {
                return DM_USB_CONTROL_INVALID;
            }
        }
        if (dm_usb_control_phase(device) != DM_USB_CONTROL_STATUS_OUT) {
            return DM_USB_CONTROL_INVALID;
        }
        result = dm_usb_control_out(device, NULL, 0);
    } else {
        if (dm_usb_control_phase(device) == DM_USB_CONTROL_DATA_OUT) {
            result = dm_usb_control_out(device, out_data, out_length);
        }
        if (result == DM_USB_CONTROL_ACCEPTED &&
            dm_usb_control_phase(device) != DM_USB_CONTROL_STATUS_IN) {
            return DM_USB_CONTROL_INVALID;
        }
        if (result == DM_USB_CONTROL_ACCEPTED) {
            size_t status_length = 0;

            result = dm_usb_control_in(device, NULL, 0, &status_length);
            if (result == DM_USB_CONTROL_ACCEPTED && status_length != 0) {
                return DM_USB_CONTROL_INVALID;
            }
        }
    }
    if (result != DM_USB_CONTROL_ACCEPTED ||
        dm_usb_control_phase(device) != DM_USB_CONTROL_IDLE) {
        return result == DM_USB_CONTROL_ACCEPTED ?
               DM_USB_CONTROL_INVALID : result;
    }

    *actual_length = total;
    return DM_USB_CONTROL_ACCEPTED;
}

DmUsbControlPhase dm_usb_control_phase(const DmUsbControlDevice *device)
{
    return device ? device->phase : DM_USB_CONTROL_STALLED;
}

uint8_t dm_usb_control_address(const DmUsbControlDevice *device)
{
    return device ? device->address : 0;
}

uint8_t dm_usb_control_configuration(const DmUsbControlDevice *device)
{
    return device ? device->configuration : 0;
}
