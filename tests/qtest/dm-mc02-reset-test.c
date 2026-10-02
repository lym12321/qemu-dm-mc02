/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"

#define DTCM_BASE          0x20000000ull
#define GPIOA_BASE         0x58020000ull
#define GPIOC_BASE         0x58020800ull
#define GPIO_MODER         0x00
#define GPIO_IDR           0x10
#define GPIO_ODR           0x14
#define TIM2_BASE          0x40000000ull
#define TIM2_CR1           (TIM2_BASE + 0x00)
#define SPI2_BASE          0x40003800ull
#define SPI2_CR1           (SPI2_BASE + 0x00)
#define CORDIC_BASE        0x58004400ull
#define CORDIC_CSR         (CORDIC_BASE + 0x00)
#define CORDIC_WDATA       (CORDIC_BASE + 0x04)
#define CORDIC_RDATA       (CORDIC_BASE + 0x08)
#define USB_GRXFSIZ        0x40040024ull
#define FDCAN_MSG_RAM      0x4000ac55ull
#define DTCM_MAGIC         0xa55a5aa5u
#define GPIO_POWER_OUTPUTS ((1u << (13 * 2)) | (1u << (14 * 2)) | \
                           (1u << (15 * 2)))
#define GPIO_POWER_LATCHES ((1u << 13) | (1u << 14) | (1u << 15))
#define GPIO_BMI_CS_INACTIVE ((1u << 0) | (1u << 3))

static void set_bool(QTestState *qts, const char *property, bool value)
{
    qtest_qom_set_bool(qts, "/machine", property, value);
}

static void test_warm_reset_reprojects_board_state(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t rx_fifo_reset = qtest_readl(qts, USB_GRXFSIZ);

    qtest_writel(qts, DTCM_BASE, DTCM_MAGIC);
    qtest_writel(qts, TIM2_CR1, 1);
    qtest_writel(qts, SPI2_CR1, 1);
    qtest_writel(qts, CORDIC_CSR, 1u << 19); /* Paired result pending. */
    qtest_writel(qts, CORDIC_WDATA, 0x20000000);
    qtest_writel(qts, USB_GRXFSIZ, 77);
    qtest_writeb(qts, FDCAN_MSG_RAM, 0xa5);
    set_bool(qts, "user-key", true);
    set_bool(qts, "electrical-power", true);
    qtest_writel(qts, GPIOC_BASE + GPIO_MODER, GPIO_POWER_OUTPUTS);
    qtest_writel(qts, GPIOC_BASE + GPIO_ODR, GPIO_POWER_LATCHES);
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* Warm reset keeps ordinary SoC RAM but resets peripheral state. */
    g_assert_cmphex(qtest_readl(qts, DTCM_BASE), ==, DTCM_MAGIC);
    g_assert_cmphex(qtest_readl(qts, TIM2_CR1), ==, 0);
    g_assert_cmphex(qtest_readl(qts, SPI2_CR1), ==, 0);
    g_assert_cmphex(qtest_readl(qts, CORDIC_CSR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, CORDIC_RDATA), ==, 0);
    g_assert_cmphex(qtest_readl(qts, USB_GRXFSIZ), ==, rx_fifo_reset);
    g_assert_cmphex(qtest_readb(qts, FDCAN_MSG_RAM), ==, 0);

    /* Board CS deassertion and external key input are re-projected after
     * generic GPIO/EXTI reset. */
    g_assert_cmphex(qtest_readl(qts, GPIOC_BASE + GPIO_ODR), ==,
                    GPIO_BMI_CS_INACTIVE);
    g_assert_cmphex(qtest_readl(qts, GPIOA_BASE + GPIO_IDR) & (1u << 15), ==,
                    0);
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "user-key"));

    /* The electrical policy remains enabled, but reset GPIO latches remove
     * switched 5 V and the downstream CAN/RS485 consumers are powered off. */
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "out1-enabled"));
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "out2-enabled"));
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "5v-switch-enabled"));
    g_assert_false(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "mcu-power-good"));
    qtest_quit(qts);
}

static void test_cold_reset_clears_volatile_memory(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    qtest_writel(qts, DTCM_BASE, DTCM_MAGIC);
    set_bool(qts, "cold-reset", true);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, DTCM_BASE), ==, 0);
    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/board-reset/warm-state",
                    test_warm_reset_reprojects_board_state);
    g_test_add_func("/dm-mc02/board-reset/cold-volatile-memory",
                    test_cold_reset_clears_volatile_memory);
    return g_test_run();
}
