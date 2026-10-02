/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "dm-mc02-adc-test-common.h"

static void test_injected_software_sequence(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t jsqr = 1u | (0u << ADC_JSQR_JSQ1_SHIFT) |
                    (1u << ADC_JSQR_JSQ2_SHIFT);
    uint32_t isr;
    uint32_t cr;

    /* Two injected ranks use the same board-independent channel source
     * lookup as regular conversion, but are delivered through JDRx. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_JSQR, jsqr);
    g_assert_cmphex(qtest_readl(qts, ADC1_JSQR), ==, jsqr);
    qtest_writel(qts, ADC1_IER, ADC_IER_JEOCIE | ADC_IER_JEOSIE);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_true(cr & ADC_CR_JADSTART);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0);

    /* Reset sampling time is three sample half-cycles plus 33 processing
     * half-cycles: 36 / (2 * 1.5 MHz) = 12000 ns. */
    qtest_clock_step(qts, 11999);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_false(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS));
    qtest_clock_step(qts, 1);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & ADC_ISR_JEOC);
    g_assert_false(isr & ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0);

    /* Reading JDR acknowledges only JEOC. */
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_false(isr & ADC_ISR_JEOC);
    g_assert_false(isr & ADC_ISR_JEOS);

    qtest_clock_step(qts, 11999);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC);
    qtest_clock_step(qts, 1);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS));
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0x0200);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_false(cr & ADC_CR_JADSTART);

    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0x0200);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_false(isr & ADC_ISR_JEOC);
    g_assert_true(isr & ADC_ISR_JEOS);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);

    /* JADSTP stops the injected timer between ranks and preserves completed
     * status/data until software clears the status flags. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_JSQR, jsqr);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTP);
    g_assert_false(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0);

    qtest_quit(qts);
}

static void test_injected_software_jdiscen_full_sequence(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t jsqr = 2u | (0u << ADC_JSQR_JSQ1_SHIFT) |
                    (1u << ADC_JSQR_JSQ2_SHIFT) |
                    (2u << ADC_JSQR_JSQ3_SHIFT);
    uint32_t isr;

    /* JDISCEN only splits externally triggered injected conversions.  A
     * software start with JEXTEN=0 must run every configured rank. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS | ADC_CFGR_JDISCEN);
    qtest_writel(qts, ADC1_JSQR, jsqr);
    g_assert_cmphex(qtest_readl(qts, ADC1_CFGR) &
                    (ADC_CFGR_JQDIS | ADC_CFGR_JDISCEN), ==,
                    ADC_CFGR_JQDIS | ADC_CFGR_JDISCEN);
    g_assert_cmphex(qtest_readl(qts, ADC1_JSQR), ==, jsqr);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);

    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);

    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0x0200);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);

    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC | ADC_ISR_JEOS);
    /* The board-independent fallback only defines values for its historical
     * first two ranks; rank three is a valid zero-valued sample.  JEOS above
     * proves that this rank, rather than an early stop, completed. */
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR3), ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR4), ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART, ==, 0);

    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);
    qtest_quit(qts);
}

static void test_jauto_regular_to_injected(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t isr;

    /* JAUTO uses the injected software-trigger configuration and starts the
     * injected sequence at regular EOS.  JQDIS supplies one active context. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JAUTO | ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_JSQR, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);

    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_EOC | ADC_ISR_EOS), ==,
                    ADC_ISR_EOC | ADC_ISR_EOS);
    g_assert_cmphex(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);

    /* Automatic injection is a separate conversion interval after regular
     * EOS, and the one-shot regular start is already complete. */
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==, 0);
    qtest_clock_step(qts, 1);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC | ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) &
                    (ADC_CR_ADSTART | ADC_CR_JADSTART), ==, 0);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* In continuous mode the next regular sequence is held until the
     * automatically-started injected sequence has completed. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JAUTO | ADC_CFGR_JQDIS |
                 ADC_CFGR_CONT);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_JSQR, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC | ADC_ISR_EOS);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);
    g_assert_false(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOC | ADC_ISR_JEOS);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS, ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);

    qtest_quit(qts);
}

