/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"

#define FDCAN_MSG_RAM_BASE 0x4000ac00ull
#define FDCAN_MSG_RAM_SIZE 0x2800ull
#define FLASH_BASE         0x08000000ull
#define ITCM_BASE          0x00000000ull
#define DTCM_BASE          0x20000000ull
#define AXI_SRAM_BASE      0x24000000ull
#define D2_SRAM_BASE       0x30000000ull
#define D3_SRAM_BASE       0x38000000ull
#define CALIBRATION_BASE   0x1ff1e000ull
#define CALIBRATION_UID0   (CALIBRATION_BASE + 0x800)
#define FLASH_R_BASE       0x52002000ull
#define FLASH_KEYR1        (FLASH_R_BASE + 0x04)
#define FLASH_CR1          (FLASH_R_BASE + 0x0c)
#define FLASH_KEY1         0x45670123u
#define FLASH_KEY2         0xcdef89abu
#define FLASH_CR_PG        (1u << 1)
#define FLASH_IMAGE_SIZE   (1024 * 1024)
#define FLASH_IMAGE_MARKER 0x80000
#define FLASH_PROGRAM_OFFSET 0x1000

static void test_message_ram_owner_and_reset(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint64_t address = FDCAN_MSG_RAM_BASE + 0x123;
    uint64_t last = FDCAN_MSG_RAM_BASE + FDCAN_MSG_RAM_SIZE - 1;

    qtest_writeb(qts, address, 0xa5);
    qtest_writeb(qts, last, 0x5a);
    g_assert_cmphex(qtest_readb(qts, address), ==, 0xa5);
    g_assert_cmphex(qtest_readb(qts, last), ==, 0x5a);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readb(qts, address), ==, 0);
    g_assert_cmphex(qtest_readb(qts, last), ==, 0);
    qtest_quit(qts);
}

static void test_soc_memory_reset_and_persistence(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    const uint64_t volatile_regions[] = {
        ITCM_BASE, DTCM_BASE, AXI_SRAM_BASE, D2_SRAM_BASE, D3_SRAM_BASE,
    };
    const uint32_t flash_value = 0x5a;
    const uint32_t calibration_uid0 = 0x12345678;

    /* Use the real H723 unlock/program path so the read-only execution view
     * is not mistaken for a writable RAM window. */
    qtest_writel(qts, FLASH_KEYR1, FLASH_KEY1);
    qtest_writel(qts, FLASH_KEYR1, FLASH_KEY2);
    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    qtest_writeb(qts, FLASH_BASE, flash_value);
    qtest_writeb(qts, CALIBRATION_UID0, 0xff);
    for (size_t i = 0; i < ARRAY_SIZE(volatile_regions); ++i) {
        qtest_writeb(qts, volatile_regions[i] + 0x100, 0xa0 + i);
    }
    g_assert_cmphex(qtest_readb(qts, FLASH_BASE), ==, flash_value);
    g_assert_cmphex(qtest_readl(qts, CALIBRATION_UID0), ==,
                    calibration_uid0);

    /* A warm reset retains both non-volatile Flash and ordinary SRAM. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readb(qts, FLASH_BASE), ==, flash_value);
    g_assert_cmphex(qtest_readl(qts, CALIBRATION_UID0), ==,
                    calibration_uid0);
    for (size_t i = 0; i < ARRAY_SIZE(volatile_regions); ++i) {
        g_assert_cmphex(qtest_readb(qts, volatile_regions[i] + 0x100), ==,
                        0xa0 + i);
    }

    /* A cold reset clears volatile CPU RAM, but not Flash or factory ROM. */
    qtest_qom_set_bool(qts, "/machine", "cold-reset", true);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readb(qts, FLASH_BASE), ==, flash_value);
    g_assert_cmphex(qtest_readl(qts, CALIBRATION_UID0), ==,
                    calibration_uid0);
    for (size_t i = 0; i < ARRAY_SIZE(volatile_regions); ++i) {
        g_assert_cmphex(qtest_readb(qts, volatile_regions[i] + 0x100), ==, 0);
    }
    qtest_quit(qts);
}

static void shutdown_qemu(QTestState *qts)
{
    qtest_qmp_assert_success(qts, "{ 'execute': 'quit' }");
    qtest_quit(qts);
}

static void test_internal_flash_file_lifecycle(void)
{
    g_autofree char *directory = g_dir_make_tmp(
        "qtest-dm-mc02-flash-XXXXXX", NULL);
    g_autofree char *path = g_build_filename(directory, "flash.bin", NULL);
    g_autofree uint8_t *image = g_malloc(FLASH_IMAGE_SIZE);
    g_autofree gchar *saved = NULL;
    gsize saved_size = 0;
    const uint8_t marker = 0x6b;
    const uint8_t programmed = 0x3c;

    memset(image, 0xff, FLASH_IMAGE_SIZE);
    image[FLASH_IMAGE_MARKER] = marker;
    g_assert_true(g_file_set_contents(path, (const char *)image,
                                      FLASH_IMAGE_SIZE, NULL));

    QTestState *qts = qtest_initf("-machine dm-mc02,flash-file=%s", path);
    g_assert_cmphex(qtest_readb(qts, FLASH_BASE + FLASH_IMAGE_MARKER), ==,
                    marker);
    qtest_writel(qts, FLASH_R_BASE + 0x04, FLASH_KEY1);
    qtest_writel(qts, FLASH_R_BASE + 0x04, FLASH_KEY2);
    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    qtest_writeb(qts, FLASH_BASE + FLASH_PROGRAM_OFFSET, programmed);
    g_assert_cmphex(qtest_readb(qts, FLASH_BASE + FLASH_PROGRAM_OFFSET), ==,
                    programmed);
    QDict *response = qtest_qmp(
        qts, "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'flash-file', 'value': 'late-image.bin' } }");
    g_assert_true(qdict_haskey(response, "error"));
    qobject_unref(response);
    shutdown_qemu(qts);

    g_assert_true(g_file_get_contents(path, &saved, &saved_size, NULL));
    g_assert_cmpuint(saved_size, ==, FLASH_IMAGE_SIZE);
    g_assert_cmphex((uint8_t)saved[FLASH_IMAGE_MARKER], ==, marker);
    g_assert_cmphex((uint8_t)saved[FLASH_PROGRAM_OFFSET], ==, programmed);
    g_clear_pointer(&saved, g_free);

    qts = qtest_initf("-machine dm-mc02,flash-file=%s", path);
    g_assert_cmphex(qtest_readb(qts, FLASH_BASE + FLASH_IMAGE_MARKER), ==,
                    marker);
    g_assert_cmphex(qtest_readb(qts, FLASH_BASE + FLASH_PROGRAM_OFFSET), ==,
                    programmed);
    shutdown_qemu(qts);
    g_assert_cmpint(g_remove(path), ==, 0);
    g_assert_cmpint(g_rmdir(directory), ==, 0);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/memory/message-ram-owner",
                    test_message_ram_owner_and_reset);
    g_test_add_func("/dm-mc02/memory/soc-reset-persistence",
                    test_soc_memory_reset_and_persistence);
    g_test_add_func("/dm-mc02/memory/internal-flash-file-lifecycle",
                    test_internal_flash_file_lifecycle);
    return g_test_run();
}
