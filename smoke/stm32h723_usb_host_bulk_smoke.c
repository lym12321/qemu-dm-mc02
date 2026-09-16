#include <stdint.h>

#include "dm_stm32h7_usb_host_bulk.h"
#include "dm_stm32h7_usb_host_control.h"
#include "dm_usb_host_descriptor.h"
#include "dm_usb_host_endpoint_state.h"
#include "dm_usb_host_pipe.h"

#define USB_BASE 0x40040000u
#define SYST_CSR (*(volatile uint32_t *)0xe000e010u)
#define SYST_RVR (*(volatile uint32_t *)0xe000e014u)
#define SYST_CVR (*(volatile uint32_t *)0xe000e018u)
#define GAHBCFG  (*(volatile uint32_t *)(USB_BASE + 0x008u))
#define HPRT0    (*(volatile uint32_t *)(USB_BASE + 0x440u))
#define HCCHAR(channel) \
    (*(volatile uint32_t *)(USB_BASE + 0x500u + 0x20u * (channel)))
#define HCTSIZ(channel) \
    (*(volatile uint32_t *)(USB_BASE + 0x510u + 0x20u * (channel)))
#define RESULT   ((volatile uint32_t *)0x20000000u)

#define HPRT0_PWR      (1u << 12)
#define HPRT0_ENA      (1u << 2)
#define HPRT0_CONNSTS  (1u << 0)
#define RESULT_MARKER  0x48534255u /* "USBH" */
#define RESULT_DONE    0x444f4e45u /* "DONE" */
#define RESULT_IN_READY 0x52454144u /* "READ" */
#define RESULT_DONE_INDEX 27u

void Reset_Handler(void);
void SysTick_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [15] = (uint32_t)(uintptr_t)SysTick_Handler,
};

void SysTick_Handler(void)
{
}

