/* Minimal polling control-transfer driver for STM32H7 DWC2 host mode. */
#ifndef DM_STM32H7_USB_HOST_CONTROL_H
#define DM_STM32H7_USB_HOST_CONTROL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum DmStm32H7UsbHostControlResult {
    DM_STM32H7_USB_HOST_CONTROL_OK,
    DM_STM32H7_USB_HOST_CONTROL_NAK,
    DM_STM32H7_USB_HOST_CONTROL_STALL,
    DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR,
    DM_STM32H7_USB_HOST_CONTROL_INVALID,
} DmStm32H7UsbHostControlResult;

typedef struct DmStm32H7UsbHostControl DmStm32H7UsbHostControl;

typedef void (*DmStm32H7UsbHostControlPoll)(
    void *opaque, DmStm32H7UsbHostControl *control);

struct DmStm32H7UsbHostControl {
    uintptr_t base;
    uint8_t channel;
    uint8_t device_address;
    uint16_t ep0_max_packet_size;
    DmStm32H7UsbHostControlPoll poll;
    void *poll_opaque;
};

void dm_stm32h7_usb_host_control_init(DmStm32H7UsbHostControl *control,
                                      uintptr_t base, uint8_t channel,
                                      uint16_t ep0_max_packet_size,
                                      uint8_t device_address);

/* Advance an optional virtual controller before each HCINT poll. */
void dm_stm32h7_usb_host_control_set_poll(
    DmStm32H7UsbHostControl *control,
    DmStm32H7UsbHostControlPoll poll, void *opaque);

/* Assert/deassert HPRT0.RST for an already attached root-port device. */
bool dm_stm32h7_usb_host_control_port_reset(
    DmStm32H7UsbHostControl *control);

/*
 * Run one complete synchronous EP0 control transfer at device_address.
 * NAK ends this attempt after the channel reports halted; a caller may retry
 * the complete transfer at a later virtual-time event.
 */
DmStm32H7UsbHostControlResult dm_stm32h7_usb_host_control_transfer(
    DmStm32H7UsbHostControl *control, const uint8_t setup[8],
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length);

#endif /* DM_STM32H7_USB_HOST_CONTROL_H */
