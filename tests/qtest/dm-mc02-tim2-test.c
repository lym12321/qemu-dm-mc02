/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"
#include "qapi/qmp/qdict.h"
#include "hw/arm/dm_mc02_tim2.h"

#define TIM2_BASE       0x40000000
#define TIM2_CR1        (TIM2_BASE + 0x00)
#define TIM2_CR2        (TIM2_BASE + 0x04)
#define TIM2_CCMR1      (TIM2_BASE + 0x18)
#define TIM2_DIER       (TIM2_BASE + 0x0c)
#define TIM2_SR         (TIM2_BASE + 0x10)
#define TIM2_EGR        (TIM2_BASE + 0x14)
#define TIM2_CNT        (TIM2_BASE + 0x24)
#define TIM2_PSC        (TIM2_BASE + 0x28)
#define TIM2_ARR        (TIM2_BASE + 0x2c)
#define TIM2_CCR1       (TIM2_BASE + 0x34)
#define TIM2_CCR2       (TIM2_BASE + 0x38)
#define TIM2_CCR3       (TIM2_BASE + 0x3c)
#define TIM2_CCR4       (TIM2_BASE + 0x40)
#define TIM8_BASE       0x40010400
#define TIM8_CR1        (TIM8_BASE + 0x00)
#define TIM8_DIER       (TIM8_BASE + 0x0c)
#define TIM8_SR         (TIM8_BASE + 0x10)
#define TIM8_EGR        (TIM8_BASE + 0x14)
#define TIM8_CNT        (TIM8_BASE + 0x24)
#define TIM8_PSC        (TIM8_BASE + 0x28)
#define TIM8_ARR        (TIM8_BASE + 0x2c)
#define TIM8_RCR        (TIM8_BASE + 0x30)
#define TIM8_CCR1       (TIM8_BASE + 0x34)
#define TIM2_BDTR       (TIM2_BASE + 0x44)
#define TIM8_BDTR       (TIM8_BASE + 0x44)
#define TIM1_BASE       0x40010000
#define TIM1_CR1        (TIM1_BASE + 0x00)
#define TIM1_CCMR1      (TIM1_BASE + 0x18)
#define TIM1_CCER       (TIM1_BASE + 0x20)
#define TIM1_PSC        (TIM1_BASE + 0x28)
#define TIM1_ARR        (TIM1_BASE + 0x2c)
#define TIM1_CCR1       (TIM1_BASE + 0x34)
#define TIM1_BDTR       (TIM1_BASE + 0x44)
#define RCC_BASE        0x58024400
#define RCC_D1CFGR      (RCC_BASE + 0x18)
#define RCC_D2CFGR      (RCC_BASE + 0x1c)
#define ADC1_BASE       0x40022000
#define ADC1_ISR        (ADC1_BASE + 0x00)
#define ADC1_CR         (ADC1_BASE + 0x08)
#define ADC1_CFGR       (ADC1_BASE + 0x0c)

#define TIM_CR1_CEN     (1u << 0)
#define TIM_CR1_CMS_1   (1u << 5)
#define TIM_CR1_CMS_11  (3u << 5)
#define TIM_CR1_ARPE    (1u << 7)
#define TIM_CCMR1_CC1S_INPUT (1u << 0)
#define TIM_CCMR1_OC1PE (1u << 3)
#define TIM_DIER_CC1IE  (1u << 1)
#define TIM_DIER_CC2IE  (1u << 2)
#define TIM_DIER_CC3IE  (1u << 3)
#define TIM_DIER_CC4IE  (1u << 4)
#define TIM_SR_UIF      (1u << 0)
#define TIM_SR_CC1IF    (1u << 1)
#define TIM_SR_CC2IF    (1u << 2)
#define TIM_SR_CC3IF    (1u << 3)
#define TIM_SR_CC4IF    (1u << 4)
#define TIM_EGR_UG      (1u << 0)
#define TIM_BDTR_BKE    (1u << 12)
#define TIM_BDTR_BKP    (1u << 13)
#define TIM_BDTR_AOE    (1u << 14)
#define TIM_BDTR_MOE    (1u << 15)
#define TIM_CR1_CKD_SHIFT 8
#define TIM_CR1_CKD_MASK  (3u << TIM_CR1_CKD_SHIFT)
#define ADC_ISR_EOC     (1u << 2)
#define ADC_CR_ADEN     (1u << 0)
#define ADC_CR_ADSTART  (1u << 2)
#define ADC_CFGR_EXTSEL_SHIFT 5
#define ADC_CFGR_EXTEN_SHIFT 10

