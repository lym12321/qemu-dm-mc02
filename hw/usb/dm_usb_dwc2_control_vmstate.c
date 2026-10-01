/* Composite VMState for the reusable USB control/DWC2 device link. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_dwc2_control.h"

void dm_usb_dwc2_control_link_init(
    DmUsbDwc2ControlLink *link, const DmUsbControlOps *control_ops,
    void *control_opaque, DmUsbDwc2Irq *irq, void *irq_opaque,
    uint16_t ep0_max_packet_size)
{
    if (!link) {
        return;
    }
    link->control_marker = DM_USB_DWC2_CONTROL_LINK_CONTROL_MARKER;
    link->dwc2_marker = DM_USB_DWC2_CONTROL_LINK_DWC2_MARKER;
    dm_usb_control_init(&link->control, control_ops, control_opaque,
                        ep0_max_packet_size);
    dm_usb_dwc2_init(&link->dwc2, &link->control, irq, irq_opaque,
                     ep0_max_packet_size);
}

static int dm_usb_dwc2_control_link_post_load(void *opaque, int version_id)
{
    DmUsbDwc2ControlLink *state = opaque;

    if (!state || version_id != 1 ||
        state->control_marker != DM_USB_DWC2_CONTROL_LINK_CONTROL_MARKER ||
        state->dwc2_marker != DM_USB_DWC2_CONTROL_LINK_DWC2_MARKER ||
        !dm_usb_control_state_valid(&state->control) ||
        !dm_usb_dwc2_state_valid(&state->dwc2) ||
        state->dwc2.control != &state->control ||
        state->control.max_packet_size != state->dwc2.ep0_max_packet_size) {
        return -EINVAL;
    }

    /* Both raw children are now valid.  This is the only point at which the
     * DWC2 transaction callback graph and derived IRQ level are rebuilt. */
    dm_usb_dwc2_sync_runtime(&state->dwc2);
    return 0;
}

static const VMStateDescription vmstate_dm_usb_dwc2_control_link = {
    .name = "dm-usb-dwc2-control-link",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_usb_dwc2_control_link_post_load,
    .fields = (VMStateField[]) {
        /* Control is the producer of EP0 phase state.  DWC2 raw state is
         * loaded second, but its callback/IRQ sync is parent-owned. */
        VMSTATE_UINT32_EQUAL(control_marker, DmUsbDwc2ControlLink, NULL),
        VMSTATE_STRUCT(control, DmUsbDwc2ControlLink, 0,
                       vmstate_dm_usb_control_raw,
                       DmUsbControlDevice),
        VMSTATE_UINT32_EQUAL(dwc2_marker, DmUsbDwc2ControlLink, NULL),
        VMSTATE_STRUCT(dwc2, DmUsbDwc2ControlLink, 0,
                       vmstate_dm_usb_dwc2_raw,
                       DmUsbDwc2Device),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_usb_dwc2_control_link_vmstate(void)
{
    return &vmstate_dm_usb_dwc2_control_link;
}
