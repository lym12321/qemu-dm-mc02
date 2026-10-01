/* Synchronous control-transfer driver for the STM32H7 DWC2 host channels. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_channel_control.h"

#define DM_USB_HOST_CHANNEL_CONTROL_MAX_PACKET_SIZE \
    DM_STM32H7_OTG_HCCHAR_MPS_MASK

static DmUsbHostChannelControlResult control_result(
    DmUsbTransactionStatus status, size_t actual_length,
    unsigned transactions)
{
    return (DmUsbHostChannelControlResult) {
        .status = status,
        .actual_length = actual_length,
        .transactions = transactions,
    };
}

static DmUsbHostChannelControlResult control_invalid(void)
{
    return control_result(DM_USB_TRANSACTION_INVALID, 0, 0);
}

static DmUsbTransactionStatus control_channel_status(uint32_t hcint)
{
    if (hcint & DM_STM32H7_OTG_HCINT_STALL) {
        return DM_USB_TRANSACTION_STALL;
    }
    if (hcint & DM_STM32H7_OTG_HCINT_NAK) {
        return DM_USB_TRANSACTION_NAK;
    }
    if (hcint & DM_STM32H7_OTG_HCINT_XACTERR) {
        return DM_USB_TRANSACTION_INVALID;
    }
    if (hcint & DM_STM32H7_OTG_HCINT_XFRC) {
        return DM_USB_TRANSACTION_ACCEPTED;
    }
    return DM_USB_TRANSACTION_INVALID;
}

static void control_fifo_write(DmStm32H7OtgHost *host, unsigned channel,
                               const uint8_t *data, uint32_t length)
{
    while (length) {
        uint32_t value = 0;
        unsigned count = length < 4 ? length : 4;

        for (unsigned index = 0; index < count; ++index) {
            value |= (uint32_t)data[index] << (8 * index);
        }
        dm_stm32h7_otg_host_fifo_write(host, channel, value, count);
        data += count;
        length -= count;
    }
}

static void control_fifo_read(DmStm32H7OtgHost *host, unsigned channel,
                              uint8_t *data, uint32_t length)
{
    while (length) {
        unsigned count = length < 4 ? length : 4;
        uint32_t value = dm_stm32h7_otg_host_fifo_read(host, channel, count);

        for (unsigned index = 0; index < count; ++index) {
            data[index] = value >> (8 * index);
        }
        data += count;
        length -= count;
    }
}

static DmUsbHostChannelControlResult control_issue(
    DmUsbHostChannelControl *control, uint8_t device_address,
    bool direction_in, DmStm32H7OtgHostChannelPid pid,
    const uint8_t *out_data, uint32_t length, uint64_t timestamp_ns)
{
    uint32_t hcchar;
    uint32_t hctsiz;
    uint32_t remaining;
    uint32_t hcint;
    DmUsbTransactionStatus status;

    if (!control || !control->host ||
        control->channel >= DM_STM32H7_OTG_HOST_CHANNELS ||
        length > control->ep0_max_packet_size ||
        (!direction_in && length && !out_data)) {
        return control_invalid();
    }

    if (!direction_in && length) {
        control_fifo_write(control->host, control->channel, out_data, length);
    }
    dm_stm32h7_otg_host_write(
        control->host, DM_STM32H7_OTG_HCINT(control->channel),
        DM_STM32H7_OTG_HCINT_VALID_MASK, timestamp_ns);
    hctsiz = length | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT) |
             ((uint32_t)pid << DM_STM32H7_OTG_HCTSIZ_PID_SHIFT);
    dm_stm32h7_otg_host_write(control->host,
                               DM_STM32H7_OTG_HCTSIZ(control->channel),
                               hctsiz, timestamp_ns);
    hcchar = (uint32_t)device_address << DM_STM32H7_OTG_HCCHAR_DEVADDR_SHIFT;
    hcchar |= control->ep0_max_packet_size;
    if (direction_in) {
        hcchar |= DM_STM32H7_OTG_HCCHAR_EPDIR;
    }
    dm_stm32h7_otg_host_write(control->host,
                               DM_STM32H7_OTG_HCCHAR(control->channel),
                               hcchar | DM_STM32H7_OTG_HCCHAR_CHENA,
                               timestamp_ns);

    hcint = dm_stm32h7_otg_host_read(control->host,
                                     DM_STM32H7_OTG_HCINT(control->channel));
    status = control_channel_status(hcint);
    remaining = dm_stm32h7_otg_host_read(
        control->host, DM_STM32H7_OTG_HCTSIZ(control->channel)) &
        DM_STM32H7_OTG_HCTSIZ_XFERSIZE_MASK;
    if (status != DM_USB_TRANSACTION_ACCEPTED || remaining > length) {
        return control_result(status, 0, 1);
    }
    return control_result(status, length - remaining, 1);
}

void dm_usb_host_channel_control_init(DmUsbHostChannelControl *control,
                                      DmStm32H7OtgHost *host,
                                      unsigned channel,
                                      uint16_t ep0_max_packet_size)
{
    if (!control) {
        return;
    }
    *control = (DmUsbHostChannelControl) {
        .host = host,
        .channel = channel,
        .ep0_max_packet_size = ep0_max_packet_size ? ep0_max_packet_size : 64,
    };
}

DmUsbHostChannelControlResult dm_usb_host_channel_control_transfer(
    DmUsbHostChannelControl *control, uint8_t device_address,
    const uint8_t setup[8], const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, uint64_t timestamp_ns)
{
    DmUsbHostChannelControlResult result;
    bool direction_in;
    uint16_t requested_length;
    size_t offset = 0;
    unsigned transactions;
    DmStm32H7OtgHostChannelPid pid;

    if (!control || !control->host || !setup || device_address > 0x7f ||
        !control->ep0_max_packet_size ||
        control->ep0_max_packet_size >
            DM_USB_HOST_CHANNEL_CONTROL_MAX_PACKET_SIZE ||
        control->channel >= DM_STM32H7_OTG_HOST_CHANNELS ||
        (in_capacity && !in_data)) {
        return control_invalid();
    }
    direction_in = setup[0] & 0x80;
    requested_length = setup[6] | ((uint16_t)setup[7] << 8);
    if (direction_in) {
        if (out_data || out_length || in_capacity < requested_length) {
            return control_invalid();
        }
    } else if ((out_length && !out_data) || out_length != requested_length ||
               in_data || in_capacity) {
        return control_invalid();
    }

    result = control_issue(control, device_address, false,
                           DM_STM32H7_OTG_HOST_PID_SETUP, setup, 8,
                           timestamp_ns);
    if (result.status != DM_USB_TRANSACTION_ACCEPTED ||
        result.actual_length != 8) {
        return result;
    }
    transactions = result.transactions;

    if (direction_in) {
        pid = DM_STM32H7_OTG_HOST_PID_DATA1;
        while (offset < requested_length) {
            uint32_t packet_length = requested_length - offset <
                                     control->ep0_max_packet_size ?
                                     requested_length - offset :
                                     control->ep0_max_packet_size;

            result = control_issue(control, device_address, true, pid, NULL,
                                   packet_length, timestamp_ns);
            transactions += result.transactions;
            if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
                return control_result(result.status, offset, transactions);
            }
            if (result.actual_length > packet_length) {
                return control_result(DM_USB_TRANSACTION_INVALID, offset,
                                      transactions);
            }
            control_fifo_read(control->host, control->channel,
                              in_data + offset, result.actual_length);
            offset += result.actual_length;
            if (result.actual_length < packet_length) {
                break;
            }
            pid = pid == DM_STM32H7_OTG_HOST_PID_DATA0 ?
                  DM_STM32H7_OTG_HOST_PID_DATA1 :
                  DM_STM32H7_OTG_HOST_PID_DATA0;
        }
        result = control_issue(control, device_address, false,
                               DM_STM32H7_OTG_HOST_PID_DATA1, NULL, 0,
                               timestamp_ns);
    } else {
        pid = DM_STM32H7_OTG_HOST_PID_DATA1;
        while (offset < out_length) {
            uint32_t packet_length = out_length - offset <
                                     control->ep0_max_packet_size ?
                                     out_length - offset :
                                     control->ep0_max_packet_size;

            result = control_issue(control, device_address, false, pid,
                                   out_data + offset, packet_length,
                                   timestamp_ns);
            transactions += result.transactions;
            if (result.status != DM_USB_TRANSACTION_ACCEPTED ||
                result.actual_length != packet_length) {
                return control_result(
                    result.status == DM_USB_TRANSACTION_ACCEPTED ?
                    DM_USB_TRANSACTION_INVALID : result.status,
                    offset, transactions);
            }
            offset += packet_length;
            pid = pid == DM_STM32H7_OTG_HOST_PID_DATA0 ?
                  DM_STM32H7_OTG_HOST_PID_DATA1 :
                  DM_STM32H7_OTG_HOST_PID_DATA0;
        }
        result = control_issue(control, device_address, true,
                               DM_STM32H7_OTG_HOST_PID_DATA1, NULL, 0,
                               timestamp_ns);
    }
    transactions += result.transactions;
    if (result.status != DM_USB_TRANSACTION_ACCEPTED ||
        result.actual_length != 0) {
        return control_result(result.status, offset, transactions);
    }
    return control_result(DM_USB_TRANSACTION_ACCEPTED, offset, transactions);
}