void Reset_Handler(void)
{
    static const uint8_t get_descriptor[] = {
        0x80, 0x06, 0, 0x01, 0, 0, 18, 0,
    };
    static const uint8_t set_address[] = {
        0x00, 0x05, 5, 0, 0, 0, 0, 0,
    };
    static const uint8_t get_configuration_header[] = {
        0x80, 0x06, 0, 0x02, 0, 0, 9, 0,
    };
    DmStm32H7UsbHostControl control;
    DmStm32H7UsbHostControlResult control_result;
    static DmUsbHostChannelAllocator allocator;
    static const uint8_t channel_ids[] = { 1 };
    DmUsbHostConfiguration configuration;
    DmUsbHostInterface interface_descriptor;
    DmUsbHostEndpoint in_endpoint;
    DmUsbHostEndpoint out_endpoint;
    DmUsbHostPipe in_pipe;
    DmUsbHostPipe out_pipe;
    DmUsbHostEndpointState in_state;
    DmUsbHostEndpointState out_state;
    DmStm32H7UsbHostBulk bulk;
    DmUsbHostBulkTransferResult bulk_result;
    bool bulk_initialized;
    uint8_t descriptor[18];
    uint8_t configuration_header[9];
    uint8_t configuration_descriptor[64];
    uint8_t get_configuration[8] = { 0x80, 0x06, 0, 0x02, 0, 0, 0, 0 };
    uint8_t set_configuration[8] = { 0x00, 0x09, 0, 0, 0, 0, 0, 0 };
    uint8_t out_data[130];
    uint8_t in_data[64];
    size_t actual_length;
    size_t index;

    for (index = 0; index < sizeof(out_data); ++index) {
        out_data[index] = (uint8_t)index;
    }
    RESULT[0] = RESULT_MARKER;
    dm_stm32h7_usb_host_control_init(&control, USB_BASE, 0, 8, 0);
    RESULT[1] = dm_stm32h7_usb_host_control_port_reset(&control) ? HPRT0 : 0;

    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, get_descriptor, NULL, 0, descriptor, sizeof(descriptor),
        &actual_length);
    RESULT[2] = control_result;
    RESULT[3] = actual_length == sizeof(descriptor) ?
                descriptor[0] | ((uint32_t)descriptor[1] << 8) : 0;
    if (control_result != DM_STM32H7_USB_HOST_CONTROL_OK ||
        actual_length != sizeof(descriptor)) {
        goto done;
    }
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, set_address, NULL, 0, NULL, 0, &actual_length);
    RESULT[4] = control_result;
    if (control_result != DM_STM32H7_USB_HOST_CONTROL_OK) {
        goto done;
    }
    control.device_address = 5;
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, get_configuration_header, NULL, 0, configuration_header,
        sizeof(configuration_header), &actual_length);
    RESULT[5] = control_result;
    if (control_result != DM_STM32H7_USB_HOST_CONTROL_OK ||
        actual_length != sizeof(configuration_header) ||
        dm_usb_host_descriptor_parse_configuration_header(
            configuration_header, sizeof(configuration_header),
            &configuration) != DM_USB_HOST_DESCRIPTOR_OK ||
        configuration.total_length > sizeof(configuration_descriptor)) {
        goto done;
    }
    RESULT[6] = configuration.total_length |
                ((uint32_t)configuration.value << 16);
    get_configuration[6] = (uint8_t)configuration.total_length;
    get_configuration[7] = (uint8_t)(configuration.total_length >> 8);
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, get_configuration, NULL, 0, configuration_descriptor,
        sizeof(configuration_descriptor), &actual_length);
    RESULT[7] = control_result;
    if (control_result != DM_STM32H7_USB_HOST_CONTROL_OK ||
        actual_length != configuration.total_length ||
        dm_usb_host_descriptor_parse_configuration(
            configuration_descriptor, actual_length,
            &configuration) != DM_USB_HOST_DESCRIPTOR_OK ||
        dm_usb_host_descriptor_find_interface(
            configuration_descriptor, actual_length, 0, 0,
            &interface_descriptor) != DM_USB_HOST_DESCRIPTOR_OK ||
        dm_usb_host_descriptor_find_endpoint(
            configuration_descriptor, actual_length, 0, 0, 0,
            &in_endpoint) != DM_USB_HOST_DESCRIPTOR_OK ||
        dm_usb_host_descriptor_find_endpoint(
            configuration_descriptor, actual_length, 0, 0, 1,
            &out_endpoint) != DM_USB_HOST_DESCRIPTOR_OK ||
        dm_usb_host_pipe_from_endpoint(
            &out_pipe, control.device_address, &out_endpoint) !=
            DM_USB_HOST_PIPE_OK ||
        dm_usb_host_pipe_from_endpoint(
            &in_pipe, control.device_address, &in_endpoint) !=
            DM_USB_HOST_PIPE_OK) {
        goto done;
    }
    RESULT[8] = in_endpoint.address |
                ((uint32_t)out_endpoint.address << 8) |
                ((uint32_t)out_endpoint.attributes << 16) |
                ((uint32_t)out_pipe.max_packet_size << 24);
    RESULT[23] = in_pipe.device_address |
                 ((uint32_t)out_pipe.device_address << 8) |
                 ((uint32_t)in_pipe.endpoint_number << 16) |
                 ((uint32_t)out_pipe.endpoint_number << 24);
    RESULT[24] = (in_pipe.direction_in ? 1u : 0u) |
                 ((uint32_t)(out_pipe.direction_in ? 1u : 0u) << 1) |
                 ((uint32_t)in_pipe.transfer_type << 8) |
                 ((uint32_t)out_pipe.transfer_type << 16) |
                 ((uint32_t)in_pipe.max_packet_size << 24);
    RESULT[25] = in_pipe.transactions_per_microframe |
                 ((uint32_t)out_pipe.transactions_per_microframe << 8) |
                 ((uint32_t)interface_descriptor.number << 16) |
                 ((uint32_t)interface_descriptor.alternate_setting << 24);
    if (interface_descriptor.number != 0 ||
        in_endpoint.address != 0x81 || in_endpoint.attributes != 2 ||
        !in_pipe.direction_in || in_pipe.endpoint_number != 1 ||
        in_pipe.transfer_type != DM_USB_HOST_PIPE_BULK ||
        in_pipe.max_packet_size != 64 ||
        out_endpoint.address != 2 || out_endpoint.attributes != 2 ||
        !out_pipe.device_address || out_pipe.endpoint_number != 2 ||
        out_pipe.direction_in || out_pipe.transfer_type !=
        DM_USB_HOST_PIPE_BULK || out_pipe.max_packet_size != 64 ||
        out_pipe.transactions_per_microframe != 1) {
        goto done;
    }
    set_configuration[2] = configuration.value;
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, set_configuration, NULL, 0, NULL, 0, &actual_length);
    RESULT[9] = control_result;
    if (control_result != DM_STM32H7_USB_HOST_CONTROL_OK ||
        !dm_usb_host_channel_allocator_init(
            &allocator, channel_ids, sizeof(channel_ids))) {
        RESULT[24] = 1;
        goto done;
    }
    RESULT[24] = 2;

    dm_usb_host_endpoint_state_init(&out_state, &out_pipe);
    bulk_initialized = dm_stm32h7_usb_host_bulk_init(
        &bulk, USB_BASE, &allocator, &out_state);
    RESULT[25] = bulk_initialized ? 1 : 0;
    if (!bulk_initialized) {
        goto done;
    }
    bulk_result = dm_stm32h7_usb_host_bulk_run(
        &bulk, out_data, sizeof(out_data), NULL, 0, false, &actual_length);
    RESULT[10] = bulk_result;
    RESULT[11] = actual_length;
    RESULT[12] = out_state.next_pid;
    RESULT[13] = HCCHAR(1);
    RESULT[14] = HCTSIZ(1);
    RESULT[15] = allocator.entry[0].owner ? 1 : 0;
    /* Keep the READY marker observable while the host injects chardev input. */
    SYST_RVR = 6400000u;
    SYST_CVR = 0;
    SYST_CSR = 7u;
    RESULT[26] = RESULT_IN_READY;
    __asm__ volatile ("wfi" ::: "memory");
    dm_usb_host_endpoint_state_init(&in_state, &in_pipe);
    if (!dm_stm32h7_usb_host_bulk_init(
            &bulk, USB_BASE, &allocator, &in_state)) {
        goto done;
    }
    bulk_result = dm_stm32h7_usb_host_bulk_run(
        &bulk, NULL, 0, in_data, sizeof(in_data), false, &actual_length);
    RESULT[16] = bulk_result;
    RESULT[17] = actual_length;
    RESULT[18] = in_state.next_pid;
    RESULT[19] = HCCHAR(1);
    RESULT[20] = HCTSIZ(1);
    RESULT[21] = allocator.entry[0].owner ? 1 : 0;
    RESULT[22] = actual_length >= 4 ?
                 in_data[0] | ((uint32_t)in_data[1] << 8) |
                 ((uint32_t)in_data[2] << 16) |
                 ((uint32_t)in_data[3] << 24) : 0xffffffffu;
    /* Once the serial input is consumed, bulk IN returns NAK while the H723
     * channel remains enabled in the controller. The synchronous PIO client
     * must return that result and stop the channel before releasing its lease.
     */
    bulk_result = dm_stm32h7_usb_host_bulk_run(
        &bulk, NULL, 0, in_data, sizeof(in_data), false, &actual_length);
    RESULT[23] = bulk_result;
    RESULT[24] = HCCHAR(1) & (1u << 31) ? 1u : 0u;
    if (bulk_result != DM_USB_HOST_BULK_NAK || actual_length != 0) {
        goto done;
    }
    RESULT[26] = 1;

done:
    RESULT[RESULT_DONE_INDEX] = RESULT_DONE;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
