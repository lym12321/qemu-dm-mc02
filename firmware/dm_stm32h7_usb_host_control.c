/* Minimal polling control-transfer driver for STM32H7 DWC2 host mode. */
#include "dm_stm32h7_usb_host_control.h"

#define OTG_HPRT0                  0x440u
#define OTG_HCCHAR(channel)        (0x500u + 0x20u * (channel))
#define OTG_HCINT(channel)         (0x508u + 0x20u * (channel))
#define OTG_HCTSIZ(channel)        (0x510u + 0x20u * (channel))
#define OTG_HCFIFO(channel)        (0x1000u + 0x1000u * (channel))

#define HPRT0_PWR                  (1u << 12)
#define HPRT0_RST                  (1u << 8)
#define HPRT0_ENACHG               (1u << 3)
#define HPRT0_ENA                  (1u << 2)
#define HPRT0_CONNDET              (1u << 1)
#define HPRT0_CONNSTS              (1u << 0)

#define HCCHAR_CHENA               (1u << 31)
#define HCCHAR_CHDIS               (1u << 30)
#define HCCHAR_DEVADDR_SHIFT       22
#define HCCHAR_EPDIR               (1u << 15)

#define HCINT_XFRC                 (1u << 0)
#define HCINT_CHHLTD               (1u << 1)
#define HCINT_STALL                (1u << 3)
#define HCINT_NAK                  (1u << 4)
#define HCINT_XACTERR              (1u << 7)
#define HCINT_ALL                  0x3fffu

#define HCTSIZ_PID_SHIFT           29
#define HCTSIZ_PKT_SHIFT           19
#define HCTSIZ_XFERSIZE_MASK       0x7ffffu
#define HCTSIZ_PID_DATA0           0u
#define HCTSIZ_PID_DATA1           2u
#define HCTSIZ_PID_SETUP           3u
#define STM32H7_USB_HOST_CHANNELS  12u
#define HCCHAR_DEVADDR_MAX         0x7fu
#define HCCHAR_MPS_MASK            0x7ffu

static bool dm_stm32h7_usb_host_control_config_valid(
    const DmStm32H7UsbHostControl *control)
{
    return control && control->channel < STM32H7_USB_HOST_CHANNELS &&
           control->device_address <= HCCHAR_DEVADDR_MAX &&
           control->ep0_max_packet_size &&
           control->ep0_max_packet_size <= HCCHAR_MPS_MASK;
}

static volatile uint32_t *dm_stm32h7_usb_host_reg(
    const DmStm32H7UsbHostControl *control, uint32_t offset)
{
    return (volatile uint32_t *)(control->base + offset);
}

static void dm_stm32h7_usb_host_fifo_write(
    const DmStm32H7UsbHostControl *control, const uint8_t *data,
    size_t length)
{
    volatile uint8_t *fifo = (volatile uint8_t *)(control->base +
                                                   OTG_HCFIFO(control->channel));

    for (size_t index = 0; index < length; ++index) {
        fifo[0] = data[index];
    }
}

static void dm_stm32h7_usb_host_fifo_read(
    const DmStm32H7UsbHostControl *control, uint8_t *data, size_t length)
{
    volatile uint8_t *fifo = (volatile uint8_t *)(control->base +
                                                   OTG_HCFIFO(control->channel));

    for (size_t index = 0; index < length; ++index) {
        data[index] = fifo[0];
    }
}

static DmStm32H7UsbHostControlResult dm_stm32h7_usb_host_stage(
    DmStm32H7UsbHostControl *control, uint32_t pid, bool direction_in,
    const uint8_t *out_data, uint8_t *in_data, size_t length,
    size_t *actual_length)
{
    volatile uint32_t *hcint = dm_stm32h7_usb_host_reg(
        control, OTG_HCINT(control->channel));
    volatile uint32_t *hctsiz = dm_stm32h7_usb_host_reg(
        control, OTG_HCTSIZ(control->channel));
    volatile uint32_t *hcchar = dm_stm32h7_usb_host_reg(
        control, OTG_HCCHAR(control->channel));
    uint32_t interrupt;
    uint32_t remaining;
    size_t actual;

    *hcint = HCINT_ALL;
    if (!direction_in && length) {
        dm_stm32h7_usb_host_fifo_write(control, out_data, length);
    }
    *hctsiz = (uint32_t)length | (1u << HCTSIZ_PKT_SHIFT) |
              (pid << HCTSIZ_PID_SHIFT);
    *hcchar = ((uint32_t)control->device_address << HCCHAR_DEVADDR_SHIFT) |
              control->ep0_max_packet_size |
              (direction_in ? HCCHAR_EPDIR : 0) | HCCHAR_CHENA;

    do {
        if (control->poll) {
            control->poll(control->poll_opaque, control);
        }
        interrupt = *hcint;
    } while (!(interrupt & (HCINT_CHHLTD | HCINT_NAK)));

    if (interrupt & HCINT_XFRC) {
        remaining = *hctsiz & HCTSIZ_XFERSIZE_MASK;
        if (remaining > length) {
            *actual_length = 0;
            *hcint = HCINT_ALL;
            return DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR;
        }
        actual = length - remaining;
        if (direction_in && actual) {
            dm_stm32h7_usb_host_fifo_read(control, in_data, actual);
        }
        *actual_length = actual;
        *hcint = HCINT_ALL;
        return DM_STM32H7_USB_HOST_CONTROL_OK;
    }
    if (interrupt & HCINT_NAK) {
        /* Stop the channel before returning it to a caller-owned retry. */
        *hcchar |= HCCHAR_CHDIS;
        do {
            if (control->poll) {
                control->poll(control->poll_opaque, control);
            }
            interrupt = *hcint;
        } while (!(interrupt & HCINT_CHHLTD));
        *actual_length = 0;
        *hcint = HCINT_ALL;
        return DM_STM32H7_USB_HOST_CONTROL_NAK;
    }
    *actual_length = 0;
    *hcint = HCINT_ALL;
    if (interrupt & HCINT_STALL) {
        return DM_STM32H7_USB_HOST_CONTROL_STALL;
    }
    if (interrupt & HCINT_XACTERR) {
        return DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR;
    }
    return DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR;
}

