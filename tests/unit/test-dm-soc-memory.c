/* Focused ownership contract for STM32H723-class SoC memory. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_soc.h"
#include "hw/arm/dm_mc02_flash.h"

extern unsigned test_migratable_ram_calls;
extern unsigned test_migratable_rom_device_calls;
extern unsigned test_nonmigratable_ram_calls;
extern unsigned test_nonmigratable_rom_calls;
extern unsigned test_invalid_owner_calls;
extern unsigned test_io_calls;
extern unsigned test_flush_calls;
extern MemoryRegion *test_flush_region;
extern hwaddr test_flush_offset;
extern hwaddr test_flush_size;
void test_soc_memory_stubs_init(void);
void test_soc_memory_stubs_cleanup(void);

static void test_dynamic_regions_are_migratable(void)
{
    DmMc02SocMemory memory;
    MemoryRegion system_memory;
    const DmMc02SocProfile *soc = dm_mc02_soc_stm32h723();

    test_soc_memory_stubs_init();
    dm_mc02_soc_memory_init(&memory, NULL, &system_memory, soc,
                            NULL, NULL, &error_abort);

    /* Flash and every volatile CPU RAM region are dynamic bytes.  The
     * reusable SoC layer registers them as QEMU global RAM blocks. */
    g_assert_cmpuint(test_migratable_ram_calls, ==, 6);
    g_assert_cmpuint(test_nonmigratable_ram_calls, ==, 0);
    g_assert_cmpuint(test_nonmigratable_rom_calls, ==, 1);
    g_assert_cmpuint(test_invalid_owner_calls, ==, 0);

    test_soc_memory_stubs_cleanup();
}

static void test_flash_romd_keeps_one_byte_owner_and_flushes_commits(void)
{
    DmMc02SocMemory memory;
    DmMc02Flash flash;
    MemoryRegion system_memory;
    const DmMc02SocProfile *soc = dm_mc02_soc_stm32h723();
    const MemoryRegionOps *ops = dm_mc02_flash_storage_ops();
    const uint32_t address = 0x20000;
    uint8_t *storage;

    test_soc_memory_stubs_init();
    dm_mc02_soc_memory_init(&memory, NULL, &system_memory, soc,
                            ops, &flash, &error_abort);
    storage = memory_region_get_ram_ptr(&memory.flash);
    dm_mc02_flash_init(&flash, NULL, storage, soc->flash_size, &memory.flash);
    g_assert_cmpuint(test_migratable_ram_calls, ==, 6);
    g_assert_cmpuint(test_migratable_rom_device_calls, ==, 1);
    g_assert_cmpuint(test_invalid_owner_calls, ==, 0);
    g_assert_cmpuint(test_io_calls, ==, 1); /* Registers only, no overlay. */
    g_assert_true(memory_region_is_romd(&memory.flash));
    g_assert_true(memory.flash.ops == ops);
    g_assert_true(memory.flash.opaque == &flash);
    g_assert_true(flash.storage == storage);
    g_assert_true(flash.storage_region == &memory.flash);

    ops->write(&flash, address, 0x12345678, 4);
    g_assert_cmphex((uint32_t)ldl_le_p(storage + address), ==, UINT32_MAX);
    flash.iomem.ops->write(&flash, 0x04, 0x45670123, 4);
    flash.iomem.ops->write(&flash, 0x04, 0xcdef89ab, 4);
    ops->write(&flash, address, 0x12345678, 4);
    g_assert_cmphex((uint32_t)ldl_le_p(storage + address), ==, UINT32_MAX);
    flash.iomem.ops->write(&flash, 0x0c, 1u << 1, 4);
    g_assert_true(memory_region_is_romd(&memory.flash));
    for (unsigned i = 0; i < 7; ++i) {
        ops->write(&flash, address + 4 * i, 0x12345678 + i, 4);
    }
    g_assert_cmphex((uint32_t)ldl_le_p(storage + address), ==, UINT32_MAX);
    g_assert_cmpuint(test_flush_calls, ==, 0);
    ops->write(&flash, address + 28, 0x1234567f, 4);
    for (unsigned i = 0; i < 8; ++i) {
        g_assert_cmphex((uint32_t)ldl_le_p(storage + address + 4 * i), ==, 0x12345678 + i);
    }
    g_assert_cmpuint(test_flush_calls, ==, 1);
    g_assert_true(test_flush_region == &memory.flash);
    g_assert_cmphex(test_flush_offset, ==, address);
    g_assert_cmpuint(test_flush_size, ==, 32);

    flash.iomem.ops->write(&flash, 0x0c, (1u << 2) | (1u << 8) | (1u << 7), 4);
    g_assert_cmphex((uint32_t)ldl_le_p(storage + address), ==, UINT32_MAX);
    g_assert_cmpuint(test_flush_calls, ==, 2);
    g_assert_cmphex(test_flush_offset, ==, address);
    g_assert_cmpuint(test_flush_size, ==, 128 * 1024);
    g_assert_true(memory_region_is_romd(&memory.flash));
    test_soc_memory_stubs_cleanup();
    g_free(memory.fdcan_msg_ram.data);
}

static void test_profile_map_remains_valid(void)
{
    const DmMc02SocProfile *soc = dm_mc02_soc_stm32h723();

    /* STM32H723 CMSIS: CORDIC_BASE = D2_AHB2PERIPH_BASE + 0x4400. */
    g_assert_cmphex(soc->cordic_base, ==, 0x48024400);
    g_assert_true(dm_mc02_soc_validate_map(soc));
}

static void test_dma_sram_ranges(void)
{
    /* RM0468 Rev 3, section 2.1.6: DMA1/2 can reach AXI and AHB SRAM,
     * not the CPU's TCM. Sizes are the H723 profile's modeled SRAM geometry. */
    static const struct {
        hwaddr address;
        size_t bytes;
        bool valid;
    } cases[] = {
        { 0x2400cde8, 144, true },
        { 0x2404ffd0, 48, true },
        { 0x2404ffd0, 49, false },
        { 0x30007fd0, 48, true },
        { 0x30007fd0, 49, false },
        { 0x38003fd0, 48, true },
        { 0x38003fd0, 49, false },
        { 0x20000100, 144, false },
        { 0x00000100, 144, false },
        { UINT64_MAX - 10, 48, false },
        { 0x24000000, 0, false },
    };

    for (unsigned i = 0; i < ARRAY_SIZE(cases); ++i) {
        g_assert_cmpint(dm_mc02_soc_dma_sram_range_valid(
                        dm_mc02_soc_stm32h723(), cases[i].address,
                        cases[i].bytes), ==, cases[i].valid);
    }
    g_assert_false(dm_mc02_soc_dma_sram_range_valid(NULL, 0x24000000, 48));
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-soc-memory/dma-sram-ranges", test_dma_sram_ranges);
    g_test_add_func("/dm-soc-memory/dynamic-regions-migratable",
                    test_dynamic_regions_are_migratable);
    g_test_add_func("/dm-soc-memory/profile-map-valid",
                    test_profile_map_remains_valid);
    g_test_add_func("/dm-soc-memory/flash-romd-owner-and-flush",
                    test_flash_romd_keeps_one_byte_owner_and_flushes_commits);
    return g_test_run();
}
