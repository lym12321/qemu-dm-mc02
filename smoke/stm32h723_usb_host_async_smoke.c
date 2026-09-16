#include <stdint.h>

#include "dm_stm32h7_usb_host_control.h"
#include "dm_stm32h7_irq_lock.h"
#include "dm_stm32h7_usb_host_pipe.h"
#include "dm_stm32h7_usb_host_pipe_async_dispatch.h"
#include "dm_usb_host_descriptor.h"
#include "dm_usb_host_pipe.h"

#define USB_BASE 0x40040000u
#define HPRT0 (*(volatile uint32_t *)(USB_BASE + 0x440u))
#define GAHBCFG (*(volatile uint32_t *)(USB_BASE + 0x008u))
#define GINTMSK (*(volatile uint32_t *)(USB_BASE + 0x018u))
#define HAINTMSK (*(volatile uint32_t *)(USB_BASE + 0x418u))
#define HCINTMSK(channel) \
    (*(volatile uint32_t *)(USB_BASE + 0x50cu + 0x20u * (channel)))
#define RESULT ((volatile uint32_t *)0x20000000u)
#define NVIC_ISER2 (*(volatile uint32_t *)0xe000e108u)
#define NVIC_ICPR2 (*(volatile uint32_t *)0xe000e288u)

#define GAHBCFG_GINT (1u << 0)
#define GINTSTS_HCINT (1u << 25)
#define HCINT_XFRC (1u << 0)
#define HCINT_CHHLTD (1u << 1)
#define HCINT_NAK (1u << 4)
#define HCINT_XACTERR (1u << 7)
#define HCINT_CHANNEL_EVENTS (HCINT_XFRC | HCINT_CHHLTD | HCINT_NAK | \
                              HCINT_XACTERR)
#define USB_IRQ_BIT (1u << 13)
#define RESULT_MARKER 0x48534155u /* "UASH" */
#define RESULT_DONE 0x444f4e45u /* "DONE" */
#define RESULT_DONE_INDEX 24u

void Reset_Handler(void);
void Usb_Handler(void);

static DmUsbHostChannelAllocator channel_allocator;
static DmStm32H7UsbHostPipeAsyncDispatch dispatch;
static DmStm32H7UsbHostPipeAsync async_cancelled;
static DmStm32H7UsbHostPipeAsync async_nak;
static DmStm32H7IrqLock irq_lock;
static uint8_t cancelled_report[8];
static uint8_t nak_report[8];
static volatile uint32_t irq_events;
static volatile uint32_t irq_completions;

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16 + 77 + 1] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16 + 77] = (uint32_t)(uintptr_t)Usb_Handler,
};

static int control_ok(DmStm32H7UsbHostControlResult result)
{
    return result == DM_STM32H7_USB_HOST_CONTROL_OK;
}

