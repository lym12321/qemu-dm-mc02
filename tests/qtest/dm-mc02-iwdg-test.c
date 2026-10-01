/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"
#include "qapi/qmp/qdict.h"

#define IWDG_BASE 0x58004800ull
#define IWDG_KR   (IWDG_BASE + 0x00)
#define IWDG_PR   (IWDG_BASE + 0x04)
#define IWDG_RLR  (IWDG_BASE + 0x08)
#define IWDG_SR   (IWDG_BASE + 0x0c)
#define IWDG_WINR (IWDG_BASE + 0x10)
#define RCC_RSR   0x580244d0ull
#define IWDG_SR_PVU (1u << 0)
#define IWDG_SR_RVU (1u << 1)
#define IWDG_SR_WVU (1u << 2)
#define RCC_RSR_RMVF (1u << 16)
#define RCC_RSR_IWDG1RSTF (1u << 26)
#define IWDG_STATUS_UPDATE_DELAY_NS UINT64_C(156250)

static char *iwdg_diagnostics(QTestState *qts)
{
    QDict *response;
    char *diagnostics;

    response = qtest_qmp(qts,
        "{ 'execute': 'qom-get', 'arguments': { 'path': '/machine', "
        "'property': 'iwdg-diagnostics' } }");
    g_assert_nonnull(response);
    diagnostics = g_strdup(qdict_get_str(response, "return"));
    qobject_unref(response);
    return diagnostics;
}

static void configure_window(QTestState *qts, uint32_t window)
{
    /* Match the ST HAL ordering: start, unlock, wait for PR/RLR updates, then
     * use the committed WINR write's automatic reload. */
    qtest_writel(qts, IWDG_KR, 0xcccc);
    qtest_writel(qts, IWDG_KR, 0x5555);
    qtest_writel(qts, IWDG_PR, 6);       /* /256, 8 ms watchdog ticks */
    qtest_writel(qts, IWDG_RLR, 31);     /* 32 ticks, 256 ms timeout */
    qtest_clock_step(qts, 1000 * 1000);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, 0);
    qtest_writel(qts, IWDG_WINR, window);
    qtest_clock_step(qts, 1000 * 1000);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, 0);
}

static void configure_timeout(QTestState *qts)
{
    qtest_writel(qts, IWDG_KR, 0xcccc);
    qtest_writel(qts, IWDG_KR, 0x5555);
    qtest_writel(qts, IWDG_PR, 6);       /* /256 */
    qtest_writel(qts, IWDG_RLR, 31);     /* 8192 LSI cycles */
    qtest_clock_step(qts, 1000 * 1000);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, 0);
    qtest_writel(qts, IWDG_KR, 0xaaaa);
}

static void test_status_update_handshake(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0");
    g_autofree char *diagnostics;

    /* Locked writes leave both the committed register and SR unchanged. */
    qtest_writel(qts, IWDG_PR, 6);
    g_assert_cmphex(qtest_readl(qts, IWDG_PR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, 0);

    qtest_writel(qts, IWDG_KR, 0x5555);
    qtest_writel(qts, IWDG_PR, 6);
    g_assert_cmphex(qtest_readl(qts, IWDG_PR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, IWDG_SR_PVU);
    /* The register's own update bit suppresses a second write. */
    qtest_writel(qts, IWDG_PR, 5);
    qtest_writel(qts, IWDG_RLR, 31);
    g_assert_cmphex(qtest_readl(qts, IWDG_RLR), ==, 0xfff);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==,
                    IWDG_SR_PVU | IWDG_SR_RVU);
    qtest_clock_step(qts, IWDG_STATUS_UPDATE_DELAY_NS - 1);
    g_assert_cmphex(qtest_readl(qts, IWDG_PR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, IWDG_RLR), ==, 0xfff);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==,
                    IWDG_SR_PVU | IWDG_SR_RVU);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, IWDG_PR), ==, 6);
    g_assert_cmphex(qtest_readl(qts, IWDG_RLR), ==, 31);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, 0);

    /* WINR remains old while WVU is set, then its commit automatically
     * reloads the watchdog using the already committed PR/RLR values. */
    qtest_writel(qts, IWDG_KR, 0xcccc);
    qtest_clock_step(qts, 200 * 1000 * 1000);
    qtest_writel(qts, IWDG_KR, 0x5555);
    qtest_writel(qts, IWDG_WINR, 30);
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 0xfff);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, IWDG_SR_WVU);
    qtest_clock_step(qts, IWDG_STATUS_UPDATE_DELAY_NS - 1);
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 0xfff);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 30);
    g_assert_cmphex(qtest_readl(qts, IWDG_SR), ==, 0);
    qtest_clock_step(qts, 255 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=0"));
    qtest_clock_step(qts, 2 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=1"));
    qtest_quit(qts);
}

static void test_window_boundary_and_reload(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0");
    g_autofree char *diagnostics;

    /* WINR=0xfff is the reset/default configuration and does not enable the
     * descending reload window. */
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 0xfff);
    qtest_writel(qts, IWDG_WINR, 30);
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 0xfff);
    qtest_writeb(qts, IWDG_WINR, 30);
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 0xfff);
    configure_window(qts, 30);
    qtest_clock_step(qts, 8 * 1000 * 1000);
    qtest_writel(qts, IWDG_KR, 0xaaaa);

    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "reloads=1"));
    g_assert_nonnull(strstr(diagnostics, "window-violations=0"));
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 30);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_quit(qts);
}

