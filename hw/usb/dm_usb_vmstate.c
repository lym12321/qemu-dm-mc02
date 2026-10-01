/* Shared VMState encodings for board-independent USB components. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_vmstate.h"
#include "migration/qemu-file.h"

static int get_dm_usb_size_t(QEMUFile *f, void *pv, size_t size,
                             const VMStateField *field)
{
    uint64_t value;

    (void)size;
    (void)field;
    value = qemu_get_be64(f);
    if (value > SIZE_MAX) {
        return -EINVAL;
    }
    *(size_t *)pv = (size_t)value;
    return 0;
}

static int put_dm_usb_size_t(QEMUFile *f, void *pv, size_t size,
                             const VMStateField *field,
                             JSONWriter *vmdesc)
{
    (void)size;
    (void)field;
    (void)vmdesc;
    qemu_put_be64(f, *(const size_t *)pv);
    return 0;
}

const VMStateInfo dm_usb_vmstate_info_size_t = {
    .name = "dm-usb-size-t",
    .get = get_dm_usb_size_t,
    .put = put_dm_usb_size_t,
};