static QTestState *tim2_start(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* Keep the historical timer fixture at 32 MHz while exercising timer
     * behavior.  The machine reset clock is HCLK at 64 MHz; this explicitly
     * programs the real board's HCLK=/2, APB1/APB2=/2 arrangement. */
    qtest_writel(qts, RCC_D1CFGR, 0x8);
    qtest_writel(qts, RCC_D2CFGR, 0x440);
    return qts;
}

static void tim2_configure_fixture_clock(QTestState *qts)
{
    qtest_writel(qts, RCC_D1CFGR, 0x8);
    qtest_writel(qts, RCC_D2CFGR, 0x440);
}

static void tim2_configure(QTestState *qts, uint32_t psc, uint32_t arr)
{
    qtest_writel(qts, TIM2_PSC, psc);
    qtest_writel(qts, TIM2_ARR, arr);
    qtest_writel(qts, TIM2_SR, 0);
}

static void test_counter_freezes_when_disabled(void)
{
    QTestState *qts = tim2_start();

    qtest_writel(qts, TIM2_CNT, 123);
    qtest_clock_step(qts, 1000000);

    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 123);
    qtest_quit(qts);
}

static void test_dead_time_decoder_boundaries(void)
{
    static const struct {
        uint32_t dtg;
        uint32_t ticks;
    } cases[] = {
        { 0x00, 0 },
        { 0x7f, 127 },
        { 0x80, 128 },
        { 0xbf, 254 },
        { 0xc0, 256 },
        { 0xdf, 504 },
        { 0xe0, 512 },
        { 0xff, 1008 },
    };

    for (size_t i = 0; i < ARRAY_SIZE(cases); ++i) {
        g_assert_cmpuint(dm_mc02_tim2_decode_dead_time_ticks(cases[i].dtg),
                         ==, cases[i].ticks);
    }
}

static void test_tim8_ckd_does_not_divide_counter(void)
{
    for (unsigned ckd = 0; ckd < 3; ++ckd) {
        QTestState *qts = tim2_start();

        qtest_writel(qts, TIM8_PSC, 3);
        qtest_writel(qts, TIM8_ARR, 7);
        qtest_writel(qts, TIM8_CR1,
                     (ckd << TIM_CR1_CKD_SHIFT) | TIM_CR1_CEN);
        qtest_clock_step(qts, 500);
        g_assert_cmphex(qtest_readl(qts, TIM8_CNT), ==, 4);
        qtest_clock_step(qts, 500);
        g_assert_cmphex(qtest_readl(qts, TIM8_CNT), ==, 0);
        g_assert_true(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);
        qtest_quit(qts);
    }
}

static char *qom_get_string(QTestState *qts, const char *property)
{
    QDict *response;
    char *value;

    response = qtest_qmp(qts, "{ 'execute': 'qom-get', 'arguments': "
                         "{ 'path': '/machine', 'property': %s } }",
                         property);
    g_assert_true(qdict_haskey(response, "return"));
    value = g_strdup(qdict_get_str(response, "return"));
    qobject_unref(response);
    return value;
}

static void tim_configure_pwm1(QTestState *qts, hwaddr base, uint32_t ckd)
{
    qtest_writel(qts, base + 0x28, 0);
    qtest_writel(qts, base + 0x2c, 31);
    qtest_writel(qts, base + 0x34, 16);
    qtest_writel(qts, base + 0x18, 6u << 4);
    qtest_writel(qts, base + 0x20, (1u << 0) | (1u << 2));
    qtest_writel(qts, base + 0x44, 2u | TIM_BDTR_MOE);
    qtest_writel(qts, base + 0x00,
                 (ckd << TIM_CR1_CKD_SHIFT) | TIM_CR1_CEN);
}

