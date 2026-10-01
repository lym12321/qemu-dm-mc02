/* Synchronous control-transfer driver for the STM32H7 DWC2 host channels. */
#ifndef HW_USB_DM_USB_HOST_CHANNEL_CONTROL_H
#define HW_USB_DM_USB_HOST_CHANNEL_CONTROL_H

#include "hw/usb/dm_stm32h7_otg_host.h"
#include "hw/usb/dm_usb_transaction.h"

#include <stddef.h>
#include <stdint.h>

typedef struct DmUsbHostChannelControlResult {
    DmUsbTransactionStatus status;
    size_t actual_length;
    unsigned transactions;
} DmUsbHostChannelControlResult;

/*
 * This is a synchronous controller-side driver for fixture and integration
 * users. It programs one H723 host channel; QEMU USB ownership and board
 * wiring remain outside this boundary.
 */
typedef struct DmUsbHostChannelControl {
    DmStm32H7OtgHost *host;
    unsigned channel;
    uint16_t ep0_max_packet_size;
} DmUsbHostChannelControl;

void dm_usb_host_channel_control_init(DmUsbHostChannelControl *control,
                                      DmStm32H7OtgHost *host,
                                      unsigned channel,
                                      uint16_t ep0_max_packet_size);

/*
 * Drives setup, zero or more data packets, and the opposite-direction status
 * packet through HCCHAR/HCTSIZ/HCFIFO. The device address is written to
 * HCCHAR for every stage, so a SET_ADDRESS completion changes the address
 * used only by a subsequent call.
 */
DmUsbHostChannelControlResult dm_usb_host_channel_control_transfer(
    DmUsbHostChannelControl *control, uint8_t device_address,
    const uint8_t setup[8], const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, uint64_t timestamp_ns);

#endif /* HW_USB_DM_USB_HOST_CHANNEL_CONTROL_H */
