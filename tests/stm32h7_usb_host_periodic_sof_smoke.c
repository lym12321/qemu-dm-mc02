#include "dm_stm32h7_usb_host_periodic_sof.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define OTG_HFNUM 0x408u
#define OTG_HPRT0 0x440u

typedef struct TestSubmit {
    unsigned calls;
    DmUsbHostEndpointDataPid last_pid;
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
    (void)out_data;
    (void)out_length;
    (void)in_data;
    (void)in_capacity;
    ++test->calls;
    test->last_pid = pid;
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
    DmStm32H7UsbHostSof source;
    DmStm32H7UsbHostPeriodicSof event;
    DmUsbHostEndpointCompletion completion;
    TestSubmit test = { 0 };
    uint8_t data[8] = { 0 };

    registers[OTG_HFNUM / sizeof(uint32_t)] = 10;
    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    if (expect(dm_usb_host_periodic_poller_init(
                   &poller, &endpoint_state, DM_USB_HOST_SPEED_HIGH,
                   1000, submit, &test) == DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize periodic poller") ||
        expect(dm_stm32h7_usb_host_sof_init(
                   &source, (uintptr_t)registers, 1000) ==
                   DM_USB_HOST_SOF_CLOCK_OK &&
                   dm_stm32h7_usb_host_periodic_sof_init(
                       &event, &source, &poller),
               "bind high-speed source and poller") ||
        expect(dm_stm32h7_usb_host_periodic_sof_on_event(
                   &event, NULL, 0, data, sizeof(data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   completion == DM_USB_HOST_ENDPOINT_NAK && test.calls == 1 &&
                   test.last_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "first SOF event submits due poll") ) {
        return 1;
    }

    registers[OTG_HFNUM / sizeof(uint32_t)] = 11;
    if (expect(dm_stm32h7_usb_host_periodic_sof_on_event(
                   &event, NULL, 0, data, sizeof(data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_NOT_DUE && test.calls == 1,
               "intermediate SOF does not submit") ) {
        return 1;
    }

    registers[OTG_HFNUM / sizeof(uint32_t)] = 12;
    if (expect(dm_stm32h7_usb_host_periodic_sof_on_event(
                   &event, NULL, 0, data, sizeof(data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED && test.calls == 2 &&
                   test.last_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "due SOF submits one later NAK poll") ) {
        return 1;
    }

    poller.schedule.speed = DM_USB_HOST_SPEED_FULL;
    if (expect(!dm_stm32h7_usb_host_periodic_sof_init(
                   &event, &source, &poller),
               "reject source and schedule speed mismatch")) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host periodic SOF smoke passed");
    return 0;
}
