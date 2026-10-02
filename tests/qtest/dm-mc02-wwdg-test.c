/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"
#include "qapi/qmp/qdict.h"

#define WWDG_BASE       0x50003000ull
#define WWDG_CR         (WWDG_BASE + 0x00)
#define WWDG_CFR        (WWDG_BASE + 0x04)
#define WWDG_SR         (WWDG_BASE + 0x08)
#define RCC_RSR         0x580244d0ull
#define RCC_CFGR        0x58024410ull
#define RCC_D1CFGR      0x58024418ull
#define RCC_D2CFGR      0x5802441cull
#define PPB_BASE        0xe000e000ull
#define NVIC_ISPR0      (PPB_BASE + 0x200)

#define WWDG_CR_T       0x7fu
#define WWDG_CR_WDGA   (1u << 7)
#define WWDG_CFR_W      0x7fu
#define WWDG_CFR_EWI   (1u << 9)
#define WWDG_CFR_WDGTB_SHIFT 11
#define WWDG_SR_EWIF   (1u << 0)

#define RCC_RSR_RMVF       (1u << 16)
#define RCC_RSR_WWDG1RSTF (1u << 28)
#define WWDG_TICK_NS       64000ull /* 4096 / 64 MHz */

static char *wwdg_diagnostics(QTestState *qts)
{
    QDict *response;
    char *diagnostics;

    response = qtest_qmp(qts,
        "{ 'execute': 'qom-get', 'arguments': { 'path': '/machine', "
        "'property': 'wwdg-diagnostics' } }");
    g_assert_nonnull(response);
    diagnostics = g_strdup(qdict_get_str(response, "return"));
    qobject_unref(response);
    return diagnostics;
}

static void wwdg_start(QTestState *qts, uint32_t cfr, uint32_t counter)
{
    qtest_writel(qts, WWDG_CFR, cfr);
    qtest_writel(qts, WWDG_CR, counter | WWDG_CR_WDGA);
}

static void test_defaults_and_early_wakeup(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t irq_mask = 1u;
    g_autofree char *diagnostics = NULL;

    g_assert_cmphex(qtest_readl(qts, WWDG_CR), ==, WWDG_CR_T);
    g_assert_cmphex(qtest_readl(qts, WWDG_CFR), ==, WWDG_CFR_W);
    g_assert_cmphex(qtest_readl(qts, WWDG_SR), ==, 0);
    diagnostics = wwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "clock-hz=64000000"));

    /* WWDG register transactions are not allowed to cross a 32-bit word or
     * be unaligned; QEMU must not split them into writes to adjacent fields. */
    qtest_writew(qts, WWDG_CR + 1, 0xffff);
    qtest_writew(qts, WWDG_CR + 3, 0xffff);
    qtest_writel(qts, WWDG_CR + 2, 0xffffffff);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR), ==, WWDG_CR_T);
    g_assert_cmphex(qtest_readl(qts, WWDG_CFR), ==, WWDG_CFR_W);

    qtest_irq_intercept_in(qts, "/machine/armv7m");
    qtest_writel(qts, PPB_BASE + 0x100, irq_mask);
    wwdg_start(qts, WWDG_CFR_W | WWDG_CFR_EWI, WWDG_CR_T);

    qtest_clock_step(qts, (WWDG_CR_T - 0x40) * WWDG_TICK_NS - 1);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR) & WWDG_CR_T, >, 0x40);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR) & WWDG_CR_T, ==, 0x40);
    g_assert_cmphex(qtest_readl(qts, WWDG_SR), ==, WWDG_SR_EWIF);
    g_assert_true(qtest_get_irq(qts, 0));
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0) & irq_mask, ==, irq_mask);

    /* EWIF is W0C; clearing it deasserts the NVIC line while the second
     * watchdog stage remains armed. */
    qtest_writel(qts, WWDG_SR, 0);
    g_assert_cmphex(qtest_readl(qts, WWDG_SR), ==, 0);
    g_assert_false(qtest_get_irq(qts, 0));

    /* WDGA cannot be cleared by software.  A T-only write is a legal reload
     * while the active counter is inside the default [0x40, 0x7f] window. */
    qtest_writeb(qts, WWDG_CR, WWDG_CR_T);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR), ==,
                    WWDG_CR_T | WWDG_CR_WDGA);
    diagnostics = wwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "reloads=2"));
    g_assert_nonnull(strstr(diagnostics, "ewi=1"));
    qtest_quit(qts);
}

