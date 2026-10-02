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
#define FLASH_SR1          (FLASH_R_BASE + 0x10)
#define FLASH_CCR1         (FLASH_R_BASE + 0x14)
#define FLASH_KEY1         0x45670123u
#define FLASH_KEY2         0xcdef89abu
#define FLASH_CR_PG        (1u << 1)
#define FLASH_CR_LOCK      (1u << 0)
#define FLASH_CR_SER       (1u << 2)
#define FLASH_CR_FW        (1u << 6)
#define FLASH_CR_START     (1u << 7)
#define FLASH_SR_EOP       (1u << 16)
#define FLASH_SR_PGSERR    (1u << 18)
#define FLASH_IMAGE_SIZE   (1024 * 1024)
#define FLASH_IMAGE_MARKER 0x80000
#define FLASH_PROGRAM_OFFSET 0x1000

static void flash_unlock(QTestState *qts)
{
    qtest_writel(qts, FLASH_KEYR1, FLASH_KEY1);
    qtest_writel(qts, FLASH_KEYR1, FLASH_KEY2);
}

static void flash_program_word(QTestState *qts, uint64_t address,
                               uint32_t first)
{
    /* ST HAL_FLASH_Program() on H72x/3x: a 256-bit-aligned address and
     * FLASH_NB_32BITWORD_IN_FLASHWORD == 8 consecutive 32-bit stores.
     * Source: ST stm32h7xx-hal-driver 7e541d9, Src/stm32h7xx_hal_flash.c. */
    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    for (unsigned i = 0; i < 8; ++i) {
        qtest_writel(qts, address + 4 * i, i ? UINT32_MAX : first);
    }
    qtest_writel(qts, FLASH_CR1, 0);
}

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
    flash_unlock(qts);
    flash_program_word(qts, FLASH_BASE, 0xffffff00 | flash_value);
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

