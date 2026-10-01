/* Narrow MemoryRegion and Message-RAM stubs for the SoC memory ownership
 * contract test.  The real QEMU machine is covered by the direct qtest. */
#include "qemu/osdep.h"
#include "exec/memory.h"
#include "hw/misc/dm_message_ram.h"

typedef struct TestRamAllocation {
    MemoryRegion *region;
    uint8_t *data;
} TestRamAllocation;

static GHashTable *test_ram_allocations;
unsigned test_migratable_ram_calls;
unsigned test_nonmigratable_ram_calls;
unsigned test_nonmigratable_rom_calls;
unsigned test_invalid_owner_calls;

void test_soc_memory_stubs_init(void);
void test_soc_memory_stubs_cleanup(void);

static void test_record_region(MemoryRegion *mr, const char *name,
                               uint64_t size, bool migratable,
                               bool count_as_ram)
{
    TestRamAllocation *allocation = g_new0(TestRamAllocation, 1);

    memset(mr, 0, sizeof(*mr));
    mr->name = g_strdup(name);
    mr->size = int128_make64(size);
    allocation->region = mr;
    allocation->data = g_malloc0(size);
    g_hash_table_insert(test_ram_allocations, mr, allocation);
    if (migratable) {
        test_migratable_ram_calls++;
    } else if (count_as_ram) {
        test_nonmigratable_ram_calls++;
    }
}

void memory_region_init_ram(MemoryRegion *mr, Object *owner,
                            const char *name, uint64_t size, Error **errp)
{
    (void)errp;
    if (owner) {
        test_invalid_owner_calls++;
    }
    test_record_region(mr, name, size, true, true);
}

void memory_region_init_ram_nomigrate(MemoryRegion *mr, Object *owner,
                                      const char *name, uint64_t size,
                                      Error **errp)
{
    (void)errp;
    if (owner) {
        test_invalid_owner_calls++;
    }
    test_record_region(mr, name, size, false, true);
}

void memory_region_init_rom_nomigrate(MemoryRegion *mr, Object *owner,
                                      const char *name, uint64_t size,
                                      Error **errp)
{
    (void)errp;
    if (owner) {
        test_invalid_owner_calls++;
    }
    test_record_region(mr, name, size, false, false);
    test_nonmigratable_rom_calls++;
}

void *memory_region_get_ram_ptr(MemoryRegion *mr)
{
    TestRamAllocation *allocation =
        g_hash_table_lookup(test_ram_allocations, mr);

    g_assert_nonnull(allocation);
    return allocation->data;
}

void memory_region_add_subregion(MemoryRegion *mr, hwaddr offset,
                                 MemoryRegion *subregion)
{
    (void)mr;
    (void)offset;
    (void)subregion;
}

bool dm_message_ram_init(DmMessageRam *ram, DeviceState *owner,
                         const char *name, uint64_t size, Error **errp)
{
    (void)owner;
    (void)name;
    (void)errp;
    memset(ram, 0, sizeof(*ram));
    ram->size = size;
    ram->data = g_malloc0(size);
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

void test_soc_memory_stubs_init(void)
{
    test_ram_allocations = g_hash_table_new_full(g_direct_hash,
                                                 g_direct_equal,
                                                 NULL,
                                                 (GDestroyNotify)g_free);
    test_migratable_ram_calls = 0;
    test_nonmigratable_ram_calls = 0;
    test_nonmigratable_rom_calls = 0;
    test_invalid_owner_calls = 0;
}

void test_soc_memory_stubs_cleanup(void)
{
    GHashTableIter iter;
    gpointer key, value;

    g_hash_table_iter_init(&iter, test_ram_allocations);
    while (g_hash_table_iter_next(&iter, &key, &value)) {
        TestRamAllocation *allocation = value;
        g_free(allocation->data);
        g_free((char *)allocation->region->name);
    }
    g_hash_table_unref(test_ram_allocations);
    test_ram_allocations = NULL;
}
