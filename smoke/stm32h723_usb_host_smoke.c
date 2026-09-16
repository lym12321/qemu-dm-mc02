#include <stdint.h>

#include "dm_stm32h7_usb_host_control.h"
#include "dm_stm32h7_usb_host_pipe.h"
#include "dm_stm32h7_usb_host_periodic.h"
#include "dm_stm32h7_usb_host_periodic_registry_sof.h"
#include "dm_stm32h7_usb_host_sof.h"
#include "dm_stm32h7_usb_host_sof_irq.h"
#include "dm_usb_host_descriptor.h"
#include "dm_usb_host_endpoint_state.h"
#include "dm_usb_host_pipe.h"
#include "dm_usb_host_periodic_registry.h"

#define USB_BASE 0x40040000u
#define GAHBCFG  (*(volatile uint32_t *)(USB_BASE + 0x008u))
#define GINTSTS  (*(volatile uint32_t *)(USB_BASE + 0x014u))
#define GINTMSK  (*(volatile uint32_t *)(USB_BASE + 0x018u))
#define HPRT0    (*(volatile uint32_t *)(USB_BASE + 0x440u))
#define HCCHAR(channel) \
    (*(volatile uint32_t *)(USB_BASE + 0x500u + 0x20u * (channel)))
#define RESULT   ((volatile uint32_t *)0x20000000u)
#define NVIC_ISER2 (*(volatile uint32_t *)0xe000e108u)
#define NVIC_ICPR2 (*(volatile uint32_t *)0xe000e288u)

#define HPRT0_PWR      (1u << 12)
#define HPRT0_ENA      (1u << 2)
#define HPRT0_CONNSTS  (1u << 0)
#define GINTSTS_SOF    (1u << 3)
#define GAHBCFG_GINT   (1u << 0)
#define USB_IRQ_BIT    (1u << 13)
#define RESULT_MARKER  0x48535455u /* "USTH" */
#define RESULT_DONE    0x444f4e45u /* "DONE" */
#define RESULT_DONE_INDEX 21u

void Reset_Handler(void);
void Usb_Sof_Handler(void);

static DmStm32H7UsbHostPeriodicRegistrySof periodic_registry_sof;
static DmStm32H7UsbHostSofIrq sof_irq;
static volatile DmUsbHostEndpointCompletion periodic_completion;
static volatile uint32_t periodic_irq_submitted;
static volatile uint32_t periodic_irq_events;

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16 + 77 + 1] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16 + 77] = (uint32_t)(uintptr_t)Usb_Sof_Handler,
};

static int control_ok(DmStm32H7UsbHostControlResult result)
{
    return result == DM_STM32H7_USB_HOST_CONTROL_OK;
}

static void record_completion(void *opaque,
                              DmUsbHostEndpointCompletion completion)
{
    *(volatile DmUsbHostEndpointCompletion *)opaque = completion;
}

void Usb_Sof_Handler(void)
{
    ++periodic_irq_events;
    periodic_irq_submitted += dm_stm32h7_usb_host_sof_irq_handle(&sof_irq);
}