static void test_tim1_tim8_complementary_dead_time(void)
{
    QTestState *qts = tim2_start();
    char *tim1;
    char *tim8;

    tim_configure_pwm1(qts, TIM1_BASE, 0);
    tim_configure_pwm1(qts, TIM8_BASE, 0);
    tim1 = qom_get_string(qts, "tim1-ch1-output");
    tim8 = qom_get_string(qts, "tim8-ch1-output");
    g_assert_nonnull(strstr(tim1, "main_enabled=1,main_level=0"));
    g_assert_nonnull(strstr(tim1, "n_enabled=1,n_level=0"));
    g_assert_nonnull(strstr(tim1, "dead_time_ticks=2,dead_time_cycles=2"));
    g_assert_nonnull(strstr(tim8, "main_enabled=1,main_level=0"));
    g_assert_nonnull(strstr(tim8, "n_enabled=1,n_level=0"));
    g_assert_nonnull(strstr(tim8, "dead_time_ticks=2,dead_time_cycles=2"));
    g_free(tim1);
    g_free(tim8);

    /* Two 32 MHz timer input cycles are the programmed dead time. */
    qtest_clock_step(qts, 84);
    tim1 = qom_get_string(qts, "tim1-ch1-output");
    tim8 = qom_get_string(qts, "tim8-ch1-output");
    g_assert_nonnull(strstr(tim1, "main_enabled=1,main_level=1"));
    g_assert_nonnull(strstr(tim1, "n_enabled=1,n_level=0"));
    g_assert_nonnull(strstr(tim8, "main_enabled=1,main_level=1"));
    g_assert_nonnull(strstr(tim8, "n_enabled=1,n_level=0"));
    g_free(tim1);
    g_free(tim8);

    /* Falling OCREF turns the main side off immediately and delays the
     * complementary turn-on by the same interval. */
    qtest_clock_step(qts, 416);
    tim8 = qom_get_string(qts, "tim8-ch1-output");
    g_assert_nonnull(strstr(tim8, "main_enabled=1,main_level=0"));
    g_assert_nonnull(strstr(tim8, "n_enabled=1,n_level=0"));
    g_free(tim8);
    qtest_clock_step(qts, 84);
    tim8 = qom_get_string(qts, "tim8-ch1-output");
    g_assert_nonnull(strstr(tim8, "main_enabled=1,main_level=0"));
    g_assert_nonnull(strstr(tim8, "n_enabled=1,n_level=1"));
    g_free(tim8);

    /* CKD changes only tDTS.  Four input cycles are required with CKD=01. */
    qtest_writel(qts, TIM8_CR1,
                 (1u << TIM_CR1_CKD_SHIFT) | TIM_CR1_CEN);
    qtest_writel(qts, TIM8_CNT, 0);
    qtest_clock_step(qts, 124);
    tim8 = qom_get_string(qts, "tim8-ch1-output");
    g_assert_nonnull(strstr(tim8, "dead_time_ticks=2,dead_time_cycles=4"));
    g_assert_nonnull(strstr(tim8, "main_level=0"));
    g_free(tim8);
    qtest_clock_step(qts, 1);
    tim8 = qom_get_string(qts, "tim8-ch1-output");
    g_assert_nonnull(strstr(tim8, "main_level=1"));
    g_free(tim8);

    qtest_writel(qts, TIM8_BDTR, TIM_BDTR_BKE | TIM_BDTR_BKP | TIM_BDTR_MOE);
    tim8 = qom_get_string(qts, "tim8-ch1-output");
    g_assert_nonnull(strstr(tim8, "main_enabled=0"));
    g_assert_nonnull(strstr(tim8, "n_enabled=0"));
    g_free(tim8);
    qtest_quit(qts);
}

static void test_tim1_forced_ocref_output(void)
{
    QTestState *qts = tim2_start();
    char *output;

    /* OC1M=101 is forced active.  The output observation API must expose
     * this constant OCREF just like PWM1/PWM2, including the N output. */
    qtest_writel(qts, TIM1_CCMR1, 5u << 4);
    qtest_writel(qts, TIM1_CCER, (1u << 0) | (1u << 2));
    qtest_writel(qts, TIM1_BDTR, TIM_BDTR_MOE);
    qtest_writel(qts, TIM1_CR1, TIM_CR1_CEN);
    output = qom_get_string(qts, "tim1-ch1-output");
    g_assert_nonnull(strstr(output, "main_enabled=1,main_level=1"));
    g_assert_nonnull(strstr(output, "n_enabled=1,n_level=0"));
    g_free(output);

    /* OC1M=100 is forced inactive and reverses the two logical gates. */
    qtest_writel(qts, TIM1_CCMR1, 4u << 4);
    output = qom_get_string(qts, "tim1-ch1-output");
    g_assert_nonnull(strstr(output, "main_enabled=1,main_level=0"));
    g_assert_nonnull(strstr(output, "n_enabled=1,n_level=1"));
    g_free(output);
    qtest_quit(qts);
}

