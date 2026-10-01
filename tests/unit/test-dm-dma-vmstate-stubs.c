/* Minimal system-memory symbols for the DMA component VMState unit test. */
#include "qemu/osdep.h"
#include "exec/memory.h"

AddressSpace address_space_memory;

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

MemTxResult address_space_rw(AddressSpace *as, hwaddr addr, MemTxAttrs attrs,
                             void *buf, hwaddr len, bool is_write)
{
    (void)as;
    (void)addr;
    (void)attrs;
    (void)buf;
    (void)len;
    (void)is_write;
    return MEMTX_ERROR;
}
