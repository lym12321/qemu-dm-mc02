#include "dm_stm32h7_usb_host_periodic_registry_sof.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define OTG_HFNUM 0x408u
#define OTG_HPRT0 0x440u

typedef struct TestSubmit {
    unsigned calls;
} TestSubmit;

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static DmUsbHostPeriodicSubmitResult submit(
    void *opaque, const DmUsbHostPipe *pipe, DmUsbHostEndpointDataPid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion,
    size_t *actual_length)
{
    TestSubmit *test = opaque;

    (void)pipe;
    (void)pid;
    (void)out_data;
    (void)out_length;
    (void)in_data;
    (void)in_capacity;
    ++test->calls;
    *completion = DM_USB_HOST_ENDPOINT_NAK;
    *actual_length = 0;
    return DM_USB_HOST_PERIODIC_SUBMIT_OK;
}

int main(void)
{
    uint32_t registers[(OTG_HPRT0 / sizeof(uint32_t)) + 1] = { 0 };
    DmUsbHostPipe pipe = {
        .device_address = 5,
        .endpoint_number = 1,
        .attributes = DM_USB_HOST_PIPE_INTERRUPT,
        .direction_in = true,
        .transfer_type = DM_USB_HOST_PIPE_INTERRUPT,
        .max_packet_size = 8,
        .transactions_per_microframe = 1,
        .interval = 2,
    };
    DmUsbHostEndpointState endpoint_state;
    DmUsbHostPeriodicPoller poller;
    DmUsbHostPeriodicRegistry registry;
    DmUsbHostPeriodicRegistryEntry entry;
    DmStm32H7UsbHostSof source;
    DmStm32H7UsbHostPeriodicRegistrySof event;
    TestSubmit submit_state = { 0 };
    uint8_t data[8];

    registers[OTG_HFNUM / sizeof(uint32_t)] = 10;
    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    dm_usb_host_periodic_poller_init(
        &poller, &endpoint_state, DM_USB_HOST_SPEED_HIGH, 1000,
        submit, &submit_state);
    dm_usb_host_periodic_registry_init(&registry, DM_USB_HOST_SPEED_HIGH);
    entry = (DmUsbHostPeriodicRegistryEntry) {
        .poller = &poller,
        .in_data = data,
        .in_capacity = sizeof(data),
    };
    if (expect(dm_usb_host_periodic_registry_add(&registry, &entry) &&
                   dm_stm32h7_usb_host_sof_init(
                       &source, (uintptr_t)registers, 1000) ==
                   DM_USB_HOST_SOF_CLOCK_OK &&
                   dm_stm32h7_usb_host_periodic_registry_sof_init(
                       &event, &source, &registry),
               "bind source and registry") ||
        expect(dm_stm32h7_usb_host_periodic_registry_sof_on_event(&event) == 1 &&
                   submit_state.calls == 1,
               "first due SOF dispatches registry endpoint")) {
        return 1;
    }

    registers[OTG_HFNUM / sizeof(uint32_t)] = 11;
    if (expect(dm_stm32h7_usb_host_periodic_registry_sof_on_event(&event) == 0 &&
                   submit_state.calls == 1,
               "intermediate SOF does not dispatch") ) {
        return 1;
    }

    registers[OTG_HFNUM / sizeof(uint32_t)] = 12;
    if (expect(dm_stm32h7_usb_host_periodic_registry_sof_on_event(&event) == 1 &&
                   submit_state.calls == 2,
               "later due SOF dispatches once") ) {
        return 1;
    }

    registry.speed = DM_USB_HOST_SPEED_FULL;
    if (expect(!dm_stm32h7_usb_host_periodic_registry_sof_init(
                   &event, &source, &registry),
               "reject source and registry speed mismatch")) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host periodic registry SOF smoke passed");
    return 0;
}