static void test_tim1_extended_ocref_mode_is_not_aliased(void)
{
    QTestState *qts = tim2_start();
    char *output;

    /* H723 TIM1 encodes OC1M[3] at bit 16.  Mode 12 is not implemented by
     * this compact model and must not alias to mode 4 (forced inactive). */
    qtest_writel(qts, TIM1_CCMR1, (1u << 16) | (4u << 4));
    qtest_writel(qts, TIM1_CCER, 1u);
    qtest_writel(qts, TIM1_BDTR, TIM_BDTR_MOE);
    qtest_writel(qts, TIM1_CR1, TIM_CR1_CEN);
    output = qom_get_string(qts, "tim1-ch1-output");
    g_assert_cmpstr(output, ==, "unsupported");
    g_free(output);

    qtest_quit(qts);
}

static void test_update_period_sets_uif(void)
{
    QTestState *qts = tim2_start();

    /* (PSC + 1) * (ARR + 1) / 32 MHz = 1 us. */
    tim2_configure(qts, 3, 7);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);

    qtest_clock_step(qts, 999);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 7);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_UIF);

    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 0);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_UIF);

    /* UIF is latched; clear it and verify the next complete period. */
    qtest_writel(qts, TIM2_SR, 0);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_UIF);
    qtest_clock_step(qts, 1000);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_UIF);

    qtest_quit(qts);
}

static void test_live_psc_arr_write_preserves_counter(void)
{
    QTestState *qts = tim2_start();

    tim2_configure(qts, 0, 99);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, 500);

    /* 500 ns at 32 MHz is exactly 16 timer input ticks. */
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 16);

    /* ARR is immediate with ARPE clear, while PSC remains shadowed until an
     * update event. Both writes preserve the current counter phase. */
    qtest_writel(qts, TIM2_PSC, 3);
    qtest_writel(qts, TIM2_ARR, 199);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 16);

    qtest_clock_step(qts, 1000);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 48);

    /* The active prescaler is transferred at the next update. */
    qtest_clock_step(qts, 4750);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_UIF);
    qtest_writel(qts, TIM2_SR, 0);
    qtest_clock_step(qts, 1000);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 8);

    qtest_quit(qts);
}

static void test_apb_timer_domains_are_independent(void)
{
    QTestState *qts = tim2_start();

    /* Both domains start at 32 MHz in the explicit fixture. */
    qtest_writel(qts, TIM2_PSC, 0);
    qtest_writel(qts, TIM2_ARR, 99);
    qtest_writel(qts, TIM8_PSC, 0);
    qtest_writel(qts, TIM8_ARR, 99);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_writel(qts, TIM8_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, 500);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 16);
    g_assert_cmphex(qtest_readl(qts, TIM8_CNT), ==, 16);

    /* APB1 moves from /2 to /4.  With TIMPRE=0, TIM2 becomes 16 MHz while
     * APB2 and TIM8 remain at 32 MHz.  The counters retain their phase at
     * the live clock transition. */
    qtest_writel(qts, RCC_D2CFGR, 0x450);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 16);
    g_assert_cmphex(qtest_readl(qts, TIM8_CNT), ==, 16);
    qtest_clock_step(qts, 500);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 24);
    g_assert_cmphex(qtest_readl(qts, TIM8_CNT), ==, 32);

    qtest_quit(qts);
}

static void test_cc2_compare_flag_and_irq(void)
{
    QTestState *qts = tim2_start();

    qtest_irq_intercept_in(qts, "/machine/armv7m");
    tim2_configure(qts, 3, 7);
    qtest_writel(qts, TIM2_CCR2, 4);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC2IE);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);

    qtest_clock_step(qts, 499);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC2IF);
    g_assert_false(qtest_get_irq(qts, 28));
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC2IF);
    g_assert_true(qtest_get_irq(qts, 28));

    qtest_writel(qts, TIM2_SR, 0);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC2IF);
    g_assert_false(qtest_get_irq(qts, 28));
    qtest_quit(qts);
}

