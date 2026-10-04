/* Narrow MemoryRegion boundary for the GPIO/SYSCFG/EXTI composition test. */
#include "qemu/osdep.h"
#include "exec/memory.h"

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    mr->ops = ops;
    mr->opaque = opaque;
    (void)owner;
    (void)name;
    (void)size;
}
