/* Minimal MemoryRegion symbol for the isolated PWR/RCC VMState test. */
#include "qemu/osdep.h"
#include "exec/memory.h"

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    (void)owner;
    mr->ops = ops;
    mr->opaque = opaque;
    mr->name = name;
    mr->size = int128_make64(size);
}
