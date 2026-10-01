/* QEMU USBPort lifecycle binding for the reusable STM32H7 host port. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_qemu_port.h"

static DmStm32H7OtgPortSpeed dm_usb_host_qemu_port_speed(
    const USBDevice *device)
{
    switch (device->speed) {
    case USB_SPEED_LOW:
        return DM_STM32H7_OTG_PORT_LOW_SPEED;
    case USB_SPEED_HIGH:
        return DM_STM32H7_OTG_PORT_HIGH_SPEED;
    default:
        return DM_STM32H7_OTG_PORT_FULL_SPEED;
    }
}

static void dm_usb_host_qemu_port_attach(USBPort *port)
{
    DmUsbHostQemuPort *adapter = port->opaque;

    if (adapter->host && !adapter->resetting) {
        dm_stm32h7_otg_host_set_port_connected(
            adapter->host, true, dm_usb_host_qemu_port_speed(port->dev));
    }
}

static void dm_usb_host_qemu_port_detach(USBPort *port)
{
    DmUsbHostQemuPort *adapter = port->opaque;

    if (adapter->host && !adapter->resetting) {
        dm_stm32h7_otg_host_set_port_connected(
            adapter->host, false, DM_STM32H7_OTG_PORT_FULL_SPEED);
    }
}

static USBPortOps dm_usb_host_qemu_port_ops = {
    .attach = dm_usb_host_qemu_port_attach,
    .detach = dm_usb_host_qemu_port_detach,
};

void dm_usb_host_qemu_port_init(DmUsbHostQemuPort *adapter, USBBus *bus,
                                DmStm32H7OtgHost *host, int index)
{
    if (!adapter || !bus || !host) {
        return;
    }
    *adapter = (DmUsbHostQemuPort) {
        .host = host,
        .bus = bus,
    };
    usb_register_port(bus, &adapter->port, adapter, index,
                      &dm_usb_host_qemu_port_ops,
                      USB_SPEED_MASK_LOW | USB_SPEED_MASK_FULL |
                      USB_SPEED_MASK_HIGH);
    adapter->registered = true;
}

void dm_usb_host_qemu_port_cleanup(DmUsbHostQemuPort *adapter)
{
    if (!adapter || !adapter->registered) {
        return;
    }
    usb_unregister_port(adapter->bus, &adapter->port);
    adapter->registered = false;
    adapter->host = NULL;
    adapter->bus = NULL;
}

void dm_usb_host_qemu_port_reset(void *opaque, uint64_t timestamp_ns)
{
    DmUsbHostQemuPort *adapter = opaque;

    (void)timestamp_ns;
    if (!adapter || !adapter->registered || !adapter->port.dev) {
        return;
    }
    adapter->resetting = true;
    usb_port_reset(&adapter->port);
    adapter->resetting = false;
}