static void test_default_window_is_disabled(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0");
    g_autofree char *diagnostics;

    qtest_writel(qts, IWDG_KR, 0xcccc);
    qtest_writel(qts, IWDG_KR, 0xaaaa);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "reloads=1"));
    g_assert_nonnull(strstr(diagnostics, "window-violations=0"));
    qtest_quit(qts);
}

static void test_early_reload_requests_reset(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0");
    g_autofree char *diagnostics;

    configure_window(qts, 30);
    qtest_writel(qts, IWDG_KR, 0xaaaa);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "reloads=0"));
    g_assert_nonnull(strstr(diagnostics, "window-violations=1"));
    /* A reset request is processed before the next QTest command. */
    g_assert_cmphex(qtest_readl(qts, IWDG_WINR), ==, 0xfff);
    qtest_quit(qts);
}

static void test_window_timeout(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0");
    g_autofree char *diagnostics;

    configure_window(qts, 30);
    qtest_clock_step(qts, 256 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=1"));
    g_assert_nonnull(strstr(diagnostics, "window-violations=0"));
    qtest_quit(qts);
}

static void test_reset_reason_is_projected_to_rcc(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,iwdg-boot-grace-ms=0");

    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_IWDG1RSTF, ==, 0);
    configure_timeout(qts);
    qtest_clock_step(qts, 256 * 1000 * 1000);

    /* The reset request is processed before the next QTest transaction. */
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_IWDG1RSTF, ==,
                    RCC_RSR_IWDG1RSTF);

    /* A normal system reset preserves reset-source flags until RMVF. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_IWDG1RSTF, ==,
                    RCC_RSR_IWDG1RSTF);

    /* Source bits are read-only; RMVF is the only clear operation. */
    qtest_writel(qts, RCC_RSR, RCC_RSR_IWDG1RSTF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_IWDG1RSTF, ==,
                    RCC_RSR_IWDG1RSTF);
    qtest_writew(qts, RCC_RSR + 2, 1);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_IWDG1RSTF, ==, 0);
    qtest_quit(qts);
}

static void test_lsi_error_changes_timeout_monotonically(void)
{
    QTestState *qts;
    g_autofree char *diagnostics;

    /* A +10% LSI error expires before 240 ms, while nominal 32 kHz does not.
     * This threshold is deliberately well away from the exact 232.727 ms
     * boundary so host-side command overhead cannot affect the result. */
    qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0,"
                     "iwdg-lsi-hz=32000,iwdg-lsi-error-ppm=100000");
    configure_timeout(qts);
    qtest_clock_step(qts, 240 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=1"));
    g_assert_nonnull(strstr(diagnostics, "lsi-hz=32000"));
    g_assert_nonnull(strstr(diagnostics, "lsi-error-ppm=100000"));
    g_assert_nonnull(strstr(diagnostics, "effective-lsi-hz=35200"));
    qtest_quit(qts);

    /* The nominal timeout is exactly 256 ms for PR=6/RLR=31. */
    qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0,"
                     "iwdg-lsi-hz=32000,iwdg-lsi-error-ppm=0");
    configure_timeout(qts);
    {
        QDict *response;
        QDict *error;

        response = qtest_qmp(qts,
            "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
            "'property': 'iwdg-lsi-hz', 'value': '64000' } }");
        error = qdict_get_qdict(response, "error");
        g_assert_nonnull(error);
        g_assert_nonnull(strstr(qdict_get_str(error, "desc"),
                                "startup-only"));
        qobject_unref(response);

        response = qtest_qmp(qts,
            "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
            "'property': 'iwdg-lsi-error-ppm', 'value': '100000' } }");
        error = qdict_get_qdict(response, "error");
        g_assert_nonnull(error);
        g_assert_nonnull(strstr(qdict_get_str(error, "desc"),
                                "startup-only"));
        qobject_unref(response);
    }
    qtest_clock_step(qts, 250 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=0"));
    qtest_clock_step(qts, 10 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=1"));
    qtest_quit(qts);

    /* A -10% LSI error expires after 280 ms but before 290 ms. */
    qts = qtest_init("-machine dm-mc02,iwdg-boot-grace-ms=0,"
                     "iwdg-lsi-hz=32000,iwdg-lsi-error-ppm=-100000");
    configure_timeout(qts);
    qtest_clock_step(qts, 280 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=0"));
    qtest_clock_step(qts, 10 * 1000 * 1000);
    diagnostics = iwdg_diagnostics(qts);
    g_assert_nonnull(strstr(diagnostics, "timeouts=1"));
    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/iwdg/status-update-handshake",
                    test_status_update_handshake);
    g_test_add_func("/dm-mc02/iwdg/window-boundary-and-reload",
                    test_window_boundary_and_reload);
    g_test_add_func("/dm-mc02/iwdg/default-window-disabled",
                    test_default_window_is_disabled);
    g_test_add_func("/dm-mc02/iwdg/early-reload-requests-reset",
                    test_early_reload_requests_reset);
    g_test_add_func("/dm-mc02/iwdg/window-timeout", test_window_timeout);
    g_test_add_func("/dm-mc02/iwdg/reset-reason-rcc",
                    test_reset_reason_is_projected_to_rcc);
    g_test_add_func("/dm-mc02/iwdg/lsi-error-monotonic",
                    test_lsi_error_changes_timeout_monotonically);
    return g_test_run();
}
