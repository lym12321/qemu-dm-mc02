/* Board-independent bridge from STM32H7 host channels to USB transactions. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_channel_transport.h"

static DmUsbTransactionPid dm_usb_host_channel_transport_pid(
    DmStm32H7OtgHostChannelPid pid)
{
    switch (pid) {
    case DM_STM32H7_OTG_HOST_PID_DATA0:
        return DM_USB_TRANSACTION_PID_DATA0;
    case DM_STM32H7_OTG_HOST_PID_DATA1:
        return DM_USB_TRANSACTION_PID_DATA1;
    default:
        return DM_USB_TRANSACTION_PID_AUTO;
    }
}

static DmStm32H7OtgHostChannelCompletion
dm_usb_host_channel_transport_completion(DmUsbTransactionStatus status)
{
    switch (status) {
    case DM_USB_TRANSACTION_ACCEPTED:
        return DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED;
    case DM_USB_TRANSACTION_NAK:
        return DM_STM32H7_OTG_HOST_CHANNEL_NAK;
    case DM_USB_TRANSACTION_STALL:
        return DM_STM32H7_OTG_HOST_CHANNEL_STALL;
    default:
        return DM_STM32H7_OTG_HOST_CHANNEL_TRANSACTION_ERROR;
    }
}

static void dm_usb_host_channel_transport_complete(
    DmUsbHostChannelTransport *transport, unsigned channel,
    uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length)
{
    if (transport->schedule_completion) {
        transport->schedule_completion(
            transport->completion_opaque, transport->host, channel,
            completion_token, completion, actual_length);
        return;
    }
    dm_stm32h7_otg_host_complete_channel(transport->host, channel,
                                         completion, actual_length);
}

static void dm_usb_host_channel_transport_start(
    void *opaque, const DmStm32H7OtgHostChannelRequest *request)
{
    DmUsbHostChannelTransport *transport = opaque;
    DmUsbTransaction transaction = {
        .pid = dm_usb_host_channel_transport_pid(request->pid),
        .endpoint = request->endpoint,
        .timestamp_ns = request->timestamp_ns,
    };
    DmUsbTransactionResult result;
    uint8_t *packet = transport->packet;

    if (request->transfer_size > DM_USB_HOST_CHANNEL_TRANSPORT_PACKET_CAPACITY ||
        (!transport->route && !transport->submit)) {
        dm_usb_host_channel_transport_complete(
            transport, request->channel,
            request->completion_token,
            DM_STM32H7_OTG_HOST_CHANNEL_TRANSACTION_ERROR, 0);
        return;
    }
    if (request->endpoint_type == DM_USB_ENDPOINT_CONTROL &&
        request->pid == DM_STM32H7_OTG_HOST_PID_SETUP) {
        transaction.token = DM_USB_TRANSACTION_SETUP;
        transaction.out_data = packet;
        transaction.length = request->transfer_size;
    } else if (request->direction_in) {
        transaction.token = DM_USB_TRANSACTION_IN;
        transaction.in_data = packet;
        transaction.capacity = request->transfer_size;
    } else {
        transaction.token = DM_USB_TRANSACTION_OUT;
        transaction.out_data = packet;
        transaction.length = request->transfer_size;
    }
    if ((transaction.token == DM_USB_TRANSACTION_SETUP ||
         transaction.token == DM_USB_TRANSACTION_OUT) &&
        transaction.length &&
        (!transport->read_out ||
         !transport->read_out(transport->read_out_opaque, request->channel,
                              packet, transaction.length))) {
        dm_usb_host_channel_transport_complete(
            transport, request->channel,
            request->completion_token,
            DM_STM32H7_OTG_HOST_CHANNEL_TRANSACTION_ERROR, 0);
        return;
    }

    if (transport->route) {
        result = transport->route(transport->route_opaque,
                                  request->device_address, &transaction);
    } else {
        result = transport->submit(transport->submit_opaque, &transaction);
    }
    if (result.status == DM_USB_TRANSACTION_ACCEPTED &&
        result.actual_length > request->transfer_size) {
        dm_usb_host_channel_transport_complete(
            transport, request->channel,
            request->completion_token,
            DM_STM32H7_OTG_HOST_CHANNEL_TRANSACTION_ERROR, 0);
        return;
    }
    if (result.status == DM_USB_TRANSACTION_ACCEPTED &&
        transaction.token == DM_USB_TRANSACTION_IN && transport->write_in &&
        !transport->write_in(transport->write_in_opaque, request->channel,
                             packet, result.actual_length)) {
        dm_usb_host_channel_transport_complete(
            transport, request->channel,
            request->completion_token,
            DM_STM32H7_OTG_HOST_CHANNEL_TRANSACTION_ERROR, 0);
        return;
    }
    dm_usb_host_channel_transport_complete(
        transport, request->channel,
        request->completion_token,
        dm_usb_host_channel_transport_completion(result.status),
        result.actual_length);
}

void dm_usb_host_channel_transport_init(
    DmUsbHostChannelTransport *transport, DmStm32H7OtgHost *host,
    DmUsbHostChannelTransportSubmit *submit,
    DmUsbHostChannelTransportReadOut *read_out,
    DmUsbHostChannelTransportWriteIn *write_in, void *opaque)
{
    dm_usb_host_channel_transport_init_with_opaques(
        transport, host, submit, opaque, read_out, opaque, write_in, opaque);
}

void dm_usb_host_channel_transport_init_with_opaques(
    DmUsbHostChannelTransport *transport, DmStm32H7OtgHost *host,
    DmUsbHostChannelTransportSubmit *submit, void *submit_opaque,
    DmUsbHostChannelTransportReadOut *read_out, void *read_out_opaque,
    DmUsbHostChannelTransportWriteIn *write_in, void *write_in_opaque)
{
    *transport = (DmUsbHostChannelTransport) {
        .host = host,
        .submit = submit,
        .read_out = read_out,
        .write_in = write_in,
        .submit_opaque = submit_opaque,
        .read_out_opaque = read_out_opaque,
        .write_in_opaque = write_in_opaque,
    };
    dm_stm32h7_otg_host_set_channel_start(
        host, dm_usb_host_channel_transport_start, transport);
}

void dm_usb_host_channel_transport_set_route(
    DmUsbHostChannelTransport *transport,
    DmUsbHostChannelTransportRoute *route, void *opaque)
{
    transport->route = route;
    transport->route_opaque = opaque;
}

void dm_usb_host_channel_transport_set_completion_scheduler(
    DmUsbHostChannelTransport *transport,
    DmUsbHostChannelTransportScheduleCompletion *schedule_completion,
    void *opaque)
{
    transport->schedule_completion = schedule_completion;
    transport->completion_opaque = opaque;
}
