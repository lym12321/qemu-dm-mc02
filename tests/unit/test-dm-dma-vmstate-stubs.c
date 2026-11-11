/* Minimal system-memory symbols for the DMA component unit tests. */
#include "qemu/osdep.h"
#include "exec/memory.h"

AddressSpace address_space_memory;

#ifdef _WIN32
/* VMState fixtures use ftruncate, whose QEMU Windows wrapper normally lives
 * in the block driver rather than the libraries linked by these unit tests. */
int qemu_ftruncate64(int fd, int64_t length)
{
    int error = _chsize_s(fd, length);

    if (error) {
        errno = error;
        return -1;
    }
    return 0;
}
#endif

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    (void)owner;
    (void)name;
    (void)size;
    /* Retain the real device callbacks for register-level contract tests. */
    mr->ops = ops;
    mr->opaque = opaque;
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
