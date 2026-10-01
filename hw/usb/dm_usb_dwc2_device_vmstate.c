/* Component-only VMState contract for the board-independent DWC2 device
 * controller. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_dwc2_device.h"
#include "hw/usb/dm_usb_vmstate.h"
#include "migration/qemu-file.h"
#include "migration/vmstate.h"

static int get_dwc2_pid(QEMUFile *f, void *pv, size_t size,
                        const VMStateField *field)
{
    uint32_t value;

    (void)field;
    (void)size;
    value = qemu_get_be32(f);
    if (value != DM_USB_TRANSACTION_PID_DATA0 &&
        value != DM_USB_TRANSACTION_PID_DATA1) {
        return -EINVAL;
    }
    *(DmUsbTransactionPid *)pv = (DmUsbTransactionPid)value;
    return 0;
}

static int put_dwc2_pid(QEMUFile *f, void *pv, size_t size,
                        const VMStateField *field, JSONWriter *vmdesc)
{
    DmUsbTransactionPid *pid = pv;

    (void)field;
    (void)vmdesc;
    (void)size;
    qemu_put_be32(f, *pid);
    return 0;
}

static const VMStateInfo vmstate_info_dwc2_pid = {
    .name = "dwc2-transaction-pid",
    .get = get_dwc2_pid,
    .put = put_dwc2_pid,
};

static int get_dwc2_endpoint_type(QEMUFile *f, void *pv, size_t size,
                                  const VMStateField *field)
{
    uint32_t value;

    (void)field;
    (void)size;
    value = qemu_get_be32(f);
    if (value > DM_USB_ENDPOINT_INTERRUPT) {
        return -EINVAL;
    }
    *(DmUsbEndpointType *)pv = (DmUsbEndpointType)value;
    return 0;
}

static int put_dwc2_endpoint_type(QEMUFile *f, void *pv, size_t size,
                                  const VMStateField *field,
                                  JSONWriter *vmdesc)
{
    DmUsbEndpointType *type = pv;

    (void)field;
    (void)vmdesc;
    (void)size;
    qemu_put_be32(f, *type);
    return 0;
}

static const VMStateInfo vmstate_info_dwc2_endpoint_type = {
    .name = "dwc2-endpoint-type",
    .get = get_dwc2_endpoint_type,
    .put = put_dwc2_endpoint_type,
};

#define VMSTATE_DWC2_SIZE_T(_field, _state) \
    VMSTATE_SINGLE(_field, _state, 0, dm_usb_vmstate_info_size_t, size_t)

#define VMSTATE_DWC2_PID(_field, _state) \
    VMSTATE_SINGLE(_field, _state, 0, vmstate_info_dwc2_pid, \
                   DmUsbTransactionPid)

#define VMSTATE_DWC2_ENDPOINT_TYPE(_field, _state) \
    VMSTATE_SINGLE(_field, _state, 0, vmstate_info_dwc2_endpoint_type, \
                   DmUsbEndpointType)

static const VMStateDescription vmstate_dm_usb_dwc2_endpoint = {
    .name = "dm-usb-dwc2-endpoint",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32(in_ctl, DmUsbDwc2Endpoint),
        VMSTATE_UINT32(out_ctl, DmUsbDwc2Endpoint),
        VMSTATE_UINT32(in_int, DmUsbDwc2Endpoint),
        VMSTATE_UINT32(out_int, DmUsbDwc2Endpoint),
        VMSTATE_UINT32(in_tsiz, DmUsbDwc2Endpoint),
        VMSTATE_UINT32(out_tsiz, DmUsbDwc2Endpoint),
        VMSTATE_UINT32(in_dma, DmUsbDwc2Endpoint),
        VMSTATE_UINT32(out_dma, DmUsbDwc2Endpoint),
        VMSTATE_UINT8_ARRAY(in_fifo, DmUsbDwc2Endpoint,
                            DM_USB_DWC2_FIFO_BYTES),
        VMSTATE_UINT8_ARRAY(out_fifo, DmUsbDwc2Endpoint,
                            DM_USB_DWC2_FIFO_BYTES),
        VMSTATE_DWC2_SIZE_T(in_fifo_head, DmUsbDwc2Endpoint),
        VMSTATE_DWC2_SIZE_T(in_fifo_count, DmUsbDwc2Endpoint),
        VMSTATE_DWC2_SIZE_T(out_fifo_head, DmUsbDwc2Endpoint),
        VMSTATE_DWC2_SIZE_T(out_fifo_count, DmUsbDwc2Endpoint),
        VMSTATE_END_OF_LIST()
    },
};

static const VMStateDescription vmstate_dm_usb_transaction_endpoint = {
    .name = "dm-usb-transaction-endpoint",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_BOOL(in_enabled, DmUsbTransactionEndpoint),
        VMSTATE_BOOL(out_enabled, DmUsbTransactionEndpoint),
        VMSTATE_BOOL(in_halted, DmUsbTransactionEndpoint),
        VMSTATE_BOOL(out_halted, DmUsbTransactionEndpoint),
        VMSTATE_DWC2_ENDPOINT_TYPE(type, DmUsbTransactionEndpoint),
        VMSTATE_UINT16(max_packet_size, DmUsbTransactionEndpoint),
        VMSTATE_DWC2_PID(next_in_pid, DmUsbTransactionEndpoint),
        VMSTATE_DWC2_PID(next_out_pid, DmUsbTransactionEndpoint),
        VMSTATE_END_OF_LIST()
    },
};

static bool dwc2_fifo_state_valid(const DmUsbDwc2Endpoint *endpoint)
{
    return endpoint->in_fifo_head < DM_USB_DWC2_FIFO_BYTES &&
           endpoint->in_fifo_count <= DM_USB_DWC2_FIFO_BYTES &&
           endpoint->out_fifo_head < DM_USB_DWC2_FIFO_BYTES &&
           endpoint->out_fifo_count <= DM_USB_DWC2_FIFO_BYTES;
}

static bool dwc2_transaction_endpoint_valid(
    const DmUsbTransactionEndpoint *endpoint)
{
    return endpoint->type <= DM_USB_ENDPOINT_INTERRUPT &&
           endpoint->max_packet_size <= DM_USB_DWC2_DXEPCTL_MPS_MASK &&
           (endpoint->next_in_pid == DM_USB_TRANSACTION_PID_DATA0 ||
            endpoint->next_in_pid == DM_USB_TRANSACTION_PID_DATA1) &&
           (endpoint->next_out_pid == DM_USB_TRANSACTION_PID_DATA0 ||
            endpoint->next_out_pid == DM_USB_TRANSACTION_PID_DATA1);
}

bool dm_usb_dwc2_state_valid(const DmUsbDwc2Device *state)
{
    if (!state || !state->ep0_max_packet_size ||
        state->ep0_max_packet_size > DM_USB_DWC2_DXEPCTL_MPS_MASK ||
        !state->transaction.endpoint[0].in_enabled ||
        !state->transaction.endpoint[0].out_enabled ||
        state->transaction.endpoint[0].type != DM_USB_ENDPOINT_CONTROL ||
        state->transaction.endpoint[0].max_packet_size !=
            state->ep0_max_packet_size) {
        return false;
    }

    for (unsigned ep = 0; ep < DM_USB_DWC2_MAX_ENDPOINTS; ++ep) {
        if (!dwc2_fifo_state_valid(&state->endpoint[ep]) ||
            !dwc2_transaction_endpoint_valid(&state->transaction.endpoint[ep])) {
            return false;
        }
    }

    return true;
}

static int dm_usb_dwc2_post_load(void *opaque, int version_id)
{
    DmUsbDwc2Device *state = opaque;

    if (version_id != 1 || !dm_usb_dwc2_state_valid(state)) {
        return -EINVAL;
    }

    /* Callback pointers, control ownership and IRQ output are destination
     * wiring.  Only rebind/reproject after every serialized field is valid. */
    dm_usb_dwc2_sync_runtime(state);
    return 0;
}