static void test_jauto_disabled_does_not_start_injected(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_JSQR, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0);

    qtest_quit(qts);
}

static void test_jauto_jadstp_resumes_regular(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t isr;

    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JAUTO | ADC_CFGR_JQDIS |
                 ADC_CFGR_CONT);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_JSQR, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);

    /* Abort the automatic injected conversion before it completes. */
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC | ADC_ISR_EOS);
    qtest_writel(qts, ADC1_CR, ADC_CR_JADSTP);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART, ==, 0);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_EOS), ==, 0);
    qtest_clock_step(qts, 1);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_EOC | ADC_ISR_EOS), ==,
                    ADC_ISR_EOC | ADC_ISR_EOS);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JAUTO | ADC_CFGR_JQDIS |
                 ADC_CFGR_CONT | ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_JSQR, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);

    /* JADSTP releases the automatic-injection wait, but AUTDLY still owns
     * the regular data-consumption boundary until DR is read. */
    qtest_writel(qts, ADC1_CR, ADC_CR_JADSTP);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOS);
    qtest_clock_step(qts, 100000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS, ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_EOS), ==, 0);
    qtest_clock_step(qts, 1);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_EOC | ADC_ISR_EOS), ==,
                    ADC_ISR_EOC | ADC_ISR_EOS);

    qtest_quit(qts);
}

static void test_injected_tim8_trigger(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t jsqr = (9u << ADC_JSQR_JEXTSEL_SHIFT) |
                    ADC_JSQR_JEXTEN_RISING;

    /* TIM8 MMS=010 publishes an update event on the board trigger bus. */
    qtest_writel(qts, ADC1_JSQR, jsqr);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, TIM8_EGR, 1u);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC | ADC_ISR_JEOS);
    /* Reading JDR acknowledges JEOC, so observe the flags before consuming
     * the data register. */
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100);

    /* A falling-only injected trigger must reject the same rising update. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    jsqr = (9u << ADC_JSQR_JEXTSEL_SHIFT) | (2u << 7);
    qtest_writel(qts, ADC1_JSQR, jsqr);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 100000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0);

    qtest_quit(qts);
}

static void test_injected_discontinuous_tim8_trigger(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t jsqr = (2u << 0) | (9u << ADC_JSQR_JEXTSEL_SHIFT) |
                    ADC_JSQR_JEXTEN_RISING | (0u << ADC_JSQR_JSQ1_SHIFT) |
                    (1u << ADC_JSQR_JSQ2_SHIFT) |
                    (2u << ADC_JSQR_JSQ3_SHIFT);
    uint32_t isr;

    /* JDISCEN is a one-rank injected subgroup.  The next TIM8 edge resumes
     * the same JSQR context instead of restarting rank 1. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JDISCEN);
    qtest_writel(qts, ADC1_JSQR, jsqr);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);

    qtest_writel(qts, TIM8_EGR, 1u);
    /* A second edge while the first rank is converting is not queued. */
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & ADC_ISR_JEOC, ==, ADC_ISR_JEOC);
    g_assert_cmphex(isr & ADC_ISR_JEOS, ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);

    /* The subgroup boundary waits for another external edge, not elapsed
     * virtual time. */
    qtest_clock_step(qts, 100000);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0);

    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & ADC_ISR_JEOS, ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0x0200);

    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_cmphex(isr & (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC | ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR3), ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART, ==, 0);

    qtest_quit(qts);
}