void dm_stm32h7_usb_host_control_init(DmStm32H7UsbHostControl *control,
                                      uintptr_t base, uint8_t channel,
                                      uint16_t ep0_max_packet_size,
                                      uint8_t device_address)
{
    *control = (DmStm32H7UsbHostControl) {
        .base = base,
        .channel = channel,
        .device_address = device_address,
        .ep0_max_packet_size = ep0_max_packet_size,
        .poll = NULL,
        .poll_opaque = NULL,
    };
}

void dm_stm32h7_usb_host_control_set_poll(
    DmStm32H7UsbHostControl *control,
    DmStm32H7UsbHostControlPoll poll, void *opaque)
{
    control->poll = poll;
    control->poll_opaque = opaque;
}

bool dm_stm32h7_usb_host_control_port_reset(
    DmStm32H7UsbHostControl *control)
{
    volatile uint32_t *hprt0;

    if (!dm_stm32h7_usb_host_control_config_valid(control)) {
        return false;
    }
    hprt0 = dm_stm32h7_usb_host_reg(control, OTG_HPRT0);

    *hprt0 = HPRT0_PWR | HPRT0_CONNDET | HPRT0_ENACHG | HPRT0_RST;
    *hprt0 = HPRT0_PWR;
    return (*hprt0 & (HPRT0_PWR | HPRT0_CONNSTS | HPRT0_ENA)) ==
           (HPRT0_PWR | HPRT0_CONNSTS | HPRT0_ENA);
}

DmStm32H7UsbHostControlResult dm_stm32h7_usb_host_control_transfer(
    DmStm32H7UsbHostControl *control, const uint8_t setup[8],
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length)
{
    DmStm32H7UsbHostControlResult result;
    bool direction_in;
    size_t requested_length;
    size_t offset = 0;
    uint32_t pid = HCTSIZ_PID_DATA1;

    if (!actual_length) {
        return DM_STM32H7_USB_HOST_CONTROL_INVALID;
    }
    *actual_length = 0;
    if (!dm_stm32h7_usb_host_control_config_valid(control) || !setup) {
        return DM_STM32H7_USB_HOST_CONTROL_INVALID;
    }
    direction_in = setup[0] & 0x80u;
    requested_length = (size_t)setup[6] | ((size_t)setup[7] << 8);
    if (direction_in) {
        if (out_data || out_length || requested_length > in_capacity ||
            (requested_length && !in_data)) {
            return DM_STM32H7_USB_HOST_CONTROL_INVALID;
        }
    } else if (in_data || in_capacity ||
               requested_length != out_length ||
               (out_length && !out_data)) {
        return DM_STM32H7_USB_HOST_CONTROL_INVALID;
    }

    result = dm_stm32h7_usb_host_stage(control, HCTSIZ_PID_SETUP, false,
                                       setup, NULL, 8, &offset);
    if (result != DM_STM32H7_USB_HOST_CONTROL_OK || offset != 8) {
        return result == DM_STM32H7_USB_HOST_CONTROL_OK ?
               DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR : result;
    }
    offset = 0;
    while (offset < requested_length) {
        size_t packet_length = requested_length - offset;
        size_t packet_actual;

        if (packet_length > control->ep0_max_packet_size) {
            packet_length = control->ep0_max_packet_size;
        }
        result = dm_stm32h7_usb_host_stage(
            control, pid, direction_in,
            direction_in ? NULL : out_data + offset,
            direction_in ? in_data + offset : NULL, packet_length,
            &packet_actual);
        if (result != DM_STM32H7_USB_HOST_CONTROL_OK) {
            return result;
        }
        offset += packet_actual;
        if (!direction_in && packet_actual != packet_length) {
            return DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR;
        }
        if (direction_in && packet_actual != packet_length) {
            break;
        }
        pid = pid == HCTSIZ_PID_DATA1 ? HCTSIZ_PID_DATA0 : HCTSIZ_PID_DATA1;
    }

    result = dm_stm32h7_usb_host_stage(
        control, HCTSIZ_PID_DATA1, !direction_in,
        direction_in ? NULL : (const uint8_t *)"",
        direction_in ? (uint8_t *)"" : NULL, 0, &requested_length);
    if (result != DM_STM32H7_USB_HOST_CONTROL_OK || requested_length) {
        return result == DM_STM32H7_USB_HOST_CONTROL_OK ?
               DM_STM32H7_USB_HOST_CONTROL_TRANSACTION_ERROR : result;
    }
    *actual_length = offset;
    return DM_STM32H7_USB_HOST_CONTROL_OK;
}
