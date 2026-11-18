/* Minimal host and MemoryRegion symbols for the isolated PWR/RCC test. */
#include "qemu/osdep.h"
#include "exec/memory.h"

#ifdef _WIN32
/* The Windows ftruncate wrapper lives in the block driver, which this
 * isolated test does not link. Keep the fixture's real file truncation. */
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
    mr->ops = ops;
    mr->opaque = opaque;
    mr->name = name;
    mr->size = int128_make64(size);
}
