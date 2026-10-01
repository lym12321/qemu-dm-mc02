/* Board-independent bridge from STM32H7 host channels to USB transactions. */
#ifndef HW_USB_DM_USB_HOST_CHANNEL_TRANSPORT_H
#define HW_USB_DM_USB_HOST_CHANNEL_TRANSPORT_H

#include "hw/usb/dm_stm32h7_otg_host.h"
#include "hw/usb/dm_usb_transaction.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_USB_HOST_CHANNEL_TRANSPORT_PACKET_CAPACITY 2047

typedef DmUsbTransactionResult DmUsbHostChannelTransportSubmit(
    void *opaque, const DmUsbTransaction *transaction);
/*
 * Optional device-address routing at the controller-to-transport boundary.
 * The transaction and its data buffers remain valid only for this call.
 */
typedef DmUsbTransactionResult DmUsbHostChannelTransportRoute(
    void *opaque, uint8_t device_address,
    const DmUsbTransaction *transaction);
typedef bool DmUsbHostChannelTransportReadOut(void *opaque, unsigned channel,
                                              uint8_t *data, uint32_t length);
typedef bool DmUsbHostChannelTransportWriteIn(void *opaque, unsigned channel,
                                              const uint8_t *data,
                                              uint32_t length);
/*
 * Optional terminal-completion scheduler. The transport has already copied
 * any accepted IN data before this callback runs. The scheduler must deliver
 * exactly one completion, immediately or later, without retaining a request,
 * transaction, or packet-buffer pointer.
 */
typedef void DmUsbHostChannelTransportScheduleCompletion(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length);

typedef struct DmUsbHostChannelTransport {
    DmStm32H7OtgHost *host;
    DmUsbHostChannelTransportSubmit *submit;
    DmUsbHostChannelTransportRoute *route;
    DmUsbHostChannelTransportReadOut *read_out;
    DmUsbHostChannelTransportWriteIn *write_in;
    DmUsbHostChannelTransportScheduleCompletion *schedule_completion;
    void *submit_opaque;
    void *route_opaque;
    void *read_out_opaque;
    void *write_in_opaque;
    void *completion_opaque;
    uint8_t packet[DM_USB_HOST_CHANNEL_TRANSPORT_PACKET_CAPACITY];
} DmUsbHostChannelTransport;

void dm_usb_host_channel_transport_init(
    DmUsbHostChannelTransport *transport, DmStm32H7OtgHost *host,
    DmUsbHostChannelTransportSubmit *submit,
    DmUsbHostChannelTransportReadOut *read_out,
    DmUsbHostChannelTransportWriteIn *write_in, void *opaque);
void dm_usb_host_channel_transport_init_with_opaques(
    DmUsbHostChannelTransport *transport, DmStm32H7OtgHost *host,
    DmUsbHostChannelTransportSubmit *submit, void *submit_opaque,
    DmUsbHostChannelTransportReadOut *read_out, void *read_out_opaque,
    DmUsbHostChannelTransportWriteIn *write_in, void *write_in_opaque);
void dm_usb_host_channel_transport_set_route(
    DmUsbHostChannelTransport *transport,
    DmUsbHostChannelTransportRoute *route, void *opaque);
void dm_usb_host_channel_transport_set_completion_scheduler(
    DmUsbHostChannelTransport *transport,
    DmUsbHostChannelTransportScheduleCompletion *schedule_completion,
    void *opaque);

#endif /* HW_USB_DM_USB_HOST_CHANNEL_TRANSPORT_H */
