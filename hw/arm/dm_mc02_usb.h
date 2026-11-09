#ifndef HW_ARM_DM_MC02_USB_H
#define HW_ARM_DM_MC02_USB_H
#include "exec/memory.h"
#include "hw/usb/dm_usb_bridge.h"
#include "hw/irq.h"
#define DM_MC02_USB_REGION_SIZE 0x20000

typedef struct DmMc02Usb {
    MemoryRegion iomem;
    DmUsbDwc2Device dwc2;
    DmUsbBridge *bridge;
    qemu_irq irq;
} DmMc02Usb;
bool dm_mc02_usb_init(DmMc02Usb *s, Object *owner, Chardev *chr, Error **errp);
void dm_mc02_usb_reset(DmMc02Usb *s);
void dm_mc02_usb_cleanup(DmMc02Usb *s);
void dm_mc02_usb_set_irq(DmMc02Usb *s, qemu_irq irq);
#endif
