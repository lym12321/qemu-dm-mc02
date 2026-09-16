#include "dm_usb_host_descriptor.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

int main(void)
{
    static const uint8_t configuration[] = {
        9, 2, 65, 0, 2, 1, 0, 0xa0, 50,
        8, 11, 0, 2, 3, 0, 0, 0,
        9, 4, 0, 0, 1, 3, 1, 1, 0,
        9, 0x21, 0x11, 1, 0, 1, 0x22, 0x3f, 0,
        7, 5, 0x81, 3, 8, 0, 10,
        9, 4, 1, 0, 2, 0xff, 0, 0, 0,
        7, 5, 2, 2, 64, 0, 0,
        7, 5, 0x83, 2, 64, 0, 0,
    };
    uint8_t malformed[sizeof(configuration)];
    DmUsbHostConfiguration parsed;
    DmUsbHostInterface interface_descriptor;
    DmUsbHostEndpoint endpoint;

    if (expect(dm_usb_host_descriptor_parse_configuration_header(
                   configuration, 9, &parsed) == DM_USB_HOST_DESCRIPTOR_OK,
               "parse configuration header") ||
        expect(parsed.total_length == sizeof(configuration) &&
                   parsed.interface_count == 2 && parsed.value == 1 &&
                   parsed.max_power == 50,
               "configuration header fields") ||
        expect(dm_usb_host_descriptor_parse_configuration(
                   configuration, sizeof(configuration), &parsed) ==
                   DM_USB_HOST_DESCRIPTOR_OK,
               "validate configuration") ||
        expect(dm_usb_host_descriptor_find_interface(
                   configuration, sizeof(configuration), 0, 0,
                   &interface_descriptor) == DM_USB_HOST_DESCRIPTOR_OK &&
                   interface_descriptor.endpoint_count == 1 &&
                   interface_descriptor.interface_class == 3 &&
                   interface_descriptor.interface_protocol == 1,
               "find HID interface") ||
        expect(dm_usb_host_descriptor_find_endpoint(
                   configuration, sizeof(configuration), 0, 0, 0,
                   &endpoint) == DM_USB_HOST_DESCRIPTOR_OK &&
                   endpoint.address == 0x81 && endpoint.attributes == 3 &&
                   endpoint.max_packet_size == 8 && endpoint.interval == 10,
               "find HID endpoint") ||
        expect(dm_usb_host_descriptor_find_endpoint(
                   configuration, sizeof(configuration), 1, 0, 1,
                   &endpoint) == DM_USB_HOST_DESCRIPTOR_OK &&
                   endpoint.address == 0x83 && endpoint.max_packet_size == 64,
               "find second bulk endpoint") ||
        expect(dm_usb_host_descriptor_find_endpoint(
                   configuration, sizeof(configuration), 0, 0, 1,
                   &endpoint) == DM_USB_HOST_DESCRIPTOR_NOT_FOUND,
               "reject absent endpoint") ||
        expect(dm_usb_host_descriptor_find_interface(
                   configuration, sizeof(configuration), 2, 0,
                   &interface_descriptor) == DM_USB_HOST_DESCRIPTOR_NOT_FOUND,
               "reject absent interface")) {
        return 1;
    }

    for (size_t index = 0; index < sizeof(configuration); ++index) {
        malformed[index] = configuration[index];
    }
    malformed[2] = 66;
    if (expect(dm_usb_host_descriptor_parse_configuration(
                   malformed, sizeof(malformed), &parsed) ==
                   DM_USB_HOST_DESCRIPTOR_TRUNCATED,
               "reject truncated total length")) {
        return 1;
    }
    malformed[2] = 65;
    malformed[9] = 0;
    if (expect(dm_usb_host_descriptor_parse_configuration(
                   malformed, sizeof(malformed), &parsed) ==
                   DM_USB_HOST_DESCRIPTOR_INVALID,
               "reject zero-length descriptor")) {
        return 1;
    }

    puts("RESULT: USB host configuration descriptor smoke passed");
    return 0;
}
