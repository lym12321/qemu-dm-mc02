/* Minimal MemoryRegion symbols for the isolated message-RAM owner test.
 * The board qtest covers the real QEMU RAM implementation. */
#include "qemu/osdep.h"
#include "exec/memory.h"

static MemoryRegion *test_ram_region;
static void *test_ram_data;

void memory_region_init_ram(MemoryRegion *mr, Object *owner,
                            const char *name, uint64_t size, Error **errp)
{
    (void)owner;
    (void)name;

    memset(mr, 0, sizeof(*mr));
    test_ram_region = mr;
    test_ram_data = g_malloc0(size);
    (void)errp;
}

void *memory_region_get_ram_ptr(MemoryRegion *mr)
{
    g_assert_true(mr == test_ram_region);
    return test_ram_data;
}
