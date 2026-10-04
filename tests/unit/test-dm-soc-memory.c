/* Focused ownership contract for STM32H723-class SoC memory. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_soc.h"

extern unsigned test_migratable_ram_calls;
extern unsigned test_nonmigratable_ram_calls;
extern unsigned test_nonmigratable_rom_calls;
extern unsigned test_invalid_owner_calls;
void test_soc_memory_stubs_init(void);
void test_soc_memory_stubs_cleanup(void);

static void test_dynamic_regions_are_migratable(void)
{
    DmMc02SocMemory memory;
    MemoryRegion system_memory;
    const DmMc02SocProfile *soc = dm_mc02_soc_stm32h723();

    test_soc_memory_stubs_init();
    dm_mc02_soc_memory_init(&memory, NULL, &system_memory, soc,
                            &error_abort);

    /* Flash and every volatile CPU RAM region are dynamic bytes.  The
     * reusable SoC layer registers them as QEMU global RAM blocks. */
    g_assert_cmpuint(test_migratable_ram_calls, ==, 6);
    g_assert_cmpuint(test_nonmigratable_ram_calls, ==, 0);
    g_assert_cmpuint(test_nonmigratable_rom_calls, ==, 1);
    g_assert_cmpuint(test_invalid_owner_calls, ==, 0);

    test_soc_memory_stubs_cleanup();
}

static void test_profile_map_remains_valid(void)
{
    g_assert_true(dm_mc02_soc_validate_map(dm_mc02_soc_stm32h723()));
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
    return g_test_run();
}
