/* Minimal system-memory symbols for the DMA component unit tests. */
#include "qemu/osdep.h"
#include "exec/memory.h"

AddressSpace address_space_memory;
MemTxResult (*dm_test_dma_memory_rw)(AddressSpace *as, hwaddr addr,
                                     MemTxAttrs attrs, void *buf,
                                     hwaddr len, bool is_write);

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
    if (dm_test_dma_memory_rw) {
        return dm_test_dma_memory_rw(as, addr, attrs, buf, len, is_write);
    }
    (void)as;
    (void)addr;
    (void)attrs;
    (void)buf;
    (void)len;
    (void)is_write;
    return MEMTX_ERROR;
}

/* Keep component tests on the injected memory callback when the device uses
 * address_space_read()'s inline path. Real RAM translation is covered by the
 * machine qtests rather than by these register/VMState fixtures. */
MemoryRegion *flatview_translate(FlatView *fv, hwaddr addr, hwaddr *xlat,
                                 hwaddr *len, bool is_write, MemTxAttrs attrs)
{
    static MemoryRegion io_region;

    *xlat = addr;
    return &io_region;
}

MemTxResult flatview_read_continue(FlatView *fv, hwaddr addr, MemTxAttrs attrs,
                                   void *buf, hwaddr len, hwaddr addr1,
                                   hwaddr l, MemoryRegion *mr)
{
    return address_space_read_full(&address_space_memory, addr, attrs,
                                   buf, len);
}

MemTxResult address_space_read_full(AddressSpace *as, hwaddr addr,
                                    MemTxAttrs attrs, void *buf, hwaddr len)
{
    return address_space_rw(as, addr, attrs, buf, len, false);
}

void *qemu_map_ram_ptr(RAMBlock *ram_block, ram_addr_t addr)
{
    g_assert_not_reached();
}

bool memory_region_is_ram_device(MemoryRegion *mr)
{
    return mr->ram_device;
}