static void test_tim8_oc1ref_trigger_edges(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t pwm1 = 6u << 4;

    configure_timer_fixture_clock(qts);

    /* H723 TIM8_TRGO=7 is JEXTSEL=9 for the injected ADC.  MMS=100 selects
     * the internal OC1REF waveform; this deliberately leaves CC1E clear to
     * prove that the trigger observes OC1REF before the pin output stage. */
    qtest_writel(qts, ADC1_JSQR, (9u << ADC_JSQR_JEXTSEL_SHIFT) |
                 ADC_JSQR_JEXTEN_RISING);
    qtest_writel(qts, TIM8_CCMR1, pwm1);
    qtest_writel(qts, TIM8_ARR, 31);
    qtest_writel(qts, TIM8_CCR1, 8);
    qtest_writel(qts, TIM8_CR2, 4u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR1, 1u);

    /* Timer clock is 32 MHz: ARR=31 gives a 1 us period and CCR1=8 gives a
     * falling edge at 250 ns.  The first PWM1 rising edge is the update at
     * 1 us. */
    qtest_clock_step(qts, 999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_timer_fixture_clock(qts);

    /* The same OC1REF falling edge must be visible when the ADC selects a
     * falling trigger. */
    qtest_writel(qts, ADC1_JSQR, (9u << ADC_JSQR_JEXTSEL_SHIFT) |
                 ADC_JSQR_JEXTEN_FALLING);
    qtest_writel(qts, TIM8_CCMR1, pwm1);
    qtest_writel(qts, TIM8_ARR, 31);
    qtest_writel(qts, TIM8_CCR1, 8);
    qtest_writel(qts, TIM8_CR2, 4u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR1, 1u);
    qtest_clock_step(qts, 249);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);

    qtest_quit(qts);
}

static void tim8_start_ocref_injected(QTestState *qts, unsigned mode,
                                      unsigned exten, unsigned arr,
                                      unsigned ccr)
{
    /* This helper runs several independent single-context scenarios in one
     * test process.  Select queue-disabled mode explicitly so each JSQR write
     * replaces the prior context instead of becoming pending. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_JSQR, (9u << ADC_JSQR_JEXTSEL_SHIFT) | exten);
    qtest_writel(qts, TIM8_CCMR1, mode << 4);
    qtest_writel(qts, TIM8_ARR, arr);
    qtest_writel(qts, TIM8_CCR1, ccr);
    qtest_writel(qts, TIM8_CR2, 4u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR1, 1u);
}

static void test_tim8_ocref_stateful_compare_modes(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    const unsigned ccr_time_ns = 250;
    const unsigned conversion_ns = 12000;

    configure_timer_fixture_clock(qts);

    /* Frozen mode never changes OC1REF, even though the compare match still
     * exists in the timer and can set CC1IF. */
    tim8_start_ocref_injected(qts, 0, ADC_JSQR_JEXTEN_RISING, 31, 8);
    qtest_clock_step(qts, conversion_ns + ccr_time_ns + 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_timer_fixture_clock(qts);

    /* Active-on-match starts low and produces a rising OCREF edge at CCR1. */
    tim8_start_ocref_injected(qts, 1, ADC_JSQR_JEXTEN_RISING, 31, 8);
    qtest_clock_step(qts, ccr_time_ns - 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, conversion_ns);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_timer_fixture_clock(qts);

    /* Establish a high OCREF level first.  Inactive-on-match then produces a
     * falling edge at CCR1; selecting the mode alone does not invent an edge
     * when its initial level is already low. */
    tim8_start_ocref_injected(qts, 2, ADC_JSQR_JEXTEN_FALLING, 31, 8);
    qtest_writel(qts, TIM8_CCMR1, 5u << 4);
    qtest_writel(qts, TIM8_CCMR1, 2u << 4);
    qtest_clock_step(qts, ccr_time_ns - 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, conversion_ns);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_timer_fixture_clock(qts);

    /* Toggle mode is checked at two distinct compare deadlines.  The 20 us
     * period leaves enough virtual time for the injected conversion to finish
     * before the second edge. */
    tim8_start_ocref_injected(qts, 3, ADC_JSQR_JEXTEN_RISING, 639, 8);
    qtest_clock_step(qts, ccr_time_ns);
    qtest_clock_step(qts, conversion_ns);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOC | ADC_ISR_JEOS);
    qtest_writel(qts, ADC1_JSQR, (9u << ADC_JSQR_JEXTSEL_SHIFT) |
                 ADC_JSQR_JEXTEN_FALLING);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_clock_step(qts, 7999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, conversion_ns);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);

    qtest_quit(qts);
}

