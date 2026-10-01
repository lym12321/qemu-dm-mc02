/* Narrow MemoryRegion boundary for the GPIO/SYSCFG/EXTI composition test. */
#include "qemu/osdep.h"
#include "exec/memory.h"

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    (void)mr;
    (void)owner;
    (void)ops;
    (void)opaque;
    (void)name;
    (void)size;
}
