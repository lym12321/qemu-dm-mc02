/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"
#include "qapi/qmp/qdict.h"
#include "qapi/qmp/qlist.h"
#include "qapi/qmp/qnum.h"

#define PPB_BASE          0xe000e000ull
#define SYST_CSR          (PPB_BASE + 0x10)
#define SYST_RVR          (PPB_BASE + 0x14)
#define SYST_CVR          (PPB_BASE + 0x18)
#define NVIC_ISER0        (PPB_BASE + 0x100)
#define NVIC_ICER0        (PPB_BASE + 0x180)
#define NVIC_ISPR0        (PPB_BASE + 0x200)
#define NVIC_ISPR1        (PPB_BASE + 0x204)
#define NVIC_ICPR0        (PPB_BASE + 0x280)
#define NVIC_ICPR1        (PPB_BASE + 0x284)
#define NVIC_IPR0         (PPB_BASE + 0x400)
#define SCB_ICSR          (PPB_BASE + 0xd04)
#define SCB_AIRCR         (PPB_BASE + 0xd0c)

#define EXTI_BASE         0x58000000ull
#define EXTI_FTSR1        (EXTI_BASE + 0x04)
#define EXTI_PR1          (EXTI_BASE + 0x14)
#define EXTI_C1IMR1       (EXTI_BASE + 0x80)

#define SYST_CSR_ENABLE       (1u << 0)
#define SYST_CSR_TICKINT      (1u << 1)
#define SYST_CSR_CLKSOURCE    (1u << 2)
#define SYST_CSR_COUNTFLAG    (1u << 16)
#define SCB_ICSR_PENDSTSET    (1u << 26)
#define SCB_AIRCR_VECTKEY     (0x5fau << 16)
#define SCB_AIRCR_SYSRESETREQ (1u << 2)
#define RCC_RSR               0x580244d0ull
#define RCC_RSR_RMVF          (1u << 16)
#define RCC_RSR_BORRSTF       (1u << 21)
#define RCC_RSR_PINRSTF       (1u << 22)
#define RCC_RSR_PORRSTF       (1u << 23)
#define RCC_RSR_SFTRSTF       (1u << 24)
#define TEST_EXTERNAL_IRQ     28
#define TEST_EXTI_IRQ         40

static bool qom_list_has_child(QTestState *qts, const char *name,
                               const char *type)
{
    QDict *response;
    QList *properties;
    QListEntry *entry;
    bool found = false;

    response = qtest_qmp(
        qts, "{ 'execute': 'qom-list', 'arguments': { 'path': %s } }",
        "/machine/armv7m");
    g_assert_nonnull(response);
    properties = qdict_get_qlist(response, "return");
    g_assert_nonnull(properties);

    QLIST_FOREACH_ENTRY(properties, entry) {
        QDict *property = qobject_to(QDict, qlist_entry_obj(entry));
        const char *property_name = qdict_get_str(property, "name");
        const char *property_type = qdict_get_str(property, "type");

        if (!strcmp(property_name, name)) {
            g_assert_cmpstr(property_type, ==, type);
            found = true;
            break;
        }
    }

    qobject_unref(response);
    return found;
}

static unsigned qom_list_count_external_irq_inputs(QTestState *qts)
{
    QDict *response;
    QList *properties;
    QListEntry *entry;
    unsigned count = 0;

    response = qtest_qmp(
        qts, "{ 'execute': 'qom-list', 'arguments': { 'path': %s } }",
        "/machine/armv7m");
    g_assert_nonnull(response);
    g_assert_true(qdict_haskey(response, "return"));
    properties = qdict_get_qlist(response, "return");
    g_assert_nonnull(properties);

    QLIST_FOREACH_ENTRY(properties, entry) {
        QDict *property = qobject_to(QDict, qlist_entry_obj(entry));
        const char *name = qdict_get_str(property, "name");

        if (g_str_has_prefix(name, "unnamed-gpio-in[")) {
            count++;
        }
    }

    qobject_unref(response);
    return count;
}

