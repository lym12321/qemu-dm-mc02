/* STM32H7 PIO adapter for a controller-independent periodic USB poller. */
#ifndef DM_STM32H7_USB_HOST_PERIODIC_H
#define DM_STM32H7_USB_HOST_PERIODIC_H

#include "dm_stm32h7_usb_host_pipe.h"
#include "dm_usb_host_periodic_poller.h"

typedef struct DmStm32H7UsbHostPeriodic {
    DmUsbHostPeriodicPoller poller;
    uintptr_t base;
    DmUsbHostChannelAllocator *allocator;
    DmStm32H7UsbHostPipeTransfer *transfer;
    void *transfer_opaque;
} DmStm32H7UsbHostPeriodic;

DmUsbHostPeriodicScheduleResult dm_stm32h7_usb_host_periodic_init_with_transfer(
    DmStm32H7UsbHostPeriodic *periodic, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    DmStm32H7UsbHostPipeTransfer *transfer, void *transfer_opaque,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns);

DmUsbHostPeriodicScheduleResult
dm_stm32h7_usb_host_periodic_init_with_transfer_and_retry(
    DmStm32H7UsbHostPeriodic *periodic, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    DmStm32H7UsbHostPipeTransfer *transfer, void *transfer_opaque,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns, const DmUsbHostRetryConfig *retry_config);

DmUsbHostPeriodicScheduleResult dm_stm32h7_usb_host_periodic_init_with_retry(
    DmStm32H7UsbHostPeriodic *periodic, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns, const DmUsbHostRetryConfig *retry_config);

DmUsbHostPeriodicScheduleResult dm_stm32h7_usb_host_periodic_init(
    DmStm32H7UsbHostPeriodic *periodic,
    uintptr_t base, DmUsbHostChannelAllocator *allocator,
    DmUsbHostEndpointState *endpoint_state, DmUsbHostSpeed speed,
    uint64_t origin_ns);

DmUsbHostPeriodicPollResult dm_stm32h7_usb_host_periodic_poll(
    DmStm32H7UsbHostPeriodic *periodic, uint64_t timestamp_ns,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion);

#endif /* DM_STM32H7_USB_HOST_PERIODIC_H */
