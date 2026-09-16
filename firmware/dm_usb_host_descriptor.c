/* Board- and controller-independent USB configuration descriptor parser. */
#include "dm_usb_host_descriptor.h"

#include <stdbool.h>

#define USB_DT_CONFIGURATION 2u
#define USB_DT_INTERFACE     4u
#define USB_DT_ENDPOINT      5u
#define USB_CONFIGURATION_DESCRIPTOR_LENGTH 9u
#define USB_INTERFACE_DESCRIPTOR_LENGTH     9u
#define USB_ENDPOINT_DESCRIPTOR_LENGTH      7u

static uint16_t dm_usb_host_descriptor_get_le16(const uint8_t *data)
{
    return (uint16_t)data[0] | ((uint16_t)data[1] << 8);
}

static DmUsbHostDescriptorResult dm_usb_host_descriptor_header(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration)
{
    uint16_t total_length;

    if (!data || !configuration) {
        return DM_USB_HOST_DESCRIPTOR_INVALID;
    }
    if (length < USB_CONFIGURATION_DESCRIPTOR_LENGTH) {
        return DM_USB_HOST_DESCRIPTOR_TRUNCATED;
    }
    if (data[0] < USB_CONFIGURATION_DESCRIPTOR_LENGTH ||
        data[1] != USB_DT_CONFIGURATION) {
        return DM_USB_HOST_DESCRIPTOR_INVALID;
    }
    total_length = dm_usb_host_descriptor_get_le16(data + 2);
    if (total_length < data[0]) {
        return DM_USB_HOST_DESCRIPTOR_INVALID;
    }
    *configuration = (DmUsbHostConfiguration) {
        .total_length = total_length,
        .interface_count = data[4],
        .value = data[5],
        .attributes = data[7],
        .max_power = data[8],
    };
    return DM_USB_HOST_DESCRIPTOR_OK;
}

static DmUsbHostDescriptorResult dm_usb_host_descriptor_validate(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration)
{
    DmUsbHostDescriptorResult result;
    size_t offset;

    result = dm_usb_host_descriptor_header(data, length, configuration);
    if (result != DM_USB_HOST_DESCRIPTOR_OK) {
        return result;
    }
    if (length < configuration->total_length) {
        return DM_USB_HOST_DESCRIPTOR_TRUNCATED;
    }
    offset = data[0];
    while (offset < configuration->total_length) {
        uint8_t descriptor_length;
        uint8_t descriptor_type;

        if (configuration->total_length - offset < 2) {
            return DM_USB_HOST_DESCRIPTOR_TRUNCATED;
        }
        descriptor_length = data[offset];
        descriptor_type = data[offset + 1];
        if (descriptor_length < 2 ||
            descriptor_length > configuration->total_length - offset) {
            return DM_USB_HOST_DESCRIPTOR_INVALID;
        }
        if ((descriptor_type == USB_DT_INTERFACE &&
             descriptor_length < USB_INTERFACE_DESCRIPTOR_LENGTH) ||
            (descriptor_type == USB_DT_ENDPOINT &&
             descriptor_length < USB_ENDPOINT_DESCRIPTOR_LENGTH)) {
            return DM_USB_HOST_DESCRIPTOR_INVALID;
        }
        offset += descriptor_length;
    }
    return DM_USB_HOST_DESCRIPTOR_OK;
}

DmUsbHostDescriptorResult dm_usb_host_descriptor_parse_configuration_header(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration)
{
    return dm_usb_host_descriptor_header(data, length, configuration);
}

DmUsbHostDescriptorResult dm_usb_host_descriptor_parse_configuration(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration)
{
    return dm_usb_host_descriptor_validate(data, length, configuration);
}

DmUsbHostDescriptorResult dm_usb_host_descriptor_find_interface(
    const uint8_t *data, size_t length, uint8_t interface_number,
    uint8_t alternate_setting, DmUsbHostInterface *interface_descriptor)
{
    DmUsbHostConfiguration configuration;
    DmUsbHostDescriptorResult result;
    size_t offset;

    if (!interface_descriptor) {
        return DM_USB_HOST_DESCRIPTOR_INVALID;
    }
    result = dm_usb_host_descriptor_validate(data, length, &configuration);
    if (result != DM_USB_HOST_DESCRIPTOR_OK) {
        return result;
    }
    offset = data[0];
    while (offset < configuration.total_length) {
        if (data[offset + 1] == USB_DT_INTERFACE &&
            data[offset + 2] == interface_number &&
            data[offset + 3] == alternate_setting) {
            *interface_descriptor = (DmUsbHostInterface) {
                .number = data[offset + 2],
                .alternate_setting = data[offset + 3],
                .endpoint_count = data[offset + 4],
                .interface_class = data[offset + 5],
                .interface_subclass = data[offset + 6],
                .interface_protocol = data[offset + 7],
            };
            return DM_USB_HOST_DESCRIPTOR_OK;
        }
        offset += data[offset];
    }
    return DM_USB_HOST_DESCRIPTOR_NOT_FOUND;
}

DmUsbHostDescriptorResult dm_usb_host_descriptor_find_endpoint(
    const uint8_t *data, size_t length, uint8_t interface_number,
    uint8_t alternate_setting, uint8_t endpoint_index,
    DmUsbHostEndpoint *endpoint)
{
    DmUsbHostConfiguration configuration;
    DmUsbHostDescriptorResult result;
    bool selected_interface = false;
    uint8_t found = 0;
    size_t offset;

    if (!endpoint) {
        return DM_USB_HOST_DESCRIPTOR_INVALID;
    }
    result = dm_usb_host_descriptor_validate(data, length, &configuration);
    if (result != DM_USB_HOST_DESCRIPTOR_OK) {
        return result;
    }
    offset = data[0];
    while (offset < configuration.total_length) {
        uint8_t descriptor_type = data[offset + 1];

        if (descriptor_type == USB_DT_INTERFACE) {
            selected_interface = data[offset + 2] == interface_number &&
                                 data[offset + 3] == alternate_setting;
            found = 0;
        } else if (selected_interface && descriptor_type == USB_DT_ENDPOINT) {
            if (found == endpoint_index) {
                *endpoint = (DmUsbHostEndpoint) {
                    .interface_number = interface_number,
                    .alternate_setting = alternate_setting,
                    .address = data[offset + 2],
                    .attributes = data[offset + 3],
                    .max_packet_size = dm_usb_host_descriptor_get_le16(
                        data + offset + 4),
                    .interval = data[offset + 6],
                };
                return DM_USB_HOST_DESCRIPTOR_OK;
            }
            ++found;
        }
        offset += data[offset];
    }
    return DM_USB_HOST_DESCRIPTOR_NOT_FOUND;
}