void Reset_Handler(void)
{
    static const uint8_t get_descriptor[] = {
        0x80, 0x06, 0, 0x01, 0, 0, 18, 0,
    };
    static const uint8_t set_address[] = {
        0x00, 0x05, 5, 0, 0, 0, 0, 0,
    };
    static const uint8_t get_status[] = {
        0x80, 0x00, 0, 0, 0, 0, 2, 0,
    };
    static const uint8_t get_configuration_header[] = {
        0x80, 0x06, 0, 0x02, 0, 0, 9, 0,
    };
    DmStm32H7UsbHostControl control;
    DmStm32H7UsbHostControlResult transfer;
    static DmUsbHostChannelAllocator channel_allocator;
    static const uint8_t periodic_channels[] = { 1 };
    static DmStm32H7UsbHostPeriodic periodic;
    static DmStm32H7UsbHostSof sof;
    static DmUsbHostPeriodicRegistry periodic_registry;
    DmUsbHostPeriodicRegistryEntry periodic_entry;
    DmUsbHostPeriodicPollResult poll_result;
    DmUsbHostEndpointCompletion completion;
    DmUsbHostConfiguration configuration;
    DmUsbHostInterface interface_descriptor;
    DmUsbHostEndpoint endpoint;
    DmUsbHostPipe pipe;
    DmUsbHostEndpointState endpoint_state;
    uint8_t descriptor[18];
    uint8_t status[2];
    uint8_t configuration_header[9];
    uint8_t configuration_descriptor[64];
    static uint8_t interrupt_report[8];
    uint8_t get_configuration[8] = { 0x80, 0x06, 0, 0x02, 0, 0, 0, 0 };
    uint8_t set_configuration[8] = { 0x00, 0x09, 0, 0, 0, 0, 0, 0 };
    size_t actual_length;
    uint64_t timestamp;

    RESULT[0] = RESULT_MARKER;
    dm_stm32h7_usb_host_control_init(&control, USB_BASE, 0, 64, 0);
    RESULT[1] = dm_stm32h7_usb_host_control_port_reset(&control) ? HPRT0 : 0;

    transfer = dm_stm32h7_usb_host_control_transfer(
        &control, get_descriptor, NULL, 0, descriptor, sizeof(descriptor),
        &actual_length);
    RESULT[2] = transfer;
    RESULT[3] = actual_length == sizeof(descriptor) ?
                descriptor[0] | ((uint32_t)descriptor[1] << 8) : 0;
    if (!control_ok(transfer) || actual_length != sizeof(descriptor)) {
        goto done;
    }

    transfer = dm_stm32h7_usb_host_control_transfer(
        &control, set_address, NULL, 0, NULL, 0, &actual_length);
    RESULT[4] = transfer;
    if (!control_ok(transfer)) {
        goto done;
    }
    control.device_address = 5;
    transfer = dm_stm32h7_usb_host_control_transfer(
        &control, get_status, NULL, 0, status, sizeof(status), &actual_length);
    RESULT[5] = transfer;
    RESULT[6] = actual_length == sizeof(status) ?
                status[0] | ((uint32_t)status[1] << 8) : 0xffffffffu;
    if (!control_ok(transfer) || actual_length != sizeof(status)) {
        goto done;
    }

    transfer = dm_stm32h7_usb_host_control_transfer(
        &control, get_configuration_header, NULL, 0, configuration_header,
        sizeof(configuration_header), &actual_length);
    RESULT[7] = transfer;
    if (!control_ok(transfer) || actual_length != sizeof(configuration_header) ||
        dm_usb_host_descriptor_parse_configuration_header(
            configuration_header, sizeof(configuration_header),
            &configuration) != DM_USB_HOST_DESCRIPTOR_OK ||
        configuration.total_length > sizeof(configuration_descriptor)) {
        goto done;
    }
    RESULT[8] = configuration.total_length |
                ((uint32_t)configuration.value << 16);
    get_configuration[6] = (uint8_t)configuration.total_length;
    get_configuration[7] = (uint8_t)(configuration.total_length >> 8);
    transfer = dm_stm32h7_usb_host_control_transfer(
        &control, get_configuration, NULL, 0, configuration_descriptor,
        sizeof(configuration_descriptor), &actual_length);
    RESULT[9] = transfer;
    if (!control_ok(transfer) || actual_length != configuration.total_length ||
        dm_usb_host_descriptor_parse_configuration(
            configuration_descriptor, actual_length,
            &configuration) != DM_USB_HOST_DESCRIPTOR_OK ||
        dm_usb_host_descriptor_find_interface(
            configuration_descriptor, actual_length, 0, 0,
            &interface_descriptor) != DM_USB_HOST_DESCRIPTOR_OK ||
        dm_usb_host_descriptor_find_endpoint(
            configuration_descriptor, actual_length, 0, 0, 0,
            &endpoint) != DM_USB_HOST_DESCRIPTOR_OK ||
        dm_usb_host_pipe_from_endpoint(
            &pipe, control.device_address, &endpoint) != DM_USB_HOST_PIPE_OK) {
        goto done;
    }
    RESULT[10] = endpoint.address | ((uint32_t)endpoint.attributes << 8) |
                 ((uint32_t)pipe.max_packet_size << 16);
    if (interface_descriptor.interface_class != 3 ||
        pipe.device_address != 5 || pipe.endpoint_number != 1 ||
        !pipe.direction_in ||
        pipe.transfer_type != DM_USB_HOST_PIPE_INTERRUPT ||
        pipe.max_packet_size != 8 || pipe.transactions_per_microframe != 1) {
        goto done;
    }
    set_configuration[2] = configuration.value;
    transfer = dm_stm32h7_usb_host_control_transfer(
        &control, set_configuration, NULL, 0, NULL, 0, &actual_length);
    RESULT[11] = transfer;
    if (!control_ok(transfer)) {
        goto done;
    }
    if (!dm_usb_host_channel_allocator_init(
            &channel_allocator, periodic_channels,
            sizeof(periodic_channels))) {
        goto done;
    }
    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    if (dm_stm32h7_usb_host_sof_init(&sof, USB_BASE, 0) !=
        DM_USB_HOST_SOF_CLOCK_OK) {
        goto done;
    }
    timestamp = dm_stm32h7_usb_host_sof_timestamp(&sof);
    if (dm_stm32h7_usb_host_periodic_init(
            &periodic, USB_BASE, &channel_allocator, &endpoint_state,
            sof.clock.speed,
            timestamp) !=
        DM_USB_HOST_PERIODIC_SCHEDULE_OK) {
        goto done;
    }
    poll_result = dm_stm32h7_usb_host_periodic_poll(
        &periodic, timestamp, NULL, 0, interrupt_report,
        sizeof(interrupt_report),
        &completion);
    RESULT[12] = completion;
    RESULT[13] = endpoint_state.next_pid;
    RESULT[14] = poll_result;
    poll_result = dm_stm32h7_usb_host_periodic_poll(
        &periodic, timestamp, NULL, 0, interrupt_report,
        sizeof(interrupt_report),
        &completion);
    RESULT[15] = poll_result;
    dm_usb_host_periodic_registry_init(&periodic_registry, sof.clock.speed);
    periodic_entry = (DmUsbHostPeriodicRegistryEntry) {
        .poller = &periodic.poller,
        .in_data = interrupt_report,
        .in_capacity = sizeof(interrupt_report),
        .completion = record_completion,
        .completion_opaque = (void *)&periodic_completion,
    };
    if (!dm_usb_host_periodic_registry_add(&periodic_registry,
                                           &periodic_entry) ||
        !dm_stm32h7_usb_host_periodic_registry_sof_init(
            &periodic_registry_sof, &sof, &periodic_registry)) {
        goto done;
    }
    if (!dm_stm32h7_usb_host_sof_irq_init(&sof_irq, USB_BASE,
                                          &periodic_registry_sof)) {
        goto done;
    }
    periodic_irq_events = 0;
    periodic_irq_submitted = 0;
    periodic_completion = completion;
    GINTSTS = GINTSTS_SOF;
    NVIC_ICPR2 = USB_IRQ_BIT;
    GINTMSK |= GINTSTS_SOF;
    GAHBCFG |= GAHBCFG_GINT;
    NVIC_ISER2 = USB_IRQ_BIT;
    while (!periodic_irq_submitted) {
        __asm__ volatile ("wfi" ::: "memory");
    }
    RESULT[16] = periodic_completion;
    RESULT[17] = periodic_irq_submitted;
    RESULT[18] = periodic_irq_events;
    RESULT[19] = HCCHAR(1);
    RESULT[20] = channel_allocator.entry[0].owner ? 1 : 0;

done:
    RESULT[RESULT_DONE_INDEX] = RESULT_DONE;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