static void test_cc1_cc2_compare_same_and_different_phase(void)
{
    QTestState *qts = tim2_start();

    tim2_configure(qts, 3, 7);
    qtest_writel(qts, TIM2_CCR1, 4);
    qtest_writel(qts, TIM2_CCR2, 4);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC1IE | TIM_DIER_CC2IE);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, 500);
    g_assert_cmphex(qtest_readl(qts, TIM2_SR) &
                    (TIM_SR_CC1IF | TIM_SR_CC2IF), ==,
                    TIM_SR_CC1IF | TIM_SR_CC2IF);
    qtest_writel(qts, TIM2_SR, 0);

    qtest_writel(qts, TIM2_CR1, 0);
    qtest_writel(qts, TIM2_CNT, 0);
    qtest_writel(qts, TIM2_CCR1, 2);
    qtest_writel(qts, TIM2_CCR2, 6);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, 250);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC2IF);
    qtest_writel(qts, TIM2_SR, 0);
    qtest_clock_step(qts, 500);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC2IF);
    qtest_quit(qts);
}

static void test_stateful_mixed_compare_keeps_channel_phase(void)
{
    QTestState *qts = tim2_start();

    tim2_configure(qts, 3, 7);
    /* CC1 active-on-match and CC2 toggle are deliberately phase-shifted. */
    qtest_writel(qts, TIM2_CCMR1, (1u << 4) | (3u << 12));
    qtest_writel(qts, TIM2_CCR1, 2);
    qtest_writel(qts, TIM2_CCR2, 6);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC1IE | TIM_DIER_CC2IE);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);

    qtest_clock_step(qts, 250);
    g_assert_cmphex(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF, ==,
                    TIM_SR_CC1IF);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC2IF);

    qtest_writel(qts, TIM2_SR, 0);
    qtest_clock_step(qts, 500);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC2IF);

    qtest_quit(qts);
}

static void test_center_aligned_counter_and_compare(void)
{
    QTestState *qts = tim2_start();

    /* PSC=3 gives one counter tick per 125 ns on the dm-mc02 timer clock.
     * CMS=01 selects the reusable center-aligned triangular phase. */
    tim2_configure(qts, 3, 7);
    qtest_writel(qts, TIM2_CCMR1, 6u << 4);
    qtest_writel(qts, TIM2_CCR1, 3);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC1IE | (1u << 0));
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CMS_11 | TIM_CR1_CEN);

    qtest_clock_step(qts, 250);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 2);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);

    qtest_clock_step(qts, 125);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 3);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    qtest_writel(qts, TIM2_SR, 0);

    qtest_clock_step(qts, 500);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 7);
    qtest_clock_step(qts, 125);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 6);
    qtest_clock_step(qts, 375);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 3);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    qtest_writel(qts, TIM2_SR, 0);

    qtest_clock_step(qts, 375);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 0);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_UIF);
    qtest_quit(qts);
}

static void test_center_aligned_cms_direction_filter(void)
{
    QTestState *qts = tim2_start();

    tim2_configure(qts, 3, 7);
    qtest_writel(qts, TIM2_CCMR1, 6u << 4);
    qtest_writel(qts, TIM2_CCR1, 3);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC1IE);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CMS_1 | TIM_CR1_CEN);

    /* CMS=01 reports compare matches only while counting down. */
    qtest_clock_step(qts, 375);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 3);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    qtest_clock_step(qts, 1000);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 3);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    qtest_quit(qts);
}

static void tim2_arm_adc_on_trgo(QTestState *qts, bool rising)
{
    uint32_t exten = rising ? 1u : 2u;

    /* TIM2_TRGO is regular ADC EXTSEL=11.  The ADC is only used here as the
     * machine's existing observable sink for the timer's internal OC1REF. */
    qtest_writel(qts, ADC1_CFGR,
                 (11u << ADC_CFGR_EXTSEL_SHIFT) |
                 (exten << ADC_CFGR_EXTEN_SHIFT));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
}

