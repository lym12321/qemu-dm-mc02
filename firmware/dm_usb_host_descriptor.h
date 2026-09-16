/* Board- and controller-independent USB configuration descriptor parser. */
#ifndef DM_USB_HOST_DESCRIPTOR_H
#define DM_USB_HOST_DESCRIPTOR_H

#include <stddef.h>
#include <stdint.h>

typedef enum DmUsbHostDescriptorResult {
    DM_USB_HOST_DESCRIPTOR_OK,
    DM_USB_HOST_DESCRIPTOR_INVALID,
    DM_USB_HOST_DESCRIPTOR_TRUNCATED,
    DM_USB_HOST_DESCRIPTOR_NOT_FOUND,
} DmUsbHostDescriptorResult;

typedef struct DmUsbHostConfiguration {
    uint16_t total_length;
    uint8_t interface_count;
    uint8_t value;
    uint8_t attributes;
    uint8_t max_power;
} DmUsbHostConfiguration;

typedef struct DmUsbHostInterface {
    uint8_t number;
    uint8_t alternate_setting;
    uint8_t endpoint_count;
    uint8_t interface_class;
    uint8_t interface_subclass;
    uint8_t interface_protocol;
} DmUsbHostInterface;

typedef struct DmUsbHostEndpoint {
    uint8_t interface_number;
    uint8_t alternate_setting;
    uint8_t address;
    uint8_t attributes;
    uint16_t max_packet_size;
    uint8_t interval;
} DmUsbHostEndpoint;

/* Parse the fixed nine-byte configuration descriptor header. */
DmUsbHostDescriptorResult dm_usb_host_descriptor_parse_configuration_header(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration);

/* Validate every descriptor through wTotalLength and return the configuration. */
DmUsbHostDescriptorResult dm_usb_host_descriptor_parse_configuration(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration);

/* Find one interface alternate setting in a validated configuration. */
DmUsbHostDescriptorResult dm_usb_host_descriptor_find_interface(
    const uint8_t *data, size_t length, uint8_t interface_number,
    uint8_t alternate_setting, DmUsbHostInterface *interface_descriptor);

/* Find one endpoint ordinal within an interface alternate setting. */
DmUsbHostDescriptorResult dm_usb_host_descriptor_find_endpoint(
    const uint8_t *data, size_t length, uint8_t interface_number,
    uint8_t alternate_setting, uint8_t endpoint_index,
    DmUsbHostEndpoint *endpoint);

#endif /* DM_USB_HOST_DESCRIPTOR_H */
