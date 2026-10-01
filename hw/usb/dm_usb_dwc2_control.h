/* Reusable composition of a USB control core and DWC2 device-mode state. */
#ifndef DM_USB_DWC2_CONTROL_H
#define DM_USB_DWC2_CONTROL_H

#include "hw/usb/dm_usb_control.h"
#include "hw/usb/dm_usb_dwc2_device.h"
#include "migration/vmstate.h"

#define DM_USB_DWC2_CONTROL_LINK_CONTROL_MARKER UINT32_C(0x444d4354)
#define DM_USB_DWC2_CONTROL_LINK_DWC2_MARKER    UINT32_C(0x444d4457)

/* The embedded layout makes the control pointer a destination-owned,
 * verifiable relationship instead of a serialized address. */
typedef struct DmUsbDwc2ControlLink {
    /* Wire markers make the positional child order self-checking. */
    uint32_t control_marker;
    DmUsbControlDevice control;
    uint32_t dwc2_marker;
    DmUsbDwc2Device dwc2;
} DmUsbDwc2ControlLink;

void dm_usb_dwc2_control_link_init(
    DmUsbDwc2ControlLink *link, const DmUsbControlOps *control_ops,
    void *control_opaque, DmUsbDwc2Irq *irq, void *irq_opaque,
    uint16_t ep0_max_packet_size);

/* Composite component contract.  It is not a machine migration entry. */
const VMStateDescription *dm_usb_dwc2_control_link_vmstate(void);

#endif /* DM_USB_DWC2_CONTROL_H */