static void test_stateful_ocref_compare_modes(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    const unsigned compare_ns = 250;
    const unsigned conversion_ns = 12000;

    tim2_configure_fixture_clock(qts);

    /* OC1M=001: OC1REF starts low and becomes high at CCR1. */
    tim2_arm_adc_on_trgo(qts, true);
    tim2_configure(qts, 0, 31);
    qtest_writel(qts, TIM2_CCMR1, 1u << 4);
    qtest_writel(qts, TIM2_CCR1, 8);
    qtest_writel(qts, TIM2_CR2, 4u << 4);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, compare_ns - 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, conversion_ns);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* OC1M=010: establish the internal level high, then verify the falling
     * edge at the compare deadline.  This does not require CC1E. */
    qtest_writel(qts, TIM2_CCMR1, 5u << 4);
    qtest_writel(qts, TIM2_CCMR1, 2u << 4);
    tim2_arm_adc_on_trgo(qts, false);
    tim2_configure(qts, 0, 31);
    qtest_writel(qts, TIM2_CCR1, 8);
    qtest_writel(qts, TIM2_CR2, 4u << 4);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, compare_ns - 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, conversion_ns);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* OC1M=011: two compare deadlines must produce alternating edges. */
    tim2_arm_adc_on_trgo(qts, true);
    /* Keep the second toggle beyond the 16-bit ADC rank deadline. */
    tim2_configure(qts, 0, 639);
    qtest_writel(qts, TIM2_CCMR1, 3u << 4);
    qtest_writel(qts, TIM2_CCR1, 8);
    qtest_writel(qts, TIM2_CR2, 4u << 4);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, compare_ns);
    qtest_clock_step(qts, conversion_ns);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);
    tim2_arm_adc_on_trgo(qts, false);
    qtest_clock_step(qts, 20000 - conversion_ns - 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    qtest_clock_step(qts, 1);
    qtest_clock_step(qts, conversion_ns);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_cc3_cc4_compare_flag_and_irq(void)
{
    QTestState *qts = tim2_start();

    qtest_irq_intercept_in(qts, "/machine/armv7m");
    tim2_configure(qts, 3, 7);
    qtest_writel(qts, TIM2_CCR3, 2);
    qtest_writel(qts, TIM2_CCR4, 6);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC3IE);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);

    qtest_clock_step(qts, 249);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC3IF);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC4IF);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC3IF);
    g_assert_true(qtest_get_irq(qts, 28));

    qtest_writel(qts, TIM2_SR, 0);
    g_assert_false(qtest_get_irq(qts, 28));
    qtest_clock_step(qts, 500);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC3IF);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC4IF);
    g_assert_false(qtest_get_irq(qts, 28));

    qtest_writel(qts, TIM2_SR, 0);
    qtest_writel(qts, TIM2_CR1, 0);
    qtest_writel(qts, TIM2_CNT, 0);
    qtest_writel(qts, TIM2_CCR3, 4);
    qtest_writel(qts, TIM2_CCR4, 4);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC3IE | TIM_DIER_CC4IE);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, 500);
    g_assert_cmphex(qtest_readl(qts, TIM2_SR) &
                    (TIM_SR_CC3IF | TIM_SR_CC4IF), ==,
                    TIM_SR_CC3IF | TIM_SR_CC4IF);

    qtest_quit(qts);
}

static void test_input_capture_does_not_schedule_compare(void)
{
    QTestState *qts = tim2_start();

    qtest_irq_intercept_in(qts, "/machine/armv7m");
    tim2_configure(qts, 3, 7);
    qtest_writel(qts, TIM2_CCMR1, TIM_CCMR1_CC1S_INPUT);
    qtest_writel(qts, TIM2_CCR1, 4);
    qtest_writel(qts, TIM2_DIER, TIM_DIER_CC1IE);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, 1000);

    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    g_assert_false(qtest_get_irq(qts, 28));
    qtest_quit(qts);
}

static void test_egr_nonzero_lane_does_not_generate_ug(void)
{
    QTestState *qts = tim2_start();

    qtest_writel(qts, TIM2_SR, TIM_SR_UIF);
    qtest_writeb(qts, TIM2_EGR + 1, 1);
    g_assert_cmphex(qtest_readl(qts, TIM2_SR) & TIM_SR_UIF, ==, 0);
    qtest_quit(qts);
}

