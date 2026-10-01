/* QEMU USBPort lifecycle binding for the reusable STM32H7 host port. */
#ifndef HW_USB_DM_USB_HOST_QEMU_PORT_H
#define HW_USB_DM_USB_HOST_QEMU_PORT_H

#include "hw/usb.h"
#include "hw/usb/dm_stm32h7_otg_host.h"

#include <stdbool.h>

typedef struct DmUsbHostQemuPort {
    DmStm32H7OtgHost *host;
    USBBus *bus;
    USBPort port;
    bool resetting;
    bool registered;
} DmUsbHostQemuPort;

void dm_usb_host_qemu_port_init(DmUsbHostQemuPort *adapter, USBBus *bus,
                                DmStm32H7OtgHost *host, int index);
void dm_usb_host_qemu_port_cleanup(DmUsbHostQemuPort *adapter);
void dm_usb_host_qemu_port_reset(void *opaque, uint64_t timestamp_ns);

#endif /* HW_USB_DM_USB_HOST_QEMU_PORT_H */