static void test_injected_context_queue(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t context_a = (9u << ADC_JSQR_JEXTSEL_SHIFT) |
                         ADC_JSQR_JEXTEN_RISING;
    uint32_t context_b = context_a | (1u << ADC_JSQR_JSQ2_SHIFT) |
                         (1u << 0);
    uint32_t context_c = context_a | (2u << ADC_JSQR_JSQ1_SHIFT);
    uint32_t isr;

    qtest_irq_intercept_in(qts, "/machine/armv7m");

    /* Queue-enabled mode admits one active and one pending JSQR snapshot.
     * Use different lengths so consuming B is observable independently of
     * the queue's JSQR readback value. */
    qtest_writel(qts, ADC1_IER, ADC_IER_JQOVFIE);
    qtest_writel(qts, ADC1_JSQR, context_a);
    qtest_writel(qts, ADC1_JSQR, context_b);
    g_assert_cmphex(qtest_readl(qts, ADC1_JSQR), ==, context_b);
    g_assert_false(qtest_get_irq(qts, 18));

    /* A third admission is rejected without changing either snapshot. */
    qtest_writel(qts, ADC1_JSQR, context_c);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & ADC_ISR_JQOVF);
    g_assert_true(qtest_get_irq(qts, 18));
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JQOVF);
    g_assert_false(qtest_get_irq(qts, 18));

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);

    /* Context A is consumed first.  Completion promotes B and keeps the
     * external-trigger arm set while the queue still has a context. */
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100u);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOC | ADC_ISR_JEOS);

    /* Context B has two ranks; if C had overwritten the pending slot this
     * edge would report JEOS after one rank instead. */
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & ADC_ISR_JEOC);
    g_assert_false(isr & ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100u);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOC);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR2), ==, 0x0200u);
    g_assert_false(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOC | ADC_ISR_JEOS);

    /* JQM=1 is the end-empty policy.  After A completes, the queue is empty,
     * JSQR is cleared, and a later trigger has no context to consume. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQM);
    qtest_writel(qts, ADC1_JSQR, context_a);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_JSQR), ==, 0);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_JADSTART);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_JEOC | ADC_ISR_JEOS);

    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==, 0);

    /* A new full JSQR admission restores the empty queue without requiring a
     * machine reset; the still-armed JADSTART accepts the next edge. */
    qtest_writel(qts, ADC1_JSQR, context_b);
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC, ==,
                    ADC_ISR_JEOC);

    qtest_quit(qts);
}

static void test_tim1_trigger_mux_mapping(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* TIM1_TRGO is EXTSEL=9 for regular conversions. */
    qtest_writel(qts, ADC1_CFGR, (9u << 5) | (1u << 10));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM1_CR2, 2u << 4);
    qtest_writel(qts, TIM1_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* The same physical timer source is JEXTSEL=8 when emitted on TRGO2. */
    qtest_writel(qts, ADC1_JSQR, (8u << ADC_JSQR_JEXTSEL_SHIFT) |
                 ADC_JSQR_JEXTEN_RISING);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM1_CR2, 2u << 20);
    qtest_writel(qts, TIM1_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC | ADC_ISR_JEOS);
    qtest_quit(qts);
}

static void test_tim3_trigger_mux_mapping(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* A different source selection must not accept TIM3_TRGO by accident. */
    qtest_writel(qts, ADC1_CFGR, (7u << 5) | (1u << 10));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM3_CR2, 2u << 4);
    qtest_writel(qts, TIM3_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* TIM3_TRGO is EXTSEL=4 for regular conversions on H723. */
    qtest_writel(qts, ADC1_CFGR, (4u << 5) | (1u << 10));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM3_CR2, 2u << 4);
    qtest_writel(qts, TIM3_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* TIM3_TRGO is JEXTSEL=12 for injected conversions. */
    qtest_writel(qts, ADC1_JSQR, (12u << ADC_JSQR_JEXTSEL_SHIFT) |
                 ADC_JSQR_JEXTEN_RISING);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM3_CR2, 2u << 4);
    qtest_writel(qts, TIM3_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC | ADC_ISR_JEOS);

    qtest_quit(qts);
}

static void tim3_start_oc4ref(QTestState *qts)
{
    /* TIM3 MMS=111 publishes the channel-4 OCREF edge.  The profile maps
     * that physical event to regular EXTSEL=15; the ADC performs the
     * separate injected JEXTSEL=4 mapping. */
    qtest_writel(qts, TIM3_CCMR2, 6u << 12); /* PWM1 on channel 4 */
    qtest_writel(qts, TIM3_ARR, 31);
    qtest_writel(qts, TIM3_CCR4, 8);
    qtest_writel(qts, TIM3_CR2, 7u << 4);
    qtest_writel(qts, TIM3_CR1, 1u);
}