static void test_arr_and_ccr_preload_transfer(void)
{
    QTestState *qts = tim2_start();

    tim2_configure(qts, 0, 31);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_ARPE);
    qtest_writel(qts, TIM2_ARR, 63);
    qtest_writel(qts, TIM2_CNT, 16);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_ARPE | TIM_CR1_CEN);
    qtest_clock_step(qts, 500);

    /* ARR=63 is shadowed; the active period remains 32 ticks. */
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 0);
    qtest_writel(qts, TIM2_SR, 0);
    qtest_clock_step(qts, 1000);
    g_assert_cmphex(qtest_readl(qts, TIM2_CNT), ==, 32);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    tim2_configure_fixture_clock(qts);
    tim2_configure(qts, 0, 31);
    qtest_writel(qts, TIM2_CCMR1, TIM_CCMR1_OC1PE | (6u << 4));
    qtest_writel(qts, TIM2_CCR1, 8);
    qtest_writel(qts, TIM2_CR1, TIM_CR1_CEN);
    qtest_clock_step(qts, 500);

    /* OC1PE keeps the old active CCR=0 until UG; the CPU read still returns
     * the shadow value written above. */
    g_assert_cmphex(qtest_readl(qts, TIM2_CCR1), ==, 8);
    qtest_writel(qts, TIM2_EGR, 1);
    qtest_clock_step(qts, 249);
    g_assert_false(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, TIM2_SR) & TIM_SR_CC1IF);
    qtest_quit(qts);
}

static void test_tim2_bdtr_write_is_ignored(void)
{
    QTestState *qts = tim2_start();

    qtest_writel(qts, TIM2_BDTR,
                 TIM_BDTR_BKE | TIM_BDTR_BKP | TIM_BDTR_AOE |
                 TIM_BDTR_MOE);
    g_assert_cmphex(qtest_readl(qts, TIM2_BDTR), ==, 0);

    qtest_quit(qts);
}

static void test_tim8_bdtr_break_clears_and_update_restores_moe(void)
{
    QTestState *qts = tim2_start();
    uint32_t bdtr;

    /* Reset BKIN is high.  With BKP=1, enabling break asserts active-high
     * break immediately and clears MOE. */
    qtest_writel(qts, TIM8_BDTR,
                 TIM_BDTR_BKE | TIM_BDTR_BKP | TIM_BDTR_MOE);
    bdtr = qtest_readl(qts, TIM8_BDTR);
    g_assert_true(bdtr & TIM_BDTR_BKE);
    g_assert_true(bdtr & TIM_BDTR_BKP);
    g_assert_false(bdtr & TIM_BDTR_MOE);

    /* Release the latched break by selecting active-low BKIN.  AOE defers
     * MOE recovery until an update event; changing polarity alone is not
     * sufficient. */
    qtest_writel(qts, TIM8_BDTR, TIM_BDTR_BKE | TIM_BDTR_AOE);
    bdtr = qtest_readl(qts, TIM8_BDTR);
    g_assert_true(bdtr & TIM_BDTR_BKE);
    g_assert_false(bdtr & TIM_BDTR_BKP);
    g_assert_true(bdtr & TIM_BDTR_AOE);
    g_assert_false(bdtr & TIM_BDTR_MOE);

    qtest_writel(qts, TIM8_EGR, TIM_EGR_UG);
    g_assert_true(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);
    g_assert_true(qtest_readl(qts, TIM8_BDTR) & TIM_BDTR_MOE);

    qtest_quit(qts);
}

