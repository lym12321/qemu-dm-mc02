/* Board-neutral, bounded host-request bridge for an executing USB device. */
#ifndef DM_USB_BRIDGE_H
#define DM_USB_BRIDGE_H
#include "chardev/char.h"
#include "hw/usb/dm_usb_dwc2_device.h"
#include "qapi/error.h"
typedef struct DmUsbBridge DmUsbBridge;
DmUsbBridge *dm_usb_bridge_new(DmUsbDwc2Device *device, unsigned endpoint_count, Chardev *chr,
                                Error **errp);
void dm_usb_bridge_reset(DmUsbBridge *bridge);
void dm_usb_bridge_free(DmUsbBridge *bridge);
#endif