static void test_prescaler_change_preserves_counter(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t cfr_wdgtb1 = WWDG_CFR_W | WWDG_CFR_EWI |
                          (1u << WWDG_CFR_WDGTB_SHIFT);

    wwdg_start(qts, WWDG_CFR_W | WWDG_CFR_EWI, WWDG_CR_T);
    qtest_clock_step(qts, 10 * WWDG_TICK_NS);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR) & WWDG_CR_T, ==, 0x75);

    qtest_writel(qts, WWDG_CFR, cfr_wdgtb1);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR) & WWDG_CR_T, ==, 0x75);
    qtest_clock_step(qts, (0x75 - 0x40) * 2 * WWDG_TICK_NS - 1);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR) & WWDG_CR_T, >, 0x40);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, WWDG_SR), ==, WWDG_SR_EWIF);
    qtest_quit(qts);
}

static void assert_tick_after(QTestState *qts, uint64_t tick_ns)
{
    wwdg_start(qts, WWDG_CFR_W, WWDG_CR_T);
    qtest_clock_step(qts, tick_ns - 1);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR) & WWDG_CR_T, ==, 0x7f);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR) & WWDG_CR_T, ==, 0x7e);
}

static void test_apb3_prescaler(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* ST LL_RCC_CALC_PCLK3_FREQ: HCLK 64 MHz / D1PPRE 2 = 32 MHz. */
    qtest_writel(qts, RCC_D1CFGR, 4u << 4);
    assert_tick_after(qts, 128000);
    qtest_quit(qts);
}

static void test_apb1_and_timpre_do_not_clock_wwdg(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* ST LL_APB3_GRP1_PERIPH_WWDG1: neither PCLK1 nor TIMPRE applies. */
    qtest_writel(qts, RCC_D2CFGR, 7u << 4);
    qtest_writel(qts, RCC_CFGR, 1u << 15);
    assert_tick_after(qts, 64000);
    qtest_quit(qts);
}

static void test_d1_clock_chain(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* CMSIS SystemCoreClockUpdate + LL_RCC_CALC_PCLK3_FREQ:
     * HSI 64 MHz / D1CPRE 8 / HPRE 2 / D1PPRE 4 = PCLK3 1 MHz. */
    qtest_writel(qts, RCC_D1CFGR, (0xau << 8) | (5u << 4) | 8u);
    assert_tick_after(qts, 4096000);
    qtest_quit(qts);
}

static void test_window_violation_and_timeout_reset_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    g_autofree char *diagnostics = NULL;

    /* W=0x70; after one tick CNT=0x7e, so reloading is outside the window. */
    wwdg_start(qts, 0x70, WWDG_CR_T);
    qtest_clock_step(qts, WWDG_TICK_NS);
    qtest_writel(qts, WWDG_CR, WWDG_CR_T | WWDG_CR_WDGA);
    g_assert_cmphex(qtest_readl(qts, WWDG_CR), ==, WWDG_CR_T);
    diagnostics = wwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "window-violations=1"));
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_WWDG1RSTF, ==,
                    RCC_RSR_WWDG1RSTF);

    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_WWDG1RSTF, ==, 0);
    wwdg_start(qts, WWDG_CFR_W, WWDG_CR_T);
    qtest_clock_step(qts, (WWDG_CR_T - 0x40 + 1) * WWDG_TICK_NS);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_WWDG1RSTF, ==,
                    RCC_RSR_WWDG1RSTF);
    diagnostics = wwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=1"));
    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/wwdg/defaults-and-ewi",
                    test_defaults_and_early_wakeup);
    g_test_add_func("/dm-mc02/wwdg/prescaler-phase",
                    test_prescaler_change_preserves_counter);
    g_test_add_func("/dm-mc02/wwdg/apb3-prescaler", test_apb3_prescaler);
    g_test_add_func("/dm-mc02/wwdg/apb1-timpre-independent",
                    test_apb1_and_timpre_do_not_clock_wwdg);
    g_test_add_func("/dm-mc02/wwdg/d1-clock-chain", test_d1_clock_chain);
    g_test_add_func("/dm-mc02/wwdg/window-and-timeout-reset",
                    test_window_violation_and_timeout_reset_reason);
    return g_test_run();
}