static void test_tim8_rcr_gates_update_flag_only(void)
{
    QTestState *qts = tim2_start();

    /* PSC=3 and ARR=7 produce one natural update boundary every 1000 ns. */
    qtest_writel(qts, TIM8_PSC, 3);
    qtest_writel(qts, TIM8_ARR, 7);
    qtest_writel(qts, TIM8_CCR1, 4);
    qtest_writel(qts, TIM8_RCR, 2);
    g_assert_cmphex(qtest_readl(qts, TIM8_RCR), ==, 2);
    qtest_writel(qts, TIM8_SR, 0);
    qtest_writel(qts, TIM8_CR1, TIM_CR1_CEN);

    /* CC1IF remains a per-cycle event, independently of the repetition
     * counter.  The first compare is halfway to the first update boundary. */
    qtest_clock_step(qts, 500);
    g_assert_true(qtest_readl(qts, TIM8_SR) & TIM_SR_CC1IF);
    g_assert_false(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);
    qtest_writel(qts, TIM8_SR, 0);

    /* REP=2 suppresses UIF on the first two natural update boundaries. */
    qtest_clock_step(qts, 500);
    g_assert_false(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);

    qtest_clock_step(qts, 500);
    g_assert_true(qtest_readl(qts, TIM8_SR) & TIM_SR_CC1IF);
    g_assert_false(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);
    qtest_writel(qts, TIM8_SR, 0);
    qtest_clock_step(qts, 500);
    g_assert_false(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);

    qtest_clock_step(qts, 500);
    g_assert_true(qtest_readl(qts, TIM8_SR) & TIM_SR_CC1IF);
    g_assert_false(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);
    qtest_writel(qts, TIM8_SR, 0);

    /* The third boundary is the first one allowed to set UIF. */
    qtest_clock_step(qts, 500);
    g_assert_true(qtest_readl(qts, TIM8_SR) & TIM_SR_UIF);

    /* RCR is reset state, not retained configuration. */
    qtest_writel(qts, TIM8_RCR, 2);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, TIM8_RCR), ==, 0);

    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/tim2/counter-freezes-disabled",
                    test_counter_freezes_when_disabled);
    g_test_add_func("/dm-mc02/tim2/dead-time-decoder-boundaries",
                    test_dead_time_decoder_boundaries);
    g_test_add_func("/dm-mc02/tim8/ckd-does-not-divide-counter",
                    test_tim8_ckd_does_not_divide_counter);
    g_test_add_func("/dm-mc02/tim1-tim8/complementary-dead-time",
                    test_tim1_tim8_complementary_dead_time);
    g_test_add_func("/dm-mc02/tim1/forced-ocref-output",
                    test_tim1_forced_ocref_output);
    g_test_add_func("/dm-mc02/tim1/extended-ocref-mode-no-alias",
                    test_tim1_extended_ocref_mode_is_not_aliased);
    g_test_add_func("/dm-mc02/tim2/update-period-uif",
                    test_update_period_sets_uif);
    g_test_add_func("/dm-mc02/tim2/live-psc-arr-preserves-counter",
                    test_live_psc_arr_write_preserves_counter);
    g_test_add_func("/dm-mc02/tim2/apb-timer-domains-independent",
                    test_apb_timer_domains_are_independent);
    g_test_add_func("/dm-mc02/tim2/cc2-compare-flag-irq",
                    test_cc2_compare_flag_and_irq);
    g_test_add_func("/dm-mc02/tim2/cc1-cc2-phase",
                    test_cc1_cc2_compare_same_and_different_phase);
    g_test_add_func("/dm-mc02/tim2/stateful-mixed-compare-phase",
                    test_stateful_mixed_compare_keeps_channel_phase);
    g_test_add_func("/dm-mc02/tim2/center-aligned-counter-compare",
                    test_center_aligned_counter_and_compare);
    g_test_add_func("/dm-mc02/tim2/center-aligned-cms-filter",
                    test_center_aligned_cms_direction_filter);
    g_test_add_func("/dm-mc02/tim2/stateful-ocref-compare-modes",
                    test_stateful_ocref_compare_modes);
    g_test_add_func("/dm-mc02/tim2/cc3-cc4-flag-irq",
                    test_cc3_cc4_compare_flag_and_irq);
    g_test_add_func("/dm-mc02/tim2/input-capture-no-compare",
                    test_input_capture_does_not_schedule_compare);
    g_test_add_func("/dm-mc02/tim2/egr-nonzero-lane-no-ug",
                    test_egr_nonzero_lane_does_not_generate_ug);
    g_test_add_func("/dm-mc02/tim2/arr-ccr-preload-transfer",
                    test_arr_and_ccr_preload_transfer);
    g_test_add_func("/dm-mc02/tim2/bdtr-write-ignored",
                    test_tim2_bdtr_write_is_ignored);
    g_test_add_func("/dm-mc02/tim8/bdtr-break-moe-recovery",
                    test_tim8_bdtr_break_clears_and_update_restores_moe);
    g_test_add_func("/dm-mc02/tim8/rcr-update-gating",
                    test_tim8_rcr_gates_update_flag_only);

    /* CR2.MMS routes to the board's internal trigger sink.  No public
     * machine/QTest endpoint exposes that sink, so master-event callbacks
     * are deliberately not tested here. */
    return g_test_run();
}