static uint64_t qom_get_uint(QTestState *qts, const char *path,
                             const char *property)
{
    QDict *response;
    QNum *value;
    uint64_t result;

    response = qtest_qmp(
        qts, "{ 'execute': 'qom-get', 'arguments': { 'path': %s, "
        "'property': %s } }", path, property);
    g_assert_nonnull(response);
    value = qobject_to(QNum, qdict_get(response, "return"));
    g_assert_nonnull(value);
    g_assert_true(qnum_get_try_uint(value, &result));
    qobject_unref(response);
    return result;
}

static void test_armv7m_children_and_irq_width(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* These are QEMU-owned children.  Their DeviceClass VMState is registered
     * when each child is realized; the board does not duplicate their state. */
    g_assert_true(qom_list_has_child(qts, "cpu", "child<cortex-m7-arm-cpu>"));
    g_assert_true(qom_list_has_child(qts, "nvic", "child<armv7m_nvic>"));
    g_assert_true(qom_list_has_child(qts, "systick-reg-ns",
                                     "child<armv7m_systick>"));
    /* The alias exposes NVIC's complete vector count, including the 16
     * architecturally internal exceptions.  The board-facing input count is
     * checked separately from the QOM GPIO list. */
    g_assert_cmpuint(qom_get_uint(qts, "/machine/armv7m", "num-irq"),
                     ==, 179);
    g_assert_cmpuint(qom_list_count_external_irq_inputs(qts), ==, 163);
    g_assert_cmpuint(qom_get_uint(qts, "/machine/armv7m/nvic", "num-irq"),
                     ==, 179);

    qtest_quit(qts);
}

static void test_external_irq_and_systick_virtual_time(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t irq_mask = UINT32_C(1) << TEST_EXTERNAL_IRQ;

    /* Observe the board-facing input GPIO and verify its NVIC-visible state.
     * The CPU exception output is a SysBus IRQ rather than a QOM GPIO-out,
     * so this qtest observes it through the architected pending register. */
    qtest_irq_intercept_in(qts, "/machine/armv7m");

    qtest_writeb(qts, NVIC_IPR0 + TEST_EXTERNAL_IRQ, 0x20);
    g_assert_cmphex(qtest_readb(qts, NVIC_IPR0 + TEST_EXTERNAL_IRQ), ==, 0x20);
    qtest_writel(qts, NVIC_ISER0, irq_mask);
    qtest_set_irq_in(qts, "/machine/armv7m", NULL, TEST_EXTERNAL_IRQ, 1);
    g_assert_true(qtest_get_irq(qts, TEST_EXTERNAL_IRQ));
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0) & irq_mask, ==, irq_mask);

    qtest_set_irq_in(qts, "/machine/armv7m", NULL, TEST_EXTERNAL_IRQ, 0);
    g_assert_false(qtest_get_irq(qts, TEST_EXTERNAL_IRQ));
    qtest_writel(qts, NVIC_ICPR0, irq_mask);
    qtest_writel(qts, NVIC_ICER0, irq_mask);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0) & irq_mask, ==, 0);

    /* The DM-MC02 CPU clock is 64 MHz at reset.  A reload value of 63
     * therefore expires after 1 us when the SysTick CPU clock is selected. */
    qtest_writel(qts, SYST_RVR, 63);
    qtest_writel(qts, SYST_CVR, 0);
    qtest_writel(qts, SYST_CSR,
                 SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE);
    qtest_clock_step(qts, 1000);
    g_assert_true(qtest_readl(qts, SYST_CSR) & SYST_CSR_COUNTFLAG);
    /* SysTick is an internal exception; its pending state is exposed by
     * SCB->ICSR.PENDSTSET, not the external ISPR bank. */
    g_assert_true(qtest_readl(qts, SCB_ICSR) & SCB_ICSR_PENDSTSET);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, SYST_CSR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISER0), ==, 0);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0), ==, 0);

    qtest_quit(qts);
}