void Usb_Handler(void)
{
    ++irq_events;
    irq_completions += dm_stm32h7_usb_host_pipe_async_dispatch_handle(
        &dispatch, 0);
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
    static const uint8_t channel_ids[] = { 0, 1 };
    DmStm32H7UsbHostControl control;
    DmStm32H7UsbHostControlResult control_result;
    DmUsbHostConfiguration configuration;
    DmUsbHostInterface interface_descriptor;
    DmUsbHostEndpoint endpoint;
    DmUsbHostPipe pipe;
    uint8_t descriptor[18];
    uint8_t configuration_header[9];
    uint8_t configuration_descriptor[64];
    uint8_t get_configuration[8] = { 0x80, 0x06, 0, 0x02, 0, 0, 0, 0 };
    uint8_t set_configuration[8] = { 0x00, 0x09, 0, 0, 0, 0, 0, 0 };
    DmUsbHostChannelCompletionToken cancelled_token;
    DmUsbHostChannelCompletionToken nak_token;
    DmStm32H7UsbHostPipeAsyncStartResult start_cancelled;
    DmStm32H7UsbHostPipeAsyncStartResult start_nak;
    DmUsbHostEndpointCompletion completion;
    uint64_t completion_timestamp;
    size_t completion_length;
    size_t actual_length;

    RESULT[0] = RESULT_MARKER;
    dm_stm32h7_usb_host_control_init(&control, USB_BASE, 0, 64, 0);
    RESULT[1] = dm_stm32h7_usb_host_control_port_reset(&control) ? HPRT0 : 0;
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, get_descriptor, NULL, 0, descriptor, sizeof(descriptor),
        &actual_length);
    RESULT[2] = control_result;
    RESULT[3] = actual_length == sizeof(descriptor) ?
                descriptor[0] | ((uint32_t)descriptor[1] << 8) : 0;
    if (!control_ok(control_result) || actual_length != sizeof(descriptor)) {
        goto done;
    }
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, set_address, NULL, 0, NULL, 0, &actual_length);
    RESULT[4] = control_result;
    if (!control_ok(control_result)) {
        goto done;
    }
    control.device_address = 5;
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, get_configuration_header, NULL, 0, configuration_header,
        sizeof(configuration_header), &actual_length);
    RESULT[5] = control_result;
    if (!control_ok(control_result) || actual_length != sizeof(configuration_header) ||
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
    if (!control_ok(control_result) || actual_length != configuration.total_length ||
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
    RESULT[8] = endpoint.address | ((uint32_t)endpoint.attributes << 8) |
                ((uint32_t)pipe.max_packet_size << 16);
    if (interface_descriptor.interface_class != 3 ||
        pipe.device_address != 5 || pipe.endpoint_number != 1 ||
        !pipe.direction_in || pipe.transfer_type != DM_USB_HOST_PIPE_INTERRUPT ||
        pipe.max_packet_size != 8 || pipe.transactions_per_microframe != 1) {
        goto done;
    }
    set_configuration[2] = configuration.value;
    control_result = dm_stm32h7_usb_host_control_transfer(
        &control, set_configuration, NULL, 0, NULL, 0, &actual_length);
    RESULT[9] = control_result;
    dm_stm32h7_irq_lock_init(&irq_lock);
    if (!control_ok(control_result) ||
        !dm_usb_host_channel_allocator_init(
            &channel_allocator, channel_ids, sizeof(channel_ids)) ||
        !dm_stm32h7_usb_host_pipe_async_init(&async_cancelled) ||
        !dm_stm32h7_usb_host_pipe_async_init(&async_nak) ||
        !dm_stm32h7_usb_host_pipe_async_dispatch_init(
            &dispatch, USB_BASE, dm_stm32h7_irq_lock_enter,
            dm_stm32h7_irq_lock_leave, &irq_lock)) {
        goto done;
    }

    dm_stm32h7_irq_lock_enter(&irq_lock);
    dm_stm32h7_irq_lock_enter(&irq_lock);
    if (irq_lock.depth != 2) {
        dm_stm32h7_irq_lock_leave(&irq_lock);
        dm_stm32h7_irq_lock_leave(&irq_lock);
        goto done;
    }
    dm_stm32h7_irq_lock_leave(&irq_lock);
    dm_stm32h7_irq_lock_leave(&irq_lock);
    if (irq_lock.depth) {
        goto done;
    }

    /* Program masks before starting, but keep the global/NVIC gate closed
     * until the deliberate channel-0 cancellation is complete. */
    HCINTMSK(0) = HCINT_CHANNEL_EVENTS;
    HCINTMSK(1) = HCINT_CHANNEL_EVENTS;
    HAINTMSK = (1u << 0) | (1u << 1);
    start_cancelled = dm_stm32h7_usb_host_pipe_async_dispatch_start(
        &dispatch, &async_cancelled, USB_BASE, &channel_allocator, &pipe,
        DM_STM32H7_USB_HOST_PIPE_PID_DATA0, NULL, 0, cancelled_report,
        sizeof(cancelled_report), &cancelled_token);
    RESULT[10] = start_cancelled;
    start_nak = dm_stm32h7_usb_host_pipe_async_dispatch_start(
        &dispatch, &async_nak, USB_BASE, &channel_allocator, &pipe,
        DM_STM32H7_USB_HOST_PIPE_PID_DATA0, NULL, 0, nak_report,
        sizeof(nak_report), &nak_token);
    RESULT[11] = start_nak;
    if (start_cancelled != DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED ||
        start_nak != DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED) {
        goto done;
    }
    RESULT[12] = cancelled_token.generation;
    RESULT[13] = nak_token.generation;
    RESULT[14] = dm_stm32h7_usb_host_pipe_async_dispatch_cancel(
        &dispatch, &async_cancelled, &cancelled_token, 1);
    RESULT[15] = dm_usb_host_channel_completion_state(
        &async_cancelled.completion);
    RESULT[16] = dm_usb_host_channel_completion_state(&async_nak.completion);

    irq_events = 0;
    irq_completions = 0;
    GINTMSK |= GINTSTS_HCINT;
    GAHBCFG |= GAHBCFG_GINT;
    NVIC_ICPR2 = USB_IRQ_BIT;
    NVIC_ISER2 = USB_IRQ_BIT;
    while (!irq_completions) {
        __asm__ volatile ("wfi" ::: "memory");
    }
    RESULT[17] = dm_usb_host_channel_completion_state(&async_nak.completion);
    if (!dm_usb_host_channel_completion_result(
            &async_nak.completion, &completion, &completion_length,
            &completion_timestamp)) {
        goto done;
    }
    RESULT[18] = completion;
    RESULT[19] = completion_length;
    RESULT[20] = irq_events;
    RESULT[21] = irq_completions;
    RESULT[22] = (channel_allocator.entry[0].owner ? 1u : 0u) |
                 (channel_allocator.entry[1].owner ? 2u : 0u);
    RESULT[23] = (dispatch.async[0] ? 1u : 0u) |
                 (dispatch.async[1] ? 2u : 0u);

done:
    RESULT[RESULT_DONE_INDEX] = RESULT_DONE;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
