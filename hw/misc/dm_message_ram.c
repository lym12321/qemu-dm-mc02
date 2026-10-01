/* Board-independent owner for controller message RAM backed by QEMU RAM. */
#include "qemu/osdep.h"
#include "hw/misc/dm_message_ram.h"

bool dm_message_ram_init(DmMessageRam *ram, DeviceState *owner,
                         const char *name, uint64_t size, Error **errp)
{
    Error *local_err = NULL;

    if (!ram || !name || !size) {
        error_setg(errp, "message RAM requires a name and non-zero size");
        return false;
    }

    memset(ram, 0, sizeof(*ram));
    memory_region_init_ram(&ram->region, owner ? OBJECT(owner) : NULL,
                           name, size, &local_err);
    if (local_err) {
        error_propagate(errp, local_err);
        return false;
    }
    ram->data = memory_region_get_ram_ptr(&ram->region);
    ram->size = size;
    return true;
}

void dm_message_ram_reset(DmMessageRam *ram)
{
    if (ram && ram->data) {
        memset(ram->data, 0, ram->size);
    }
}

uint8_t *dm_message_ram_data(DmMessageRam *ram)
{
    return ram ? ram->data : NULL;
}

const uint8_t *dm_message_ram_const_data(const DmMessageRam *ram)
{
    return ram ? ram->data : NULL;
}

uint64_t dm_message_ram_size(const DmMessageRam *ram)
{
    return ram ? ram->size : 0;
}

MemoryRegion *dm_message_ram_region(DmMessageRam *ram)
{
    return ram ? &ram->region : NULL;
}