static void test_gpio_exti_reaches_native_nvic(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t line_mask = UINT32_C(1) << 15;
    uint32_t irq_mask = UINT32_C(1) << (TEST_EXTI_IRQ % 32);

    qtest_irq_intercept_in(qts, "/machine/armv7m");

    /* EXTI15_10 is wired by the board profile to native NVIC input 40.
     * PA15 starts high (released user key), so driving the board input low
     * must create a falling-edge pending interrupt. */
    qtest_writel(qts, EXTI_FTSR1, line_mask);
    qtest_writel(qts, EXTI_C1IMR1, line_mask);
    qtest_qmp_assert_success(
        qts, "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'gpio-input', 'value': 'A15=0' } }");

    g_assert_true(qtest_get_irq(qts, TEST_EXTI_IRQ));
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR1) & irq_mask, ==, irq_mask);

    qtest_writel(qts, EXTI_PR1, line_mask);
    g_assert_false(qtest_get_irq(qts, TEST_EXTI_IRQ));
    /* Clearing the EXTI source deasserts the level, while NVIC pending is
     * architecturally cleared by ICPR. */
    qtest_writel(qts, NVIC_ICPR1, irq_mask);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR1) & irq_mask, ==, 0);
    qtest_quit(qts);
}

static void test_sysresetreq_latches_software_reset_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==, 0);
    qtest_writel(qts, SCB_AIRCR,
                 SCB_AIRCR_VECTKEY | SCB_AIRCR_SYSRESETREQ);
    /* The qtest MMIO command queues the reset request; a zero virtual-clock
     * step lets the normal runstate path consume it without depending on a
     * QMP event ordering detail. */
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==,
                    RCC_RSR_SFTRSTF);

    /* A subsequent ordinary reset keeps the source until firmware clears it. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==,
                    RCC_RSR_SFTRSTF);
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==, 0);
    qtest_quit(qts);
}

static void test_power_on_reset_latches_por_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==,
                    RCC_RSR_PORRSTF);

    /* A normal system reset is not another power-on event. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==,
                    RCC_RSR_PORRSTF);

    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==, 0);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==, 0);
    qtest_quit(qts);
}

static void test_power_brownout_latches_bor_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02,vin-mv=11999");

    /* A low startup input is not a runtime BOR event. */
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==,
                    RCC_RSR_PORRSTF);

    qtest_qmp_assert_success(
        qts, "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'vin-mv', 'value': '24000' } }");
    qtest_writel(qts, SYST_RVR, 0x1234);
    qtest_qmp_assert_success(
        qts, "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'vin-mv', 'value': '11999' } }");
    /* Let the queued QEMU reset request complete. */
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==,
                    RCC_RSR_BORRSTF);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==,
                    RCC_RSR_BORRSTF);
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==, 0);
    qtest_quit(qts);
}

static void test_external_nrst_latches_pin_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* POR is unrelated to an external pin reset; clear it so the assertion
     * below identifies only the pin producer. */
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    qtest_writel(qts, SYST_RVR, 0x1234);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 1);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 0);
    qtest_clock_step(qts, 0);

    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PINRSTF, ==,
                    RCC_RSR_PINRSTF);

    /* The active-low level is not edge-triggered repeatedly. */
    qtest_writel(qts, SYST_RVR, 0x5678);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 0);
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0x5678);

    /* Releasing and asserting NRST creates a second, real pin event. */
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 1);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 0);
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PINRSTF, ==,
                    RCC_RSR_PINRSTF);
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PINRSTF, ==, 0);
    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/armv7m/qom-children-and-irq-width",
                    test_armv7m_children_and_irq_width);
    g_test_add_func("/dm-mc02/armv7m/external-irq-and-systick-time",
                    test_external_irq_and_systick_virtual_time);
    g_test_add_func("/dm-mc02/armv7m/gpio-exti-reaches-native-nvic",
                    test_gpio_exti_reaches_native_nvic);
    g_test_add_func("/dm-mc02/armv7m/sysresetreq-software-reset-reason",
                    test_sysresetreq_latches_software_reset_reason);
    g_test_add_func("/dm-mc02/armv7m/power-on-reset-reason",
                    test_power_on_reset_latches_por_reason);
    g_test_add_func("/dm-mc02/armv7m/power-brownout-reset-reason",
                    test_power_brownout_latches_bor_reason);
    g_test_add_func("/dm-mc02/armv7m/external-nrst-pin-reset-reason",
                    test_external_nrst_latches_pin_reason);
    return g_test_run();
}