static void test_tim3_oc4ref_trigger_mux_mapping(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    configure_timer_fixture_clock(qts);

    /* TIM3_CH4 must not be confused with the TIM8_TRGO source. */
    qtest_writel(qts, ADC1_CFGR, (7u << 5) | (1u << 10));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    tim3_start_oc4ref(qts);
    qtest_clock_step(qts, 10000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_timer_fixture_clock(qts);

    /* H723 regular ADC trigger: TIM3 channel 4 is EXTSEL=15. */
    qtest_writel(qts, ADC1_CFGR, (15u << 5) | (1u << 10));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    tim3_start_oc4ref(qts);
    qtest_clock_step(qts, 999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_timer_fixture_clock(qts);

    /* The same physical event is injected JEXTSEL=4. */
    qtest_writel(qts, ADC1_JSQR, (4u << ADC_JSQR_JEXTSEL_SHIFT) |
                 ADC_JSQR_JEXTEN_RISING);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    tim3_start_oc4ref(qts);
    qtest_clock_step(qts, 999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS, ==,
                    ADC_ISR_JEOS);

    qtest_quit(qts);
}

static void test_tim2_trigger_mux_mapping(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* TIM2_TRGO is EXTSEL=11 for regular conversions on H723. */
    qtest_writel(qts, ADC1_CFGR, (11u << 5) | (1u << 10));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM2_CR2, 2u << 4);
    qtest_writel(qts, TIM2_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* TIM2_TRGO is JEXTSEL=2 for injected conversions. */
    qtest_writel(qts, ADC1_JSQR, (2u << ADC_JSQR_JEXTSEL_SHIFT) |
                 ADC_JSQR_JEXTEN_RISING);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM2_CR2, 2u << 4);
    qtest_writel(qts, TIM2_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_JEOC | ADC_ISR_JEOS), ==,
                    ADC_ISR_JEOC | ADC_ISR_JEOS);

    qtest_quit(qts);
}

static void test_tim2_compare_pulse_only_cc1(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t adc_trigger = (11u << 5) | (1u << 10);

    /* MMS=011 is the CC1 compare pulse.  A CC2 match at 250 ns must not be
     * forwarded as TIM2_TRGO, even though it uses the same compare timer. */
    qtest_writel(qts, ADC1_CFGR, adc_trigger);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, TIM2_ARR, 31);
    qtest_writel(qts, TIM2_CCR2, 8);
    qtest_writel(qts, TIM2_CR2, 3u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM2_CR1, 1u);
    qtest_clock_step(qts, 7000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_timer_fixture_clock(qts);

    /* With CCR1 configured, the same trigger path starts the conversion at
     * 250 ns and the one-rank conversion completes 12000 ns later. */
    qtest_writel(qts, ADC1_CFGR, adc_trigger);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, TIM2_ARR, 31);
    qtest_writel(qts, TIM2_CCR1, 8);
    qtest_writel(qts, TIM2_CR2, 3u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM2_CR1, 1u);
    qtest_clock_step(qts, 249);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, 11999);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_quit(qts);
}

static void test_tim8_busy_trigger_is_not_queued(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    configure_timer_fixture_clock(qts);

    /* TIM8 update is the selected regular trigger.  The timer period is 1 us,
     * while a reset-sampling rank takes 12000 ns, so several edges arrive
     * while the first conversion is active. */
    qtest_writel(qts, ADC1_CFGR, (7u << 5) | (1u << 10));
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, TIM8_ARR, 31);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM8_CR1, 1u);

    qtest_clock_step(qts, 12999);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100);

    /* Busy external edges are ignored by this regular ADC slice.  Clearing
     * the first result must not reveal a hidden conversion from those edges. */
    qtest_clock_step(qts, 10000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_quit(qts);
}

