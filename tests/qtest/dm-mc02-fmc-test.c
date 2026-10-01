/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"

#define FMC_BASE 0x52004000

static void test_register_window(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmphex(qtest_readl(qts, FMC_BASE), ==, 0);

    qtest_writel(qts, FMC_BASE, 0x11223344);
    g_assert_cmphex(qtest_readl(qts, FMC_BASE), ==, 0x11223344);

    qtest_writeb(qts, FMC_BASE + 1, 0xaa);
    g_assert_cmphex(qtest_readl(qts, FMC_BASE), ==, 0x1122aa44);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, FMC_BASE), ==, 0);

    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/fmc/register-window", test_register_window);
    return g_test_run();
}