static const VMStateField vmstate_dm_usb_dwc2_fields[] = {
    VMSTATE_UINT16_EQUAL(ep0_max_packet_size, DmUsbDwc2Device, NULL),
    VMSTATE_UINT32(gahbcfg, DmUsbDwc2Device),
    VMSTATE_UINT32(grstctl, DmUsbDwc2Device),
    VMSTATE_UINT32(gintsts, DmUsbDwc2Device),
    VMSTATE_UINT32(gintmsk, DmUsbDwc2Device),
    VMSTATE_UINT32(grxfsiz, DmUsbDwc2Device),
    VMSTATE_UINT32(gnptxfsiz, DmUsbDwc2Device),
    VMSTATE_UINT32_ARRAY(dieptxf, DmUsbDwc2Device,
                         DM_USB_DWC2_DIEPTXF_COUNT),
    VMSTATE_UINT32(dcfg, DmUsbDwc2Device),
    VMSTATE_UINT32(dctl, DmUsbDwc2Device),
    VMSTATE_UINT32(dsts, DmUsbDwc2Device),
    VMSTATE_UINT32(diepmsk, DmUsbDwc2Device),
    VMSTATE_UINT32(doepmsk, DmUsbDwc2Device),
    VMSTATE_UINT32(daintmsk, DmUsbDwc2Device),
    VMSTATE_UINT32(diepempmsk, DmUsbDwc2Device),
    VMSTATE_UINT64(fifo_overflow, DmUsbDwc2Device),
    VMSTATE_STRUCT_ARRAY(endpoint, DmUsbDwc2Device,
                         DM_USB_DWC2_MAX_ENDPOINTS, 0,
                         vmstate_dm_usb_dwc2_endpoint,
                         DmUsbDwc2Endpoint),
    VMSTATE_STRUCT_ARRAY(transaction.endpoint, DmUsbDwc2Device,
                         DM_USB_DWC2_MAX_ENDPOINTS, 0,
                         vmstate_dm_usb_transaction_endpoint,
                         DmUsbTransactionEndpoint),
    VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_usb_dwc2_raw = {
    .name = "dm-usb-dwc2-device-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_usb_dwc2_fields,
};

static const VMStateDescription vmstate_dm_usb_dwc2 = {
    .name = "dm-usb-dwc2-device",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_usb_dwc2_post_load,
    .fields = vmstate_dm_usb_dwc2_fields,
};

const VMStateDescription *dm_usb_dwc2_vmstate(void)
{
    return &vmstate_dm_usb_dwc2;
}

const VMStateDescription *dm_usb_dwc2_vmstate_raw(void)
{
    return &vmstate_dm_usb_dwc2_raw;
}