static void test_regular_discontinuous_external_subgroups(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t cfgr = (7u << 5) | (1u << 10) |
                    ADC_CFGR_DISCEN | ADC_CFGR_DISCNUM_2RANKS;

    /* Four ranks, two ranks per external trigger.  TIM8 UG is the selected
     * update source and each subgroup retains the next rank. */
    qtest_writel(qts, ADC1_CFGR, cfgr);
    qtest_writel(qts, ADC1_SQR1, 3u);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);

    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100u);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);

    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0200u);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);

    /* The next matching edge resumes at rank 3, not rank 1. */
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);

    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADSTART, ==, 0);
    qtest_quit(qts);
}

static void test_regular_discontinuous_autowait_boundary(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t cfgr = (7u << 5) | (1u << 10) |
                    ADC_CFGR_DISCEN | ADC_CFGR_DISCNUM_2RANKS |
                    ADC_CFGR_AUTDLY;
    uint32_t isr;

    /* AUTDLY must hold each result until DR is consumed, while DISCEN must
     * still stop the subgroup after two ranks and wait for another edge. */
    qtest_writel(qts, ADC1_CFGR, cfgr);
    qtest_writel(qts, ADC1_SQR1, 3u);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM8_EGR, 1u);

    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & ADC_ISR_EOC);
    g_assert_false(isr & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100u);

    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & ADC_ISR_EOC);
    g_assert_false(isr & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0200u);

    /* The subgroup is now complete.  No hidden rank may run before the next
     * matching trigger, even though the final DR read released AUTDLY. */
    qtest_clock_step(qts, 100000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    qtest_quit(qts);
}

void dm_mc02_adc_register_trigger_tests(void)
{
    g_test_add_func("/dm-mc02/adc/injected-software-sequence",
                    test_injected_software_sequence);
    g_test_add_func("/dm-mc02/adc/injected-software-jdiscen-full-sequence",
                    test_injected_software_jdiscen_full_sequence);
    g_test_add_func("/dm-mc02/adc/jauto-regular-to-injected",
                    test_jauto_regular_to_injected);
    g_test_add_func("/dm-mc02/adc/jauto-disabled-no-injected",
                    test_jauto_disabled_does_not_start_injected);
    g_test_add_func("/dm-mc02/adc/jauto-jadstp-resumes-regular",
                    test_jauto_jadstp_resumes_regular);
    g_test_add_func("/dm-mc02/adc/injected-tim8-trigger",
                    test_injected_tim8_trigger);
    g_test_add_func("/dm-mc02/adc/injected-discontinuous-tim8-trigger",
                    test_injected_discontinuous_tim8_trigger);
    g_test_add_func("/dm-mc02/adc/injected-context-queue",
                    test_injected_context_queue);
    g_test_add_func("/dm-mc02/adc/tim8-oc1ref-trigger-edges",
                    test_tim8_oc1ref_trigger_edges);
    g_test_add_func("/dm-mc02/adc/tim8-ocref-stateful-compare-modes",
                    test_tim8_ocref_stateful_compare_modes);
    g_test_add_func("/dm-mc02/adc/tim1-trigger-mux-mapping",
                    test_tim1_trigger_mux_mapping);
    g_test_add_func("/dm-mc02/adc/tim3-trigger-mux-mapping",
                    test_tim3_trigger_mux_mapping);
    g_test_add_func("/dm-mc02/adc/tim3-oc4ref-trigger-mux-mapping",
                    test_tim3_oc4ref_trigger_mux_mapping);
    g_test_add_func("/dm-mc02/adc/tim2-trigger-mux-mapping",
                    test_tim2_trigger_mux_mapping);
    g_test_add_func("/dm-mc02/adc/tim2-compare-pulse-only-cc1",
                    test_tim2_compare_pulse_only_cc1);
    g_test_add_func("/dm-mc02/adc/tim8-busy-trigger-not-queued",
                    test_tim8_busy_trigger_is_not_queued);
    g_test_add_func("/dm-mc02/adc/regular-discontinuous-external-subgroups",
                    test_regular_discontinuous_external_subgroups);
    g_test_add_func("/dm-mc02/adc/regular-discontinuous-autowait-boundary",
                    test_regular_discontinuous_autowait_boundary);
}
