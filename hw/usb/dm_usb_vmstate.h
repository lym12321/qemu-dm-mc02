/* Shared VMState encodings for board-independent USB components. */
#ifndef DM_USB_VMSTATE_H
#define DM_USB_VMSTATE_H

#include "migration/vmstate.h"

/* A host-size in-memory cursor with a fixed 64-bit big-endian wire format. */
extern const VMStateInfo dm_usb_vmstate_info_size_t;

#endif /* DM_USB_VMSTATE_H */
