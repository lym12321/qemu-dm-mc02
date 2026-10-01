/* Board-independent owner for controller message RAM backed by QEMU RAM. */
#ifndef HW_MISC_DM_MESSAGE_RAM_H
#define HW_MISC_DM_MESSAGE_RAM_H

#include "exec/memory.h"
#include "qapi/error.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct DmMessageRam {
    MemoryRegion region;
    uint8_t *data;
    uint64_t size;
} DmMessageRam;

/* The owner is a DeviceState because QEMU's RAM migration registration uses
 * device ownership.  NULL is valid and registers a global RAM block. */
bool dm_message_ram_init(DmMessageRam *ram, DeviceState *owner,
                         const char *name, uint64_t size, Error **errp);
void dm_message_ram_reset(DmMessageRam *ram);
uint8_t *dm_message_ram_data(DmMessageRam *ram);
const uint8_t *dm_message_ram_const_data(const DmMessageRam *ram);
uint64_t dm_message_ram_size(const DmMessageRam *ram);
MemoryRegion *dm_message_ram_region(DmMessageRam *ram);

#endif