static void test_internal_flash_word_transaction(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    const uint64_t address = FLASH_BASE + 0x20000;
    const uint32_t value = 0x12345678;

    /* Locked or PG-disabled writes do not alter the read-only NVM view. */
    qtest_writeb(qts, FLASH_CR1, 0);
    qtest_writew(qts, FLASH_CR1, FLASH_CR_PG);
    g_assert_cmphex(qtest_readl(qts, FLASH_CR1), ==, FLASH_CR_LOCK);
    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    g_assert_cmphex(qtest_readl(qts, FLASH_CR1), ==, FLASH_CR_LOCK);
    qtest_writel(qts, address, value);
    g_assert_cmphex(qtest_readl(qts, address), ==, UINT32_MAX);
    flash_unlock(qts);
    qtest_writel(qts, address, value);
    g_assert_cmphex(qtest_readl(qts, address), ==, UINT32_MAX);

    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    for (unsigned i = 0; i < 7; ++i) {
        qtest_writel(qts, address + 4 * i, value + i);
        g_assert_cmphex(qtest_readl(qts, address), ==, UINT32_MAX);
        g_assert_cmphex(qtest_readl(qts, FLASH_SR1) & FLASH_SR_EOP, ==, 0);
    }
    qtest_writel(qts, address + 28, value + 7);
    for (unsigned i = 0; i < 8; ++i) {
        g_assert_cmphex(qtest_readl(qts, address + 4 * i), ==, value + i);
    }
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_EOP);

    /* ST HAL __HAL_FLASH_CLEAR_FLAG_BANK1() uses CCR1. SR1 is read-only. */
    qtest_writel(qts, FLASH_SR1, FLASH_SR_EOP);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_EOP);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_EOP);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, 0);

    /* Model admission rejects 0->1 requests before changing any NVM byte.
     * PGSERR here is the subset's rejection status, not an ECC oracle. */
    flash_program_word(qts, address, UINT32_MAX);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    for (unsigned i = 0; i < 8; ++i) {
        g_assert_cmphex(qtest_readl(qts, address + 4 * i), ==, value + i);
    }
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);

    /* Unsupported bus widths and invalid starts reject explicitly. */
    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    qtest_writel(qts, address + 36, value);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);
    qtest_writeb(qts, address + 32, 0);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);
    qtest_writeq(qts, address + 32, 0);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);
    qtest_writel(qts, address + 33, value);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);

    /* A partial word cannot switch to another word or be forced into NVM. */
    qtest_writel(qts, address + 32, value);
    qtest_writel(qts, address + 64, value);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    g_assert_cmphex(qtest_readl(qts, address + 32), ==, UINT32_MAX);
    g_assert_cmphex(qtest_readl(qts, address + 64), ==, UINT32_MAX);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);
    qtest_writel(qts, address + 32, value);
    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG | FLASH_CR_FW);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    g_assert_cmphex(qtest_readl(qts, address + 32), ==, UINT32_MAX);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);
    qtest_writel(qts, address + 32, value);
    qtest_writel(qts, FLASH_CR1, 0);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_PGSERR);
    g_assert_cmphex(qtest_readl(qts, address + 32), ==, UINT32_MAX);
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_PGSERR);

    /* Sector erase permits a new complete program transaction. */
    qtest_writel(qts, FLASH_CR1, FLASH_CR_SER | (1u << 8) | FLASH_CR_START);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_EOP);
    for (unsigned i = 0; i < 8; ++i) {
        g_assert_cmphex(qtest_readl(qts, address + 4 * i), ==, UINT32_MAX);
    }
    qtest_writel(qts, FLASH_CCR1, FLASH_SR_EOP);
    flash_program_word(qts, address, value);
    g_assert_cmphex(qtest_readl(qts, address), ==, value);

    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    qtest_writel(qts, address + 32, value);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, FLASH_CR1), ==, FLASH_CR_LOCK);
    g_assert_cmphex(qtest_readl(qts, address), ==, value);
    g_assert_cmphex(qtest_readl(qts, address + 32), ==, UINT32_MAX);
    qtest_writel(qts, address, 0);
    g_assert_cmphex(qtest_readl(qts, address), ==, value);

    /* HAL_FLASH_Unlock only clears LOCK through KEYR1, preserving PG.
     * Unlock must reproject the program window without a later CR1 write. */
    flash_unlock(qts);
    qtest_writel(qts, FLASH_CR1, FLASH_CR_LOCK | FLASH_CR_PG);
    flash_unlock(qts);
    g_assert_cmphex(qtest_readl(qts, FLASH_CR1), ==, FLASH_CR_PG);
    for (unsigned i = 0; i < 8; ++i) {
        qtest_writel(qts, address + 32 + 4 * i, value + i);
    }
    g_assert_cmphex(qtest_readl(qts, address + 32), ==, value);
    g_assert_cmphex(qtest_readl(qts, FLASH_SR1), ==, FLASH_SR_EOP);
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
    flash_unlock(qts);
    flash_program_word(qts, FLASH_BASE + FLASH_PROGRAM_OFFSET,
                        0xffffff00 | programmed);
    /* Lifecycle persistence saves committed NVM, never an incomplete buffer. */
    qtest_writel(qts, FLASH_CR1, FLASH_CR_PG);
    qtest_writel(qts, FLASH_BASE + FLASH_PROGRAM_OFFSET + 32, 0);
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
    for (unsigned i = 1; i < 64; ++i) {
        g_assert_cmphex((uint8_t)saved[FLASH_PROGRAM_OFFSET + i], ==, 0xff);
    }
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

static void test_internal_flash_rejects_invalid_image(void)
{
    if (g_test_subprocess()) {
        QTestState *qts = qtest_initf("-machine dm-mc02,flash-file=%s",
                                     g_getenv("DM_QTEST_INVALID_FLASH"));
        qtest_quit(qts);
        g_assert_not_reached();
    }

    g_autofree char *directory = g_dir_make_tmp(
        "qtest-dm-mc02-flash-invalid-XXXXXX", NULL);
    g_autofree char *path = g_build_filename(directory, "flash.bin", NULL);
    g_autofree char *image = g_malloc0(FLASH_IMAGE_SIZE + 1);
    const size_t invalid_sizes[] = { 3, FLASH_IMAGE_SIZE + 1 };

    for (unsigned i = 0; i < ARRAY_SIZE(invalid_sizes); ++i) {
        g_assert_true(g_file_set_contents(path, image, invalid_sizes[i], NULL));
        g_setenv("DM_QTEST_INVALID_FLASH", path, true);
        g_test_trap_subprocess(NULL, 5 * G_USEC_PER_SEC, 0);
        g_test_trap_assert_failed();
        g_test_trap_assert_stderr("*internal Flash image*rejected*expected an exact*");
    }
    g_unsetenv("DM_QTEST_INVALID_FLASH");
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
    g_test_add_func("/dm-mc02/memory/internal-flash-word-transaction",
                    test_internal_flash_word_transaction);
    g_test_add_func("/dm-mc02/memory/internal-flash-invalid-image",
                    test_internal_flash_rejects_invalid_image);
    return g_test_run();
}
