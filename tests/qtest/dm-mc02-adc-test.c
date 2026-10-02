/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"

#define ADC1_BASE       0x40022000
#define ADC1_ISR        (ADC1_BASE + 0x00)
#define ADC1_IER        (ADC1_BASE + 0x04)
#define ADC1_CR         (ADC1_BASE + 0x08)
#define ADC1_CFGR       (ADC1_BASE + 0x0c)
#define ADC1_SMPR1      (ADC1_BASE + 0x14)
#define ADC1_SMPR2      (ADC1_BASE + 0x18)
#define ADC1_DR         (ADC1_BASE + 0x40)
#define ADC1_SQR1       (ADC1_BASE + 0x30)
#define ADC1_SQR2       (ADC1_BASE + 0x34)
#define ADC1_SQR3       (ADC1_BASE + 0x38)
#define ADC1_SQR4       (ADC1_BASE + 0x3c)
#define ADC1_JSQR       (ADC1_BASE + 0x4c)
#define ADC1_JDR1       (ADC1_BASE + 0x80)
#define ADC1_JDR2       (ADC1_BASE + 0x84)
#define ADC1_JDR3       (ADC1_BASE + 0x88)
#define ADC1_JDR4       (ADC1_BASE + 0x8c)
#define ADC1_DIFSEL     (ADC1_BASE + 0xc0)
#define ADC1_CALFACT_RES13  (ADC1_BASE + 0xc4)
#define ADC1_CALFACT2_RES14 (ADC1_BASE + 0xc8)
#define ADC2_BASE       0x40022100
#define ADC2_ISR        (ADC2_BASE + 0x00)
#define ADC2_CR         (ADC2_BASE + 0x08)
#define ADC2_CFGR       (ADC2_BASE + 0x0c)
#define ADC2_DR         (ADC2_BASE + 0x40)
#define ADC2_SQR1       (ADC2_BASE + 0x30)
#define ADC2_JSQR       (ADC2_BASE + 0x4c)
#define ADC12_COMMON    0x40022300
#define ADC12_CSR       (ADC12_COMMON + 0x00)
#define ADC12_CCR       (ADC12_COMMON + 0x08)
#define ADC12_CDR       (ADC12_COMMON + 0x0c)
#define ADC12_CDR2      (ADC12_COMMON + 0x10)
#define DMA1_BASE       0x40020000
#define DMA1_LISR       (DMA1_BASE + 0x00)
#define DMA1_LIFCR      (DMA1_BASE + 0x08)
#define DMA1_S0CR       (DMA1_BASE + 0x10)
#define DMA1_S0NDTR     (DMA1_BASE + 0x14)
#define DMA1_S0PAR      (DMA1_BASE + 0x18)
#define DMA1_S0M0AR     (DMA1_BASE + 0x1c)
#define DMA1_S1CR       (DMA1_BASE + 0x28)
#define DMA1_S1NDTR     (DMA1_BASE + 0x2c)
#define DMA1_S1PAR      (DMA1_BASE + 0x30)
#define DMA1_S1M0AR     (DMA1_BASE + 0x34)
#define DMA1_S2CR       (DMA1_BASE + 0x40)
#define DMA1_S2NDTR     (DMA1_BASE + 0x44)
#define DMA1_S2PAR      (DMA1_BASE + 0x48)
#define DMA1_S2M0AR     (DMA1_BASE + 0x4c)
#define DMAMUX1_BASE    0x40020800
#define TIM8_BASE       0x40010400
#define TIM8_CR2        (TIM8_BASE + 0x04)
#define TIM8_EGR        (TIM8_BASE + 0x14)
#define TIM8_CR1        (TIM8_BASE + 0x00)
#define TIM8_CCMR1      (TIM8_BASE + 0x18)
#define TIM8_ARR        (TIM8_BASE + 0x2c)
#define TIM8_CCR1       (TIM8_BASE + 0x34)
#define TIM1_BASE       0x40010000
#define TIM1_CR2        (TIM1_BASE + 0x04)
#define TIM1_EGR        (TIM1_BASE + 0x14)
#define TIM3_BASE       0x40000400
#define TIM3_CR1        (TIM3_BASE + 0x00)
#define TIM3_CR2        (TIM3_BASE + 0x04)
#define TIM3_EGR        (TIM3_BASE + 0x14)
#define TIM3_CCMR2      (TIM3_BASE + 0x1c)
#define TIM3_ARR        (TIM3_BASE + 0x2c)
#define TIM3_CCR4       (TIM3_BASE + 0x40)
#define TIM2_BASE       0x40000000
#define TIM2_CR2        (TIM2_BASE + 0x04)
#define TIM2_EGR        (TIM2_BASE + 0x14)
#define TIM2_CR1        (TIM2_BASE + 0x00)
#define TIM2_ARR        (TIM2_BASE + 0x2c)
#define TIM2_CCR1       (TIM2_BASE + 0x34)
#define TIM2_CCR2       (TIM2_BASE + 0x38)
#define RCC_BASE        0x58024400
#define RCC_CR          (RCC_BASE + 0x00)
#define RCC_CFGR        (RCC_BASE + 0x10)
#define RCC_D1CFGR      (RCC_BASE + 0x18)
#define RCC_D2CFGR      (RCC_BASE + 0x1c)
#define RCC_PLLCFGR     (RCC_BASE + 0x2c)
#define RCC_PLLCKSELR   (RCC_BASE + 0x28)
#define RCC_PLL1DIVR    (RCC_BASE + 0x30)
#define RCC_PLL2DIVR    (RCC_BASE + 0x38)
#define RCC_PLL3DIVR    (RCC_BASE + 0x40)
#define RCC_D1CCIPR     (RCC_BASE + 0x4c)
#define RCC_D3CCIPR     (RCC_BASE + 0x58)

#define ADC_ISR_ADRDY   (1u << 0)
#define ADC_ISR_EOC     (1u << 2)
#define ADC_ISR_EOS     (1u << 3)
#define ADC_ISR_OVR     (1u << 4)
#define ADC_ISR_JEOC    (1u << 5)
#define ADC_ISR_JEOS    (1u << 6)
#define ADC_ISR_JQOVF   (1u << 10)
#define ADC_CR_ADEN     (1u << 0)
#define ADC_CR_ADDIS    (1u << 1)
#define ADC_CR_ADSTART  (1u << 2)
#define ADC_CR_JADSTP   (1u << 5)
#define ADC_CR_JADSTART (1u << 3)
#define ADC_CR_ADSTP    (1u << 4)
#define ADC_CR_ADCALLIN (1u << 16)
#define ADC_CR_ADVREGEN (1u << 28)
#define ADC_CR_DEEPPWD  (1u << 29)
#define ADC_CR_LINCALRDY_MASK (0x3fu << 22)
#define ADC_CR_ADCALDIF (1u << 30)
#define ADC_CR_ADCAL    (1u << 31)
#define ADC_CFGR_CONT   (1u << 13)
#define ADC_CFGR_AUTDLY (1u << 14)
#define ADC_CFGR_OVRMOD (1u << 12)
#define ADC_CFGR_DISCEN (1u << 16)
#define ADC_CFGR_DISCNUM_2RANKS (1u << 17)
#define ADC_CFGR_JDISCEN (1u << 20)
#define ADC_CFGR_JQM    (1u << 21)
#define ADC_CFGR_JAUTO  (1u << 25)
#define ADC_CFGR_JQDIS (1u << 31)
#define ADC_CFGR_DMA_CIRCULAR 3u
#define DMA_CR_EN       (1u << 0)
#define DMA_CR_MINC     (1u << 10)
#define DMA_CR_CIRC     (1u << 8)
#define DMA_CR_TCIE     (1u << 4)
#define DMA_CR_PSIZE_16 (1u << 11)
#define DMA_CR_PSIZE_32 (2u << 11)
#define DMA_CR_MSIZE_16 (1u << 13)
#define DMA_CR_MSIZE_32 (2u << 13)
#define ADC_IER_JEOCIE (1u << 5)
#define ADC_IER_JEOSIE (1u << 6)
#define ADC_IER_JQOVFIE (1u << 10)
#define ADC_JSQR_JEXTEN_RISING (1u << 7)
#define ADC_JSQR_JEXTEN_FALLING (2u << 7)
#define ADC_JSQR_JEXTSEL_SHIFT 2
#define ADC_JSQR_JSQ1_SHIFT 9
#define ADC_JSQR_JSQ2_SHIFT 15
#define ADC_JSQR_JSQ3_SHIFT 21
#define ADC_JSQR_JSQ4_SHIFT 27
#define ADC_CCR_CKMODE_SHIFT 16
#define ADC_CCR_PRESC_SHIFT 18

#define ADC_CALFACT_RES13_MASK  0x07ff07ffu
#define ADC_CALFACT2_RES14_MASK 0x3fffffffu
#define ADC_CALFACT_S_MASK      0x000007ffu
#define ADC_CALFACT_D_MASK      0x07ff0000u
#define ADC_DIFSEL_CHANNEL0     (1u << 0)
#define ADC_LINEAR_WORD_MASK    0x3fffffffu
#define ADC_LINEAR_LAST_WORD_MASK 0x000003ffu
#define ADC_CALIBRATION_OFFSET_DELAY_NS  853334
#define ADC_CALIBRATION_LINEAR_DELAY_NS 10922667
#define ADC_REGULATOR_STARTUP_DELAY_NS 10000
#define ADC_16BIT_32CYCLE_RANK_NS 32667

#define RCC_CR_HSION    (1u << 0)
#define RCC_CR_HSEON    (1u << 16)
#define RCC_CR_PLL1ON   (1u << 24)
#define RCC_CR_PLL2ON   (1u << 26)
#define RCC_CR_PLL3ON   (1u << 28)
#define RCC_PLLCFGR_PLL2DIVPEN (1u << 19)
#define RCC_PLLCFGR_PLL3DIVREN (1u << 24)

#define RCC_D3CCIPR_ADCSEL_SHIFT 16
#define RCC_D1CCIPR_CKPERSEL_SHIFT 28

static void configure_adc_pll2p(QTestState *qts);

static void configure_timer_fixture_clock(QTestState *qts)
{
    /* Keep timer edge timestamps independent from the HSI reset rate.  The
     * H723 fixture uses HCLK=32 MHz and APB2=/2, so TIM8 runs at 32 MHz. */
    qtest_writel(qts, RCC_D1CFGR, 0x8);
    qtest_writel(qts, RCC_D2CFGR, 0x440);
}

static void test_adrdy_lifecycle(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_ADRDY);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_ADRDY);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_ADRDY);

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_ADRDY);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADDIS);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_ADRDY);

    qtest_quit(qts);
}

static void test_deeppwd_advregen_low_power_lifecycle(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on,adc-power-model=on");
    uint32_t cr;
    uint32_t isr;

    /* H723 reset state: the ADC is in deep power-down and its regulator is
     * disabled.  No enable, ready, or conversion state may leak through. */
    cr = qtest_readl(qts, ADC1_CR);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(cr & ADC_CR_DEEPPWD);
    g_assert_false(cr & (ADC_CR_ADVREGEN | ADC_CR_ADEN | ADC_CR_ADSTART));
    g_assert_false(isr & ADC_ISR_ADRDY);

    /* Leaving deep power-down does not power the ADC regulator by itself. */
    qtest_writel(qts, ADC1_CR, 0);
    g_assert_false(qtest_readl(qts, ADC1_CR) & ADC_CR_DEEPPWD);

    /* The regulator must be explicitly enabled and allowed to settle. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADVREGEN);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_ADVREGEN);

    /* Commands issued before regulator ready are ignored, including a
     * combined enable/start write. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADVREGEN | ADC_CR_ADEN |
                 ADC_CR_ADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_false(cr & (ADC_CR_ADEN | ADC_CR_ADSTART));
    g_assert_false(isr & ADC_ISR_ADRDY);

    qtest_clock_step(qts, ADC_REGULATOR_STARTUP_DELAY_NS - 1);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADVREGEN | ADC_CR_ADEN |
                 ADC_CR_ADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_false(cr & (ADC_CR_ADEN | ADC_CR_ADSTART));
    g_assert_false(isr & ADC_ISR_ADRDY);

    /* At the exact startup deadline the same commands become effective. */
    qtest_clock_step(qts, 1);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADVREGEN | ADC_CR_ADEN |
                 ADC_CR_ADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(cr & (ADC_CR_ADEN | ADC_CR_ADSTART));
    g_assert_true(isr & ADC_ISR_ADRDY);

    /* Re-entering deep power-down aborts the active regular group and
     * removes the observable ready/enable/start state. */
    qtest_writel(qts, ADC1_CR, ADC_CR_DEEPPWD);
    cr = qtest_readl(qts, ADC1_CR);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(cr & ADC_CR_DEEPPWD);
    g_assert_false(cr & (ADC_CR_ADVREGEN | ADC_CR_ADEN | ADC_CR_ADSTART));
    g_assert_false(isr & ADC_ISR_ADRDY);

    qtest_quit(qts);
}

static void test_calibration_command_constraints(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t calibration_mode = ADC_CR_ADCALLIN | ADC_CR_ADCALDIF;

    /* ADCALLIN selects linearity calibration and ADCALDIF selects
     * differential offset mode; both mode bits are configured before ADCAL. */
    qtest_writel(qts, ADC1_CR, calibration_mode);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & calibration_mode, ==,
                    calibration_mode);

    qtest_writel(qts, ADC1_CR, calibration_mode | ADC_CR_ADCAL);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL, ==,
                    ADC_CR_ADCAL);

    /* ADCAL is an rs command bit: writing zero must not cancel an active
     * calibration.  Hardware clears it after the calibration interval. */
    qtest_writel(qts, ADC1_CR, calibration_mode);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL, ==,
                    ADC_CR_ADCAL);
    qtest_clock_step(qts, ADC_CALIBRATION_LINEAR_DELAY_NS - 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL, ==,
                    ADC_CR_ADCAL);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL, ==, 0);

    /* ADCAL cannot be accepted while the ADC is enabled. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR,
                 ADC_CR_ADEN | calibration_mode | ADC_CR_ADCAL);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADEN, ==,
                    ADC_CR_ADEN);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL, ==, 0);

    qtest_quit(qts);
}

static void test_command_rs_bits_preserve_zero_writes(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADSTART, ==,
                    ADC_CR_ADSTART);

    /* ADSTART is an rs bit: a later write with ADSTART=0 must not clear the
     * in-flight regular conversion. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADSTART, ==,
                    ADC_CR_ADSTART);

    qtest_quit(qts);
}

static void test_jadstart_regular_and_calibration_boundaries(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t cr;

    /* JQDIS selects the single injected context required for a software
     * JADSTART on H723.  Regular and injected starts remain independent. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART |
                 ADC_CR_JADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_ADSTART, ==, ADC_CR_ADSTART);
    g_assert_cmphex(cr & ADC_CR_JADSTART, ==, ADC_CR_JADSTART);

    /* Calibration cannot start while the ADC is enabled or either group is
     * active; rejected ADCAL must also leave both rs bits intact. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADCAL);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_ADSTART, ==, ADC_CR_ADSTART);
    g_assert_cmphex(cr & ADC_CR_ADCAL, ==, 0);
    g_assert_cmphex(cr & ADC_CR_JADSTART, ==, ADC_CR_JADSTART);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* An injected start remains observable while the regular group is idle,
     * and a later regular start is also accepted. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_JADSTART, ==, ADC_CR_JADSTART);
    g_assert_cmphex(cr & ADC_CR_ADSTART, ==, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_JADSTART, ==, ADC_CR_JADSTART);
    g_assert_cmphex(cr & ADC_CR_ADSTART, ==, ADC_CR_ADSTART);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* When the ADC is idle, ADCAL wins over simultaneous start commands. */
    qtest_writel(qts, ADC1_CR,
                 ADC_CR_ADSTART | ADC_CR_JADSTART | ADC_CR_ADCAL);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_ADCAL, ==, ADC_CR_ADCAL);
    g_assert_cmphex(cr & (ADC_CR_ADEN | ADC_CR_ADSTART | ADC_CR_JADSTART),
                    ==, 0);

    /* Calibration is observable and remains active when command bits are
     * written during the calibration interval. */
    qtest_writeb(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART |
                 ADC_CR_JADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_ADCAL, ==, ADC_CR_ADCAL);
    g_assert_cmphex(cr & (ADC_CR_ADEN | ADC_CR_ADSTART | ADC_CR_JADSTART),
                    ==, 0);

    qtest_clock_step(qts, ADC_CALIBRATION_OFFSET_DELAY_NS - 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL, ==,
                    ADC_CR_ADCAL);
    qtest_clock_step(qts, 1);
    g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL, ==, 0);

    qtest_quit(qts);
}

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

static void test_cr_byte_and_halfword_lanes(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t cr;

    /* Separate byte writes merge in CR and the low-byte command write starts
     * a regular conversion. */
    qtest_writeb(qts, ADC1_CR, ADC_CR_ADEN);
    qtest_writeb(qts, ADC1_CR, ADC_CR_ADSTART);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & (ADC_CR_ADEN | ADC_CR_ADSTART), ==,
                    ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_quit(qts);

    qts = qtest_init("-machine dm-mc02");

    /* The upper half-word carries the calibration mode and ADCAL command;
     * it must merge into the full CR and start the virtual calibration. */
    qtest_writew(qts, ADC1_CR + 2,
                 (ADC_CR_ADCALLIN | ADC_CR_ADCAL) >> 16);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & (ADC_CR_ADCALLIN | ADC_CR_ADCAL), ==,
                    ADC_CR_ADCALLIN | ADC_CR_ADCAL);
    g_assert_cmphex(cr & ADC_CR_LINCALRDY_MASK, ==, 0);
    qtest_clock_step(qts, ADC_CALIBRATION_LINEAR_DELAY_NS);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_ADCAL, ==, 0);
    g_assert_cmphex(cr & ADC_CR_LINCALRDY_MASK, ==,
                    ADC_CR_LINCALRDY_MASK);

    qtest_quit(qts);
}

static void test_repeated_adstart_keeps_rank_progress(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* Two ranks with the reset sampling code take 36 half-cycles each at the
     * default 1.5 MHz ADC clock: 36 / 3 MHz = 12000 ns. */
    qtest_writel(qts, ADC1_SQR1, 1u);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==,
                    ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100);

    /* Reissuing the already-set rs command must not reset current_rank. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 11999);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0200);

    qtest_quit(qts);
}

static void test_regular_sequence_sqr2_sqr3(void)
{
    static const uint32_t expected[] = {
        0x0100u, 0x0200u, 0, 0, UINT16_MAX, UINT16_MAX, UINT16_MAX,
        UINT16_MAX, UINT16_MAX, 43329u, 43329u, 43329u, 43329u, 43329u,
    };
    const uint32_t sqr2 = (19u << 0) | (19u << 6) | (19u << 12) |
                          (19u << 18) | (19u << 24);
    const uint32_t sqr3 = (4u << 0) | (4u << 6) | (4u << 12) |
                          (4u << 18) | (4u << 24);
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* Four SQR1 ranks plus ten ranks from SQR2/SQR3.  Rank 5 and rank 14
     * use byte/half-word-visible SQR fields, while rank 10 exercises the
     * first SQR3 field.  Board channel 19 is the powered 3.3 V source and
     * channel 4 is the VIN divider at the default 24 V input. */
    qtest_writel(qts, ADC1_SQR1, 13u);
    qtest_writew(qts, ADC1_SQR2, 19u);
    qtest_writeb(qts, ADC1_SQR3, 4u);
    qtest_writeb(qts, ADC1_SQR3 + 3, 19u);
    g_assert_cmphex(qtest_readl(qts, ADC1_SQR2), ==, 19u);
    g_assert_cmphex(qtest_readl(qts, ADC1_SQR3), ==, 0x13000004u);
    qtest_writel(qts, ADC1_SQR2, sqr2);
    qtest_writel(qts, ADC1_SQR3, sqr3);
    g_assert_cmphex(qtest_readl(qts, ADC1_SQR2), ==, sqr2);
    g_assert_cmphex(qtest_readl(qts, ADC1_SQR3), ==, sqr3);

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    for (size_t rank = 0; rank < ARRAY_SIZE(expected); ++rank) {
        qtest_clock_step(qts, 12000);
        g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, expected[rank]);
        if (rank + 1u < ARRAY_SIZE(expected)) {
            g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
        } else {
            g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
            g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADSTART, ==,
                            0);
        }
    }

    qtest_quit(qts);
}

static void test_regular_sequence_sqr4(void)
{
    static const uint32_t expected[] = {
        0x0100u, 0x0200u, 0, 0, UINT16_MAX, UINT16_MAX, UINT16_MAX,
        UINT16_MAX, UINT16_MAX, 43329u, 43329u, 43329u, 43329u, 43329u,
        UINT16_MAX, 43329u,
    };
    const uint32_t sqr2 = (19u << 0) | (19u << 6) | (19u << 12) |
                          (19u << 18) | (19u << 24);
    const uint32_t sqr3 = (4u << 0) | (4u << 6) | (4u << 12) |
                          (4u << 18) | (4u << 24);
    const uint32_t sqr4 = (19u << 0) | (4u << 6);
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* SQR4 contributes the final two H723 regular ranks.  Use a half-word
     * write for SQ15 and a byte lane write for SQ16 before checking the
     * equivalent full register value. */
    qtest_writel(qts, ADC1_SQR1, 15u);
    qtest_writel(qts, ADC1_SQR2, sqr2);
    qtest_writel(qts, ADC1_SQR3, sqr3);
    qtest_writew(qts, ADC1_SQR4, 19u);
    qtest_writeb(qts, ADC1_SQR4 + 1, 1u);
    g_assert_cmphex(qtest_readl(qts, ADC1_SQR4), ==, sqr4);

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    for (size_t rank = 0; rank < ARRAY_SIZE(expected); ++rank) {
        qtest_clock_step(qts, 12000);
        g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, expected[rank]);
        if (rank + 1u < ARRAY_SIZE(expected)) {
            g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
        } else {
            g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
            g_assert_cmphex(qtest_readl(qts, ADC1_CR) & ADC_CR_ADSTART, ==,
                            0);
        }
    }

    qtest_quit(qts);
}

static void test_autowait_holds_until_dr_read(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t isr;

    /* AUTDLY is a one-result hardware wait state, not a trigger FIFO.  In
     * continuous mode the next rank may start only after ADC_DR is read. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_CONT | ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 12000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & (ADC_ISR_EOC | ADC_ISR_EOS));
    g_assert_false(isr & (1u << 4));

    /* No host timer should advance the sequence while EOC is still set. */
    qtest_clock_step(qts, 100000);
    isr = qtest_readl(qts, ADC1_ISR);
    g_assert_true(isr & (ADC_ISR_EOC | ADC_ISR_EOS));
    g_assert_false(isr & (1u << 4));

    /* Reading DR releases the wait state and schedules the next conversion
     * from this virtual timestamp. */
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 11999);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTP);
    qtest_quit(qts);
}

static void test_regular_dma_overrun_request_gate_case(bool endpoint,
                                                       bool overwrite)
{
    QTestState *qts;
    uint16_t sample = 0;
    uint32_t cfgr = ADC_CFGR_CONT | ADC_CFGR_DMA_CIRCULAR;
    uint32_t dma_cr = DMA_CR_EN | DMA_CR_TCIE | DMA_CR_PSIZE_16 |
                      DMA_CR_MSIZE_16;
    char machine[128];

    g_snprintf(machine, sizeof(machine),
               "-machine dm-mc02,adc-accurate-timing=on,"
               "adc-dma-endpoint=%s", endpoint ? "on" : "off");
    qts = qtest_init(machine);

    /* Leave the request unserviced for the first two ranks.  This models a
     * stream which has not been enabled yet without adding a DMA-private
     * test hook. */
    qtest_writel(qts, DMAMUX1_BASE, 9u);
    qtest_writel(qts, DMA1_S0PAR, ADC1_DR);
    qtest_writel(qts, DMA1_S0M0AR, 0x20000100);
    qtest_writel(qts, DMA1_S0NDTR, 1u);
    qtest_memwrite(qts, 0x20000100, &sample, sizeof(sample));

    /* Two ranks make the overrun data-register oracle distinguish
     * OVRMOD=0 (preserve the old value) from OVRMOD=1 (overwrite). */
    qtest_writel(qts, ADC1_SQR1, 1u);
    qtest_writel(qts, ADC1_CFGR, cfgr | (overwrite ? ADC_CFGR_OVRMOD : 0));
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);

    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_OVR), ==, ADC_ISR_EOC);

    /* The stream is still disabled, so EOC remains pending when rank 2
     * completes.  The producer must set OVR and must not issue a DMA beat. */
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_OVR), ==,
                    ADC_ISR_EOC | ADC_ISR_OVR);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==,
                    overwrite ? UINT32_C(0x0200) : UINT32_C(0x0100));

    /* Enable the consumer while OVR is still set.  Reading DR clears only
     * EOC; the next conversion must remain blocked by the latched OVR. */
    qtest_writel(qts, DMA1_S0CR, dma_cr);
    qtest_readl(qts, ADC1_DR);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_OVR), ==,
                    ADC_ISR_EOC | ADC_ISR_OVR);
    qtest_memread(qts, 0x20000100, &sample, sizeof(sample));
    g_assert_cmphex(sample, ==, 0);
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);

    /* Clearing OVR is the documented request-gate release.  Clear the
     * simultaneously pending EOC through the normal DR consumer boundary,
     * then verify the following rank reaches DMA. */
    qtest_writel(qts, ADC1_ISR, ADC_ISR_OVR);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_OVR);
    qtest_readl(qts, ADC1_DR);
    qtest_clock_step(qts, 12000);
    qtest_memread(qts, 0x20000100, &sample, sizeof(sample));
    g_assert_cmphex(sample, ==, UINT16_C(0x0200));
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
    g_assert_false(qtest_readl(qts, ADC1_ISR) &
                   (ADC_ISR_EOC | ADC_ISR_OVR));

    qtest_quit(qts);
}

static void test_regular_dma_overrun_request_gate(void)
{
    /* Exercise both direct DMA consumers and both H723 OVRMOD data-register
     * policies under the same request-gate contract. */
    for (unsigned endpoint = 0; endpoint < 2; ++endpoint) {
        for (unsigned overwrite = 0; overwrite < 2; ++overwrite) {
            test_regular_dma_overrun_request_gate_case(endpoint, overwrite);
        }
    }
}

static void test_16bit_rank_conversion_deadline(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    qtest_writel(qts, ADC1_SMPR1, 4u << (4 * 3));
    qtest_writel(qts, ADC1_SQR1, 4u << 6);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, ADC_16BIT_32CYCLE_RANK_NS - 1);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 43329u);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC1_SMPR1, 4u << (4 * 3));
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_JSQR, 4u << ADC_JSQR_JSQ1_SHIFT);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_clock_step(qts, ADC_16BIT_32CYCLE_RANK_NS - 1);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC);
    qtest_quit(qts);
}

static void test_resolution_processing_deadlines(void)
{
    static const int64_t expected_ns[] = { 32667, 31334, 30000, 28667 };

    for (unsigned resolution = 0; resolution < ARRAY_SIZE(expected_ns);
         ++resolution) {
        QTestState *qts = qtest_init(
            "-machine dm-mc02,adc-accurate-timing=on");

        qtest_writel(qts, ADC1_SMPR1, 4u << (4 * 3));
        qtest_writel(qts, ADC1_SQR1, 4u << 6);
        qtest_writel(qts, ADC1_CFGR, resolution << 2);
        qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
        qtest_clock_step(qts, expected_ns[resolution] - 1);
        g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        qtest_clock_step(qts, 1);
        g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        qtest_quit(qts);
    }
}

static void test_16bit_rank_dma_consumer_deadline(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=on");
    uint16_t samples[2] = {0};
    const uint32_t buffer = 0x20000100u;

    qtest_writel(qts, DMAMUX1_BASE + 8, 9u);
    qtest_writel(qts, DMA1_S2PAR, ADC1_DR);
    qtest_writel(qts, DMA1_S2M0AR, buffer);
    qtest_writel(qts, DMA1_S2NDTR, 2);
    qtest_memwrite(qts, buffer, samples, sizeof(samples));
    qtest_writel(qts, DMA1_S2CR, DMA_CR_EN | DMA_CR_MINC | DMA_CR_CIRC |
                 DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16);
    qtest_writel(qts, ADC1_SMPR1, 4u << (4 * 3));
    qtest_writel(qts, ADC1_SMPR2, 4u << (9 * 3));
    qtest_writel(qts, ADC1_SQR1, 1u | (4u << 6) | (19u << 12));
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_CONT | ADC_CFGR_DMA_CIRCULAR);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);

    qtest_clock_step(qts, ADC_16BIT_32CYCLE_RANK_NS - 1);
    qtest_memread(qts, buffer, samples, sizeof(samples));
    g_assert_cmphex(samples[0], ==, 0);
    g_assert_cmphex(qtest_readl(qts, DMA1_S2NDTR), ==, 2);
    qtest_clock_step(qts, 1);
    qtest_memread(qts, buffer, samples, sizeof(samples));
    g_assert_cmphex(samples[0], ==, 43329u);
    g_assert_cmphex(samples[1], ==, 0);
    g_assert_cmphex(qtest_readl(qts, DMA1_S2NDTR), ==, 1);

    qtest_clock_step(qts, ADC_16BIT_32CYCLE_RANK_NS - 1);
    qtest_memread(qts, buffer, samples, sizeof(samples));
    g_assert_cmphex(samples[1], ==, 0);
    qtest_clock_step(qts, 1);
    qtest_memread(qts, buffer, samples, sizeof(samples));
    g_assert_cmphex(samples[1], ==, UINT16_MAX);
    g_assert_cmphex(qtest_readl(qts, DMA1_S2NDTR), ==, 2);
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

static void test_regular_discontinuous_software_is_full_sequence(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* HAL permits DISCEN only with CONT clear.  A software start remains one
     * complete sequence; DISCNUM subdivides external-triggered starts only. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_DISCEN | ADC_CFGR_DISCNUM_2RANKS);
    qtest_writel(qts, ADC1_SQR1, 3u);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);

    qtest_clock_step(qts, 12000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100u);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);
    qtest_clock_step(qts, 12000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0200u);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);
    qtest_clock_step(qts, 12000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOS);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);
    qtest_clock_step(qts, 12000);
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

static void test_calibration_uses_effective_clock(void)
{
    QTestState *qts;

    qts = qtest_init("-machine dm-mc02");
    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0);
    /* 1280 ADC cycles at 4 MHz = 320000 ns. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADCAL);
    qtest_clock_step(qts, 319999);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL);
    qtest_clock_step(qts, 1);
    g_assert_false(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL);
    qtest_quit(qts);

    qts = qtest_init("-machine dm-mc02");
    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0);
    /* 16384 ADC cycles at 4 MHz = 4096000 ns. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADCALLIN | ADC_CR_ADCAL);
    qtest_clock_step(qts, 4095999);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL);
    qtest_clock_step(qts, 1);
    g_assert_false(qtest_readl(qts, ADC1_CR) & ADC_CR_ADCAL);
    qtest_quit(qts);
}

static void test_active_adc_clock_change_preserves_remaining_work(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    /* The reset-compatible ADC clock is 1.5 MHz.  Nine half-cycles have
     * elapsed from the initial 36-half-cycle rank in 3000 ns. */
    qtest_clock_step(qts, 3000);

    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0);
    /* 27 remaining half-cycles at 4 MHz = 3375 ns. */
    qtest_clock_step(qts, 3374);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_calfact_res13_mask(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT_RES13), ==, 0);
    qtest_writel(qts, ADC1_CALFACT_RES13, UINT32_MAX);
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT_RES13), ==, 0);

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CALFACT_RES13, UINT32_MAX);
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT_RES13), ==,
                    ADC_CALFACT_RES13_MASK);

    qtest_quit(qts);
}

static void test_calfact2_res14_mask(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==, 0);
    qtest_writel(qts, ADC1_CALFACT2_RES14, UINT32_MAX);
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==, 0);

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CALFACT2_RES14, UINT32_MAX);
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==,
                    ADC_CALFACT2_RES14_MASK);

    qtest_quit(qts);
}

static void test_calfact_reset(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CALFACT_RES13, ADC_CALFACT_RES13_MASK);
    qtest_writel(qts, ADC1_CALFACT2_RES14, ADC_CALFACT2_RES14_MASK);
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT_RES13), ==,
                    ADC_CALFACT_RES13_MASK);
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==,
                    ADC_CALFACT2_RES14_MASK);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT_RES13), ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==, 0);

    qtest_quit(qts);
}

static void start_one_conversion(QTestState *qts, uint32_t ccr)
{
    /* A direct SQR1 write selects the one-rank regular sequence. */
    qtest_writel(qts, ADC12_CCR, ccr);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
}

static void test_adc2_regular_conversion_is_independent(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    qtest_writel(qts, ADC2_SQR1, 0);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_ADRDY);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_ADRDY);

    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC2_ISR) &
                  (ADC_ISR_EOC | ADC_ISR_EOS));
    g_assert_cmphex(qtest_readl(qts, ADC2_DR), ==, 0x0100);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_ccr_lane_writes_reconfigure_clock(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    configure_adc_pll2p(qts);
    /* PRESC=11 is written through the high byte of CCR. */
    qtest_writeb(qts, ADC12_CCR + 2, 11u << 2);
    g_assert_cmphex(qtest_readl(qts, ADC12_CCR), ==,
                    11u << ADC_CCR_PRESC_SHIFT);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 1151999);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_adc_pll2p(qts);
    /* The same field is also reachable through a half-word write. */
    qtest_writew(qts, ADC12_CCR + 2, 11u << 2);
    g_assert_cmphex(qtest_readl(qts, ADC12_CCR), ==,
                    11u << ADC_CCR_PRESC_SHIFT);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 1152000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_csr_projects_both_adc_status_blocks(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t adc1_status;
    uint32_t adc2_status;
    uint32_t expected;
    uint32_t context_a = 1u << ADC_JSQR_JSQ1_SHIFT;
    uint32_t context_b = 2u << ADC_JSQR_JSQ1_SHIFT;
    uint32_t context_c = 3u << ADC_JSQR_JSQ1_SHIFT;

    /* Exercise ready, regular EOC/EOS, and injected JEOC/JEOS on both
     * independent ADC instances, then compare the common projection with
     * their public ISR values. */
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC2_SQR1, 0);
    qtest_writel(qts, ADC1_JSQR, 0);
    qtest_writel(qts, ADC2_JSQR, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART |
                 ADC_CR_JADSTART);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN | ADC_CR_ADSTART |
                 ADC_CR_JADSTART);
    qtest_clock_step(qts, 12000);

    adc1_status = qtest_readl(qts, ADC1_ISR);
    adc2_status = qtest_readl(qts, ADC2_ISR);
    expected = (adc1_status & 0x47f) |
               ((adc2_status & 0x47f) << 16);
    g_assert_cmphex(qtest_readl(qts, ADC12_CSR), ==, expected);
    g_assert_true(expected & ADC_ISR_ADRDY);
    g_assert_true(expected & ADC_ISR_EOC);
    g_assert_true(expected & ADC_ISR_EOS);
    g_assert_true(expected & (ADC_ISR_JEOC << 0));
    g_assert_true(expected & (ADC_ISR_JEOS << 0));
    g_assert_true(expected & (ADC_ISR_ADRDY << 16));

    /* Queue overflow is projected at bit 26 (ADC2 half, when generated by
     * ADC2) just like the other ADC2 ISR bits. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC2_JSQR, context_a);
    qtest_writel(qts, ADC2_JSQR, context_b);
    qtest_writel(qts, ADC2_JSQR, context_c);
    adc2_status = qtest_readl(qts, ADC2_ISR);
    g_assert_true(adc2_status & ADC_ISR_JQOVF);
    g_assert_true(qtest_readl(qts, ADC12_CSR) & (ADC_ISR_JQOVF << 16));

    /* CSR, CDR and CDR2 are not writable in this boundary slice. */
    expected = qtest_readl(qts, ADC12_CSR);
    qtest_writel(qts, ADC12_CSR, UINT32_MAX);
    qtest_writel(qts, ADC12_CDR, UINT32_MAX);
    qtest_writel(qts, ADC12_CDR2, UINT32_MAX);
    g_assert_cmphex(qtest_readl(qts, ADC12_CSR), ==, expected);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR2), ==, 0);

    qtest_quit(qts);
}

static void test_common_cdr_packs_regular_simultaneous_results(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* DAMDF=2 is the 32/10-bit multimode transfer format.  ADC2 is the
     * slave: enable it first, then let the ADC1 master start the pair. */
    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0x6u | (2u << 14));
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);

    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==, UINT32_C(0x01000100));
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR2), ==, 0);

    /* DAMDF=0 does not pack data.  Reset clears the previous CDR and the
     * bounded producer pairing state. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC12_CCR, 0x6u);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 1000);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==, 0);

    qtest_quit(qts);
}

static void test_common_cdr_read_acknowledges_both_eoc(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0x6u | (2u << 14));
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_CONT | ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_CONT | ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);

    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==,
                    UINT32_C(0x01000100));
    /* Reading the supported common CDR acknowledges both producers and
     * releases the AUTDLY wait state. */
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    /* The read, rather than a wall-clock delay, is the release event. */
    qtest_clock_step(qts, 4499);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_cdr2_publishes_interleaved_regular_result(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    const uint32_t delay0_ccr = 0x7u;
    const uint32_t delay2_ccr = 0x7u | (2u << 8);

    configure_adc_pll2p(qts);
    /* LL_ADC_MULTI_DUAL_REG_INTERL is DUAL=0x7.  Only the master software
     * start is accepted; the common boundary starts the enabled slave after
     * the master's sampling phase and CCR.DELAY interval. */
    qtest_writel(qts, ADC12_CCR, delay0_ccr);
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 4500);

    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR2), ==, UINT32_C(0x00000100));
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==, 0);

    /* The fixture's ADC kernel is 4 MHz.  Default sampling is 3/2 cycles and
     * DELAY=0 is another 3/2 cycles, so the slave EOC is 750 ns after the
     * master start plus one 4500 ns rank conversion. */
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 5250);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR2), ==, UINT32_C(0x00000100));

    /* CONT keeps the two kernels in cadence.  Clearing the first pair makes
     * the ordering of the next events visible: M0,S0,M1,S1. */
    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC | ADC_ISR_EOS);
    qtest_writel(qts, ADC2_ISR, ADC_ISR_EOC | ADC_ISR_EOS);
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 9000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_adc_pll2p(qts);
    /* DELAY=2 is 3.5 ADC clocks.  It moves the slave EOC from 5250 ns to
     * 5750 ns relative to this sequence start. */
    qtest_writel(qts, ADC12_CCR, delay2_ccr);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 13500);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 14750);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR2), ==, UINT32_C(0x00000100));

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_adc_pll2p(qts);
    /* DUAL=0x3 is regular interleaved plus injected simultaneous.  The
     * regular master/slave cadence is shared with DUAL=0x7; injected trigger
     * routing remains an independent ADC group until its own common-data
     * boundary is implemented. */
    qtest_writel(qts, ADC12_CCR, 0x3u);
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 19250);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 20000);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR2), ==, UINT32_C(0x00000100));
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==, 0);

    qtest_quit(qts);
}

static void test_common_master_start_drives_slave(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0x6u | (2u << 14));

    /* HAL_ADCEx_MultiModeStart_DMA enables the slave first and issues only
     * the master regular-start command. */
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_true(qtest_readl(qts, ADC2_CR) & ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==,
                    UINT32_C(0x01000100));

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0x6u | (2u << 14));
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    /* The slave cannot own a regular start while DUAL=regular simultaneous. */
    g_assert_false(qtest_readl(qts, ADC2_CR) & ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    /* A board reset must preserve the runtime common-to-peer wiring. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 0x6u | (2u << 14));
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_true(qtest_readl(qts, ADC2_CR) & ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_external_trigger_drives_slave(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t cfgr = (7u << 5) | (1u << 10);

    configure_adc_pll2p(qts);
    configure_timer_fixture_clock(qts);
    qtest_writel(qts, ADC12_CCR, 0x6u | (2u << 14));
    qtest_writel(qts, ADC1_CFGR, cfgr);
    qtest_writel(qts, ADC2_CFGR, cfgr);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC2_SQR1, 0);

    /* External ADSTART only arms each converter.  It must not take the
     * software-start admission path or allocate an unrelated pair ID. */
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    g_assert_true(qtest_readl(qts, ADC1_CR) & ADC_CR_ADSTART);
    g_assert_true(qtest_readl(qts, ADC2_CR) & ADC_CR_ADSTART);

    /* TIM8_TRGO is consumed by the ADC1/master sink first.  The common
     * admission callback starts ADC2 through the shared-ID peer boundary;
     * both ranks then publish one CDR pair. */
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==,
                    UINT32_C(0x01000100));

    /* A master-only external arm is rejected at the common boundary rather
     * than producing an unmatched ADC1 result. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    configure_adc_pll2p(qts);
    configure_timer_fixture_clock(qts);
    qtest_writel(qts, ADC12_CCR, 0x6u | (2u << 14));
    qtest_writel(qts, ADC1_CFGR, cfgr);
    qtest_writel(qts, ADC2_CFGR, cfgr);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, TIM8_EGR, 1u);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC, ==, 0);

    qtest_quit(qts);
}

static void test_common_multimode_dma(bool endpoint)
{
    QTestState *qts;
    uint32_t samples[2] = { 0, 0 };
    uint32_t cr = DMA_CR_EN | DMA_CR_MINC | DMA_CR_TCIE |
                  DMA_CR_PSIZE_32 | DMA_CR_MSIZE_32;
    char machine[96];

    g_snprintf(machine, sizeof(machine),
               "-machine dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=%s",
               endpoint ? "on" : "off");
    qts = qtest_init(machine);
    configure_adc_pll2p(qts);

    /* HAL_ADCEx_MultiModeStart_DMA() programs the common block before it
     * starts the ADC1/master.  Without DUAL/DAMDF this is two independent
     * ADC conversions, so ADC2 never joins the CDR pair and no common DMA
     * request can be produced. */
    qtest_writel(qts, ADC12_CCR, 6u | (2u << 14));

    /* HAL_ADCEx_MultiModeStart_DMA() selects the common CDR, not ADC1->DR.
     * Stream 0 is connected to the board's ADC1 request 9. */
    qtest_writel(qts, DMAMUX1_BASE, 9u);
    qtest_writel(qts, DMA1_S0PAR, ADC12_CDR);
    qtest_writel(qts, DMA1_S0M0AR, 0x20000100);
    qtest_writel(qts, DMA1_S0NDTR, ARRAY_SIZE(samples));
    qtest_memwrite(qts, 0x20000100, samples, sizeof(samples));
    qtest_writel(qts, DMA1_S0CR, cr);
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_DMA_CIRCULAR);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 9000);

    qtest_memread(qts, 0x20000100, samples, sizeof(samples));
    /* The two legacy ranks form two successive common CDR words. */
    g_assert_cmphex(samples[0], ==, UINT32_C(0x01000100));
    g_assert_cmphex(samples[1], ==, UINT32_C(0x02000200));
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==,
                    UINT32_C(0x02000200));

    qtest_quit(qts);
}

static void test_common_fifo_consuming(gconstpointer opaque)
{
    unsigned flags = GPOINTER_TO_UINT(opaque);
    bool cdr2 = flags & 1;
    bool endpoint = flags & 2;
    bool bad_destination = flags & 4;
    char machine[128];
    QTestState *qts;

    g_snprintf(machine, sizeof(machine),
               "-machine dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=%s",
               endpoint ? "on" : "off");
    qts = qtest_init(machine);
    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, (cdr2 ? 7u : 6u) | (2u << 14));
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC2_SQR1, 0);
    qtest_writel(qts, DMAMUX1_BASE, 9);
    qtest_writel(qts, DMA1_S0PAR, cdr2 ? ADC12_CDR2 : ADC12_CDR);
    qtest_writel(qts, DMA1_S0M0AR,
                  bad_destination ? 0xdead0000 : 0x20000100);
    qtest_writel(qts, 0x20000100, 0);
    qtest_writel(qts, DMA1_S0NDTR, 1);
    qtest_writel(qts, DMA1_BASE + 0x24, 4); /* DMDIS: FIFO, quarter threshold */
    qtest_writel(qts, DMA1_S0CR,
                  DMA_CR_EN | DMA_CR_PSIZE_32 | DMA_CR_MSIZE_32);
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_DMA_CIRCULAR);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);
    g_assert_false(qtest_readl(qts, DMA1_S0CR) & DMA_CR_EN);
    /* FIFO reads consume before the memory write, even if it fails. */
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    if (bad_destination) {
        g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 3));
        g_assert_false(qtest_readl(qts, DMA1_LISR) & (1u << 5));
        g_assert_cmphex(qtest_readl(qts, 0x20000100), ==, 0);
    } else {
        g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
        g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
        g_assert_false(qtest_readl(qts, DMA1_LISR) & (1u << 3));
        g_assert_cmphex(qtest_readl(qts, 0x20000100), ==,
                        cdr2 ? 0x100 : 0x01000100);
    }
    qtest_quit(qts);
}

static void test_common_multimode_dma_overrun_request_gate(bool endpoint,
                                                          unsigned retry_mask)
{
    QTestState *qts;
    uint32_t sample = 0;
    uint32_t cr = DMA_CR_EN | DMA_CR_PSIZE_32 | DMA_CR_MSIZE_32;
    char machine[128];

    g_snprintf(machine, sizeof(machine),
               "-machine dm-mc02,adc-accurate-timing=on,"
               "adc-dma-endpoint=%s", endpoint ? "on" : "off");
    qts = qtest_init(machine);
    configure_adc_pll2p(qts);

    qtest_writel(qts, ADC12_CCR, 6u | (2u << 14));

    /* Keep request 9 and the CDR destination configured, but leave the
     * stream disabled for the first two common results.  The first result
     * sets EOC; the second must set OVR and stop common CDR DMA. */
    qtest_writel(qts, DMAMUX1_BASE, 9u);
    qtest_writel(qts, DMA1_S0PAR, ADC12_CDR);
    qtest_writel(qts, DMA1_S0M0AR, 0x20000100);
    qtest_writel(qts, DMA1_S0NDTR, 1u);
    qtest_memwrite(qts, 0x20000100, &sample, sizeof(sample));
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_DMA_CIRCULAR | ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);

    qtest_clock_step(qts, 4500);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_OVR), ==, ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, ADC2_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_OVR), ==, ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);

    qtest_clock_step(qts, 4500);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_OVR), ==,
                    ADC_ISR_EOC | ADC_ISR_OVR);
    g_assert_cmphex(qtest_readl(qts, ADC2_ISR) &
                    (ADC_ISR_EOC | ADC_ISR_OVR), ==,
                    ADC_ISR_EOC | ADC_ISR_OVR);
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);
    g_assert_cmphex(qtest_readl(qts, 0x20000100), ==, 0);

    if (retry_mask) {
        if (!(retry_mask & 1)) {
            qtest_writel(qts, ADC1_ISR, ADC_ISR_OVR);
        }
        if (!(retry_mask & 2)) {
            qtest_writel(qts, ADC2_ISR, ADC_ISR_OVR);
        }
        qtest_writel(qts, DMA1_S0CR, cr);
        g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);
        g_assert_cmphex(qtest_readl(qts, DMA1_LISR), ==, 0);
        g_assert_cmphex(qtest_readl(qts, 0x20000100), ==, 0);
        g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
        /* Retry the same pending word after OVR clears, with no new
         * conversion or virtual-clock step. */
        qtest_writel(qts, DMA1_S0CR, cr & ~DMA_CR_EN);
        qtest_writel(qts, ADC1_ISR, ADC_ISR_OVR);
        qtest_writel(qts, ADC2_ISR, ADC_ISR_OVR);
        qtest_writel(qts, DMA1_S0CR, cr);
        g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
        g_assert_cmphex(qtest_readl(qts, 0x20000100), ==, 0x02000200);
        g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
        g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
        qtest_quit(qts);
        return;
    }

    /* A software CDR read consumes both pending EOC flags but not OVR.  The
     * request gate therefore remains closed until firmware clears both OVR
     * flags explicitly. */
    g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==,
                    UINT32_C(0x02000200));
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC, ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC, ==, 0);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_OVR);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_OVR);
    qtest_writel(qts, ADC1_ISR, ADC_ISR_OVR);
    qtest_writel(qts, ADC2_ISR, ADC_ISR_OVR);
    g_assert_cmphex(qtest_readl(qts, ADC1_ISR) & ADC_ISR_OVR, ==, 0);
    g_assert_cmphex(qtest_readl(qts, ADC2_ISR) & ADC_ISR_OVR, ==, 0);

    qtest_writel(qts, DMA1_S0CR, cr);
    qtest_clock_step(qts, 4500);
    g_assert_cmphex(qtest_readl(qts, 0x20000100), ==,
                    UINT32_C(0x01000100));
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
    g_assert_false(qtest_readl(qts, ADC1_ISR) &
                   (ADC_ISR_EOC | ADC_ISR_OVR));
    g_assert_false(qtest_readl(qts, ADC2_ISR) &
                   (ADC_ISR_EOC | ADC_ISR_OVR));

    qtest_quit(qts);
}

static void test_common_multimode_dma_overrun_request_gate_endpoint(void)
{
    test_common_multimode_dma_overrun_request_gate(true, 0);
}

static void test_common_multimode_dma_overrun_request_gate_mmio(void)
{
    test_common_multimode_dma_overrun_request_gate(false, 0);
}

static void test_common_dma_overrun_retry(gconstpointer opaque)
{
    test_common_multimode_dma_overrun_request_gate(true,
                                                  GPOINTER_TO_UINT(opaque));
}

static void test_common_interleaved_external_trigger_cadence(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t cfgr = (7u << 5) | (1u << 10);

    configure_adc_pll2p(qts);
    configure_timer_fixture_clock(qts);
    qtest_writel(qts, ADC12_CCR, 0x7u);
    qtest_writel(qts, ADC1_CFGR, cfgr);
    qtest_writel(qts, ADC2_CFGR, cfgr);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC2_SQR1, 0);

    /* Both ADCs arm ADSTART, but only the master may consume the regular
     * trigger edge in interleaved mode. */
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, TIM8_EGR, 1u);

    /* The 4 MHz fixture completes the master at 4500 ns and the default
     * DELAY=0 places the slave at 5250 ns (3/2 sampling + 3/2 delay cycles). */
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 4500);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 5250);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_interleaved_injected_simultaneous(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");
    uint32_t jsqr = (9u << ADC_JSQR_JEXTSEL_SHIFT) |
                    ADC_JSQR_JEXTEN_RISING;

    configure_adc_pll2p(qts);
    configure_timer_fixture_clock(qts);
    qtest_writel(qts, ADC12_CCR, 0x3u);
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_JSQR, jsqr);
    qtest_writel(qts, ADC2_JSQR, jsqr);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_writel(qts, TIM8_CR2, 2u << 4);
    qtest_writel(qts, TIM8_EGR, 1u);

    /* DUAL=0x3 shares the regular-interleaved mode but keeps injected
     * simultaneous trigger delivery as a separate ADC group.  Both injected
     * kernels receive the same trigger timestamp and complete together. */
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 4500);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_JEOC);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_JEOS);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_JEOS);

    qtest_quit(qts);
}

static void test_common_interleaved_damdf_dma(bool endpoint, unsigned dual,
                                              unsigned damdf)
{
    QTestState *qts;
    uint32_t samples[2] = { 0, 0 };
    uint32_t cr = DMA_CR_EN | DMA_CR_MINC;
    unsigned cdr_address;
    unsigned transfer_count;
    char machine[96];

    if (damdf == 2) {
        cr |= DMA_CR_PSIZE_32 | DMA_CR_MSIZE_32;
        cdr_address = ADC12_CDR2;
        transfer_count = 2;
    } else {
        cr |= DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;
        cdr_address = ADC12_CDR;
        transfer_count = 1;
    }

    g_snprintf(machine, sizeof(machine),
               "-machine dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=%s",
               endpoint ? "on" : "off");
    qts = qtest_init(machine);
    configure_adc_pll2p(qts);

    /* DAMDF=2 selects alternating 32-bit CDR2 beats. DAMDF=3 instead packs
     * four alternating 8-bit results into CDR and transfers one half-word. */
    qtest_writel(qts, ADC12_CCR, dual | (damdf << 14));
    qtest_writel(qts, ADC1_SQR1, 4u << 6);
    qtest_writel(qts, ADC2_SQR1, 0);
    qtest_writel(qts, DMAMUX1_BASE, 9u);
    qtest_writel(qts, DMA1_S0PAR, cdr_address);
    qtest_writel(qts, DMA1_S0M0AR, 0x20000100);
    qtest_writel(qts, DMA1_S0NDTR, transfer_count);
    qtest_memwrite(qts, 0x20000100, samples, sizeof(samples));
    qtest_writel(qts, DMA1_S0CR, cr);
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_DMA_CIRCULAR | ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_CONT);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);

    /* DAMDF=2 consumes the master CDR2 value immediately. DAMDF=3 merely
     * accumulates the first byte and must not issue a partial DMA request. */
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 4500);
    if (damdf == 2) {
        g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
        samples[0] = 0;
        qtest_memread(qts, 0x20000100, samples, sizeof(uint32_t));
        g_assert_cmphex(samples[0], ==, qtest_readl(qts, ADC1_DR));
    } else {
        g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);
    }

    /* The delayed slave provides the next alternating byte.  DAMDF=3 now
     * has two bytes only, so the DMA stream remains untouched. */
    g_assert_cmpint(qtest_clock_step_next(qts), ==, 5250);
    if (damdf == 2) {
        g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
        samples[1] = 0;
        qtest_memread(qts, 0x20000104, &samples[1], sizeof(uint32_t));
        g_assert_cmphex(samples[1], ==, qtest_readl(qts, ADC2_DR));
    } else {
        g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
        g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);
        /* M1 and S1 complete at 9000 and 9750 ns.  The fourth value makes
         * the complete CDR word valid.  ADC1 rank 1 is the board VIN sense
         * channel (24 V through the 11:1 divider -> raw low byte 0x41),
         * while ADC2 rank 1 uses the deterministic 0x0100 fallback; DAMDF=3
         * therefore emits M0,S0,M1,S1 = 0x41,0x00,0x41,0x00. */
        g_assert_cmpint(qtest_clock_step_next(qts), ==, 9000);
        g_assert_cmpint(qtest_clock_step_next(qts), ==, 9750);
        g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
        g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
        g_assert_cmphex(qtest_readl(qts, ADC12_CDR), ==,
                        UINT32_C(0x00410041));
        qtest_memread(qts, 0x20000100, samples, sizeof(uint16_t));
        g_assert_cmphex(samples[0] & 0xffffu, ==, UINT32_C(0x0041));
    }
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));

    qtest_quit(qts);
}

static void test_common_cdr2_dma_endpoint(void)
{
    test_common_interleaved_damdf_dma(true, 0x7u, 2);
    test_common_interleaved_damdf_dma(true, 0x3u, 2);
}

static void test_common_cdr2_dma_endpoint_reservation(unsigned dual)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=on");
    uint32_t sample = 0;
    uint32_t cr = DMA_CR_EN | DMA_CR_PSIZE_32 | DMA_CR_MSIZE_32;

    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, dual | (2u << 14));
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC2_SQR1, 0);
    qtest_writel(qts, DMAMUX1_BASE, 9u);
    qtest_writel(qts, DMA1_S0PAR, ADC12_CDR2);
    qtest_writel(qts, DMA1_S0M0AR, 0x60000000u);
    qtest_writel(qts, DMA1_S0NDTR, 1u);
    qtest_memwrite(qts, 0x20000100, &sample, sizeof(sample));
    qtest_writel(qts, DMA1_S0CR, cr);
    qtest_writel(qts, ADC1_CFGR,
                 ADC_CFGR_DMA_CIRCULAR | ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);

    /* The invalid destination must abort CDR2's source reservation.  The
     * first result and its producer EOC remain available for retry. */
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);
    g_assert_false(qtest_readl(qts, DMA1_S0CR) & DMA_CR_EN);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 3));
    g_assert_cmphex(qtest_readl(qts, 0x20000100), ==, 0);

    /* Re-enabling the matching stream retries the same CDR2 word without a
     * new conversion and commits only ADC1's source-specific EOC. */
    qtest_writel(qts, DMA1_LIFCR, 0x3du);
    qtest_writel(qts, DMA1_S0M0AR, 0x20000100);
    qtest_writel(qts, DMA1_S0NDTR, 1u);
    qtest_writel(qts, DMA1_S0CR, cr);
    g_assert_cmphex(qtest_readl(qts, 0x20000100), ==,
                    UINT32_C(0x00000100));
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_cdr2_dma_endpoint_reservation_retry(void)
{
    test_common_cdr2_dma_endpoint_reservation(0x7u);
    test_common_cdr2_dma_endpoint_reservation(0x3u);
}

static void test_common_cdr2_dma_mmio(void)
{
    test_common_interleaved_damdf_dma(false, 0x7u, 2);
    test_common_interleaved_damdf_dma(false, 0x3u, 2);
}

static void test_common_interleaved_damdf8_dma_endpoint(void)
{
    test_common_interleaved_damdf_dma(true, 0x7u, 3);
    test_common_interleaved_damdf_dma(true, 0x3u, 3);
}

static void test_common_interleaved_damdf8_dma_mmio(void)
{
    test_common_interleaved_damdf_dma(false, 0x7u, 3);
    test_common_interleaved_damdf_dma(false, 0x3u, 3);
}

static void test_common_multimode_dma_endpoint(void)
{
    test_common_multimode_dma(true);
}

static void test_common_multimode_dma_mmio(void)
{
    test_common_multimode_dma(false);
}

static void test_common_multimode_dma_endpoint_reservation_retry(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=on");
    uint32_t sample = 0;
    uint32_t cr = DMA_CR_EN | DMA_CR_PSIZE_32 | DMA_CR_MSIZE_32;

    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 6u | (2u << 14));
    qtest_writel(qts, DMAMUX1_BASE, 9u);
    qtest_writel(qts, DMA1_S0PAR, ADC12_CDR);
    qtest_writel(qts, DMA1_S0M0AR, 0x60000000u);
    qtest_writel(qts, DMA1_S0NDTR, 1u);
    qtest_memwrite(qts, 0x20000100, &sample, sizeof(sample));
    qtest_writel(qts, DMA1_S0CR, cr);
    qtest_writel(qts, ADC1_CFGR,
                 ADC_CFGR_DMA_CIRCULAR | ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC2_CFGR, ADC_CFGR_AUTDLY);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 4500);

    /* Invalid P2M memory aborts the common source reservation.  DMA reports
     * TEIF and stops, but CDR's two producer EOC flags remain for retry. */
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_true(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 1);
    g_assert_false(qtest_readl(qts, DMA1_S0CR) & DMA_CR_EN);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 3));
    g_assert_cmphex(qtest_readl(qts, 0x20000100), ==, 0);

    /* Stream enable is the level-like retry boundary.  It moves the same
     * CDR word exactly once and commits the common read acknowledgement. */
    qtest_writel(qts, DMA1_LIFCR, 0x3du);
    qtest_writel(qts, DMA1_S0M0AR, 0x20000100);
    qtest_writel(qts, DMA1_S0NDTR, 1u);
    qtest_writel(qts, DMA1_S0CR, cr);
    g_assert_cmphex(qtest_readl(qts, 0x20000100), ==,
                    UINT32_C(0x01000100));
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    g_assert_false(qtest_readl(qts, ADC2_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_multimode_dma_request_cache_key(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=on");
    uint32_t adc_samples[2] = { 0, 0 };
    uint32_t cdr_samples[2] = { 0, 0 };
    uint32_t cr = DMA_CR_EN | DMA_CR_MINC | DMA_CR_PSIZE_32 |
                 DMA_CR_MSIZE_32;

    configure_adc_pll2p(qts);
    qtest_writel(qts, ADC12_CCR, 6u | (2u << 14));

    /* Request 9 is shared by ADC1->DR and ADC12_COMMON->CDR.  Give each
     * endpoint its own stream so a cached negative/positive lookup for one
     * peripheral address cannot select or suppress the other stream. */
    qtest_writel(qts, DMAMUX1_BASE + 0, 9u);
    qtest_writel(qts, DMAMUX1_BASE + 4, 9u);
    qtest_writel(qts, DMA1_S0PAR, ADC1_DR);
    qtest_writel(qts, DMA1_S0M0AR, 0x20000100);
    qtest_writel(qts, DMA1_S0NDTR, ARRAY_SIZE(adc_samples));
    qtest_writel(qts, DMA1_S1PAR, ADC12_CDR);
    qtest_writel(qts, DMA1_S1M0AR, 0x20000200);
    qtest_writel(qts, DMA1_S1NDTR, ARRAY_SIZE(cdr_samples));
    qtest_memwrite(qts, 0x20000100, adc_samples, sizeof(adc_samples));
    qtest_memwrite(qts, 0x20000200, cdr_samples, sizeof(cdr_samples));
    qtest_writel(qts, DMA1_S0CR, cr);
    qtest_writel(qts, DMA1_S1CR, cr);
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_DMA_CIRCULAR);
    qtest_writel(qts, ADC2_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
    qtest_clock_step(qts, 9000);

    qtest_memread(qts, 0x20000100, adc_samples, sizeof(adc_samples));
    qtest_memread(qts, 0x20000200, cdr_samples, sizeof(cdr_samples));
    g_assert_cmphex(adc_samples[0], ==, UINT32_C(0x00000100));
    g_assert_cmphex(adc_samples[1], ==, UINT32_C(0x00000200));
    g_assert_cmphex(cdr_samples[0], ==, UINT32_C(0x01000100));
    g_assert_cmphex(cdr_samples[1], ==, UINT32_C(0x02000200));
    g_assert_cmphex(qtest_readl(qts, DMA1_S0NDTR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, DMA1_S1NDTR), ==, 0);
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 5));
    g_assert_true(qtest_readl(qts, DMA1_LISR) & (1u << 11));

    qtest_quit(qts);
}

static void assert_one_conversion_at(QTestState *qts, uint32_t ccr,
                                     int64_t period_ns)
{
    start_one_conversion(qts, ccr);
    qtest_clock_step(qts, period_ns - 1);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
}

static void program_offset_factors(QTestState *qts, uint32_t calfact_s,
                                   uint32_t calfact_d)
{
    uint32_t calfact;

    /* CALFACT_S and CALFACT_D share CALFACT_RES13.  Use two field writes,
     * preserving the first field exactly as firmware read-modify-writes it. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CALFACT_RES13, calfact_s & ADC_CALFACT_S_MASK);
    calfact = qtest_readl(qts, ADC1_CALFACT_RES13);
    qtest_writel(qts, ADC1_CALFACT_RES13,
                 calfact | ((calfact_d << 16) & ADC_CALFACT_D_MASK));
    g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT_RES13), ==,
                    (calfact_s & ADC_CALFACT_S_MASK) |
                    ((calfact_d << 16) & ADC_CALFACT_D_MASK));
}

static void test_calibration_factors_follow_difsel_regular_and_injected(void)
{
    const uint32_t calfact_s = 0x11u;
    const uint32_t calfact_d = 0x22u;
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* Both factors are programmable while ADC1 is enabled but idle. */
    program_offset_factors(qts, calfact_s, calfact_d);

    /* Channel 0 is single-ended: regular ADC_DR uses CALFACT_S. */
    qtest_writel(qts, ADC1_DIFSEL, 0);
    start_one_conversion(qts, 0);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100u - calfact_s);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    program_offset_factors(qts, calfact_s, calfact_d);
    qtest_writel(qts, ADC1_DIFSEL, ADC_DIFSEL_CHANNEL0);

    /* The same channel in the differential set uses CALFACT_D for DR. */
    start_one_conversion(qts, 0);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_DR), ==, 0x0100u - calfact_d);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_JSQR, 0);
    program_offset_factors(qts, calfact_s, calfact_d);

    /* Injected conversion also follows the single-ended selection. */
    qtest_writel(qts, ADC1_DIFSEL, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100u - calfact_s);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    qtest_writel(qts, ADC1_CFGR, ADC_CFGR_JQDIS);
    qtest_writel(qts, ADC1_JSQR, 0);
    program_offset_factors(qts, calfact_s, calfact_d);
    qtest_writel(qts, ADC1_DIFSEL, ADC_DIFSEL_CHANNEL0);

    /* ...and follows the differential selection through JDR1 as well. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_JADSTART);
    qtest_clock_step(qts, 12000);
    g_assert_cmphex(qtest_readl(qts, ADC1_JDR1), ==, 0x0100u - calfact_d);

    qtest_quit(qts);
}

static void test_linear_calibration_window_protocol(void)
{
    static const uint32_t values[6] = {
        0x32345678u, 0x21234567u, 0x11234566u,
        0x01234565u, 0x3a234564u, 0xffffffffu,
    };
    QTestState *qts = qtest_init("-machine dm-mc02");
    const uint32_t ready_mask = ADC_CR_LINCALRDY_MASK;
    uint32_t cr;

    /* Complete linear calibration, then enable ADC1 for the factor-window
     * hand-off protocol.  Calibration itself is only accepted while idle and
     * disabled on H723. */
    qtest_writel(qts, ADC1_CR, ADC_CR_ADCALLIN | ADC_CR_ADCAL);
    qtest_clock_step(qts, ADC_CALIBRATION_LINEAR_DELAY_NS);
    cr = qtest_readl(qts, ADC1_CR);
    g_assert_cmphex(cr & ADC_CR_ADCAL, ==, 0);
    g_assert_cmphex(cr & ready_mask, ==, ready_mask);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADCALLIN | ready_mask);

    for (unsigned word = 0; word < 6; ++word) {
        uint32_t bit = 1u << (22u + word);
        uint32_t mask = word == 5 ? ADC_LINEAR_LAST_WORD_MASK :
                                    ADC_LINEAR_WORD_MASK;
        uint32_t expected = values[word] & mask;

        /* Clearing a ready bit selects that word and exposes its saved
         * value.  All six words start at zero after calibration. */
        qtest_writel(qts, ADC1_CR,
                     ADC_CR_ADEN | ADC_CR_ADCALLIN | (ready_mask & ~bit));
        g_assert_cmphex(qtest_readl(qts, ADC1_CR) & bit, ==, 0);
        g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==, 0);

        /* While ready=0, CALFACT2 is the staging register.  Setting the
         * selected ready bit commits the masked value to that word. */
        qtest_writel(qts, ADC1_CALFACT2_RES14, values[word]);
        g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==, expected);
        qtest_writel(qts, ADC1_CR,
                     ADC_CR_ADEN | ADC_CR_ADCALLIN | ready_mask);

        /* Select it again to verify the committed value through the read
         * side of the same public window protocol. */
        qtest_writel(qts, ADC1_CR,
                     ADC_CR_ADEN | ADC_CR_ADCALLIN | (ready_mask & ~bit));
        g_assert_cmphex(qtest_readl(qts, ADC1_CALFACT2_RES14), ==, expected);
    }

    qtest_quit(qts);
}

static void test_cpu_pll1_does_not_disable_adc_clock(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* This is the board's CPU-only PLL1 setup.  ADC uses an independent
     * kernel source and must retain its reset-compatible clock until an ADC
     * source is explicitly selected. */
    qtest_writel(qts, RCC_PLLCKSELR, 2u | (2u << 4));
    qtest_writel(qts, RCC_PLL1DIVR, 39u);
    qtest_writel(qts, RCC_CR,
                 RCC_CR_HSION | RCC_CR_HSEON | RCC_CR_PLL1ON);
    qtest_writel(qts, RCC_CFGR, 3u);

    /* CCR=0 selects the reset-compatible 96 MHz kernel clock without a
     * common-register prescaler.  The board's /64 setting is exercised by
     * the dedicated prescaler test below. */
    assert_one_conversion_at(qts, 0, 188);
    qtest_quit(qts);
}

static void configure_adc_pll2p(QTestState *qts)
{
    /* HSE / 2 * 40 / 120 = 4 MHz. */
    qtest_writel(qts, RCC_PLLCKSELR, 2u | (2u << 12));
    qtest_writel(qts, RCC_PLL2DIVR, 39u | (119u << 9));
    qtest_writel(qts, RCC_PLLCFGR, RCC_PLLCFGR_PLL2DIVPEN);
    qtest_writel(qts, RCC_CR, RCC_CR_HSION | RCC_CR_HSEON | RCC_CR_PLL2ON);
    qtest_writel(qts, RCC_D3CCIPR, 0u << RCC_D3CCIPR_ADCSEL_SHIFT);
}

static void configure_adc_pll3r(QTestState *qts)
{
    /* HSE / 2 * 40 / 120 = 4 MHz. */
    qtest_writel(qts, RCC_PLLCKSELR, 2u | (2u << 20));
    qtest_writel(qts, RCC_PLL3DIVR, 39u | (119u << 24));
    qtest_writel(qts, RCC_PLLCFGR, RCC_PLLCFGR_PLL3DIVREN);
    qtest_writel(qts, RCC_CR, RCC_CR_HSION | RCC_CR_HSEON | RCC_CR_PLL3ON);
    qtest_writel(qts, RCC_D3CCIPR, 1u << RCC_D3CCIPR_ADCSEL_SHIFT);
}

static void configure_adc_clkp_hse(QTestState *qts)
{
    qtest_writel(qts, RCC_CR, RCC_CR_HSION | RCC_CR_HSEON);
    qtest_writel(qts, RCC_D1CCIPR, 2u << RCC_D1CCIPR_CKPERSEL_SHIFT);
    qtest_writel(qts, RCC_D3CCIPR, 2u << RCC_D3CCIPR_ADCSEL_SHIFT);
}

static void test_kernel_clock_source_timing(void)
{
    QTestState *qts;

    qts = qtest_init("-machine dm-mc02,adc-accurate-timing=on");
    configure_adc_pll2p(qts);
    assert_one_conversion_at(qts, 0, 4500);
    qtest_quit(qts);

    qts = qtest_init("-machine dm-mc02,adc-accurate-timing=on");
    configure_adc_pll3r(qts);
    assert_one_conversion_at(qts, 0, 4500);
    qtest_quit(qts);

    qts = qtest_init("-machine dm-mc02,adc-accurate-timing=on");
    configure_adc_clkp_hse(qts);
    /* 18 cycles at 24 MHz = 750 ns. */
    assert_one_conversion_at(qts, 0, 750);
    qtest_quit(qts);
}

static void test_common_clock_prescaler_boundaries(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    configure_adc_pll2p(qts);
    /* Code 0 is /1: 4 MHz gives a 4500 ns rank period. */
    assert_one_conversion_at(qts, 0u << ADC_CCR_PRESC_SHIFT, 4500);

    qtest_writel(qts, ADC1_ISR, ADC_ISR_EOC);
    /* Code 11 is /256: 4 MHz / 256 gives a 1152000 ns period. */
    assert_one_conversion_at(qts, 11u << ADC_CCR_PRESC_SHIFT, 1152000);
    g_assert_cmphex(qtest_readl(qts, ADC12_CCR), ==,
                    11u << ADC_CCR_PRESC_SHIFT);

    qtest_quit(qts);
}

static void test_common_clock_prescaler(void)
{
    uint32_t ccr = 0u << ADC_CCR_PRESC_SHIFT;
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* CCR readback plus the conversion deadline makes the common-clock
     * callback observable through the public qtest API. */
    start_one_conversion(qts, ccr);
    g_assert_cmphex(qtest_readl(qts, ADC12_CCR), ==, ccr);
    qtest_clock_step(qts, 1000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");

    /* PRESC code 11 selects /256; its conversion takes over 20 us. */
    ccr = 11u << ADC_CCR_PRESC_SHIFT;
    start_one_conversion(qts, ccr);
    g_assert_cmphex(qtest_readl(qts, ADC12_CCR), ==, ccr);
    qtest_clock_step(qts, 1000);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 50000);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

static void test_common_clock_hpre_modes(void)
{
    static const uint32_t hpre_cases[] = {
        0x0u, 0x8u, 0x9u, 0xau, 0xbu, 0xcu, 0xdu, 0xeu, 0xfu,
    };
    static const uint32_t ckmode_dividers[] = { 1u, 2u, 4u };
    static const uint32_t hpre_dividers[] = {
        1u, 2u, 4u, 8u, 16u, 64u, 128u, 256u, 512u,
    };

    for (size_t i = 0; i < ARRAY_SIZE(hpre_cases); ++i) {
        for (size_t mode = 0; mode < ARRAY_SIZE(ckmode_dividers); ++mode) {
            QTestState *qts = qtest_init(
                "-machine dm-mc02,adc-accurate-timing=on");
            uint32_t ckmode = (uint32_t)mode + 1u;
            uint64_t clock_hz = 64000000u / 8u / hpre_dividers[i];
            int64_t period_ns;

            /* ST CMSIS SystemCoreClockUpdate(): HCLK follows D1CPRE then
             * HPRE. RM0468 ADC_CCR.CKMODE selects HCLK / {1,2,4}. */
            clock_hz /= ckmode_dividers[mode];
            period_ns = (18 * INT64_C(1000000000) + clock_hz - 1) /
                        clock_hz;
            qtest_writel(qts, RCC_D1CFGR,
                         (0xau << 8) | hpre_cases[i]);
            assert_one_conversion_at(qts,
                                     ckmode << ADC_CCR_CKMODE_SHIFT,
                                     period_ns);
            qtest_quit(qts);
        }
    }
}

static void test_common_clock_hpre_live_change_preserves_remaining_work(void)
{
    QTestState *qts = qtest_init(
        "-machine dm-mc02,adc-accurate-timing=on");

    /* At HCLK=64 MHz an 18-cycle rank is 281.25 ns, rounded to 282 ns.
     * After 100 ns, 12 of 36 half-cycles have elapsed.  Changing HPRE to
     * /2 leaves 24 half-cycles at 32 MHz, which take 375 ns. */
    qtest_writel(qts, RCC_D1CFGR, 0x0u);
    start_one_conversion(qts, 1u << ADC_CCR_CKMODE_SHIFT);
    qtest_clock_step(qts, 100);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_writel(qts, RCC_D1CFGR, 0x8u);
    qtest_clock_step(qts, 374);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);

    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/adc/cpu-pll1-independent",
                    test_cpu_pll1_does_not_disable_adc_clock);
    g_test_add_func("/dm-mc02/adc/kernel-clock-source-timing",
                    test_kernel_clock_source_timing);
    g_test_add_func("/dm-mc02/adc/common-clock-prescaler-boundaries",
                    test_common_clock_prescaler_boundaries);
    g_test_add_func("/dm-mc02/adc/adrdy-lifecycle", test_adrdy_lifecycle);
    g_test_add_func("/dm-mc02/adc/deeppwd-advregen-low-power-lifecycle",
                    test_deeppwd_advregen_low_power_lifecycle);
    g_test_add_func("/dm-mc02/adc/common-clock-prescaler",
                    test_common_clock_prescaler);
    g_test_add_func("/dm-mc02/adc/common-clock-hpre-modes",
                    test_common_clock_hpre_modes);
    g_test_add_func("/dm-mc02/adc/common-clock-hpre-live-change",
                    test_common_clock_hpre_live_change_preserves_remaining_work);
    g_test_add_func("/dm-mc02/adc/calibration-command-constraints",
                    test_calibration_command_constraints);
    g_test_add_func("/dm-mc02/adc/command-rs-bits-preserve-zero-writes",
                    test_command_rs_bits_preserve_zero_writes);
    g_test_add_func("/dm-mc02/adc/jadstart-regular-calibration-boundaries",
                    test_jadstart_regular_and_calibration_boundaries);
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
    g_test_add_func("/dm-mc02/adc/cr-byte-halfword-lanes",
                    test_cr_byte_and_halfword_lanes);
    g_test_add_func("/dm-mc02/adc/repeated-adstart-rank-progress",
                    test_repeated_adstart_keeps_rank_progress);
    g_test_add_func("/dm-mc02/adc/regular-sequence-sqr2-sqr3",
                    test_regular_sequence_sqr2_sqr3);
    g_test_add_func("/dm-mc02/adc/regular-sequence-sqr4",
                    test_regular_sequence_sqr4);
    g_test_add_func("/dm-mc02/adc/autowait-holds-until-dr-read",
                    test_autowait_holds_until_dr_read);
    g_test_add_func("/dm-mc02/adc/regular-dma-overrun-request-gate",
                    test_regular_dma_overrun_request_gate);
    g_test_add_func("/dm-mc02/adc/16bit-rank-conversion-deadline",
                    test_16bit_rank_conversion_deadline);
    g_test_add_func("/dm-mc02/adc/resolution-processing-deadlines",
                    test_resolution_processing_deadlines);
    g_test_add_func("/dm-mc02/adc/16bit-rank-dma-consumer-deadline",
                    test_16bit_rank_dma_consumer_deadline);
    g_test_add_func("/dm-mc02/adc/tim8-busy-trigger-not-queued",
                    test_tim8_busy_trigger_is_not_queued);
    g_test_add_func("/dm-mc02/adc/regular-discontinuous-external-subgroups",
                    test_regular_discontinuous_external_subgroups);
    g_test_add_func("/dm-mc02/adc/regular-discontinuous-software-full-sequence",
                    test_regular_discontinuous_software_is_full_sequence);
    g_test_add_func("/dm-mc02/adc/regular-discontinuous-autowait-boundary",
                    test_regular_discontinuous_autowait_boundary);
    g_test_add_func("/dm-mc02/adc/calibration-effective-clock",
                    test_calibration_uses_effective_clock);
    g_test_add_func("/dm-mc02/adc/active-clock-change-remaining-rank",
                    test_active_adc_clock_change_preserves_remaining_work);
    g_test_add_func("/dm-mc02/adc/calfact-res13-mask",
                    test_calfact_res13_mask);
    g_test_add_func("/dm-mc02/adc/calfact2-res14-mask",
                    test_calfact2_res14_mask);
    g_test_add_func("/dm-mc02/adc/calfact-reset", test_calfact_reset);
    g_test_add_func("/dm-mc02/adc/calfact-difsel-regular-injected",
                    test_calibration_factors_follow_difsel_regular_and_injected);
    g_test_add_func("/dm-mc02/adc/linear-calibration-window-protocol",
                    test_linear_calibration_window_protocol);
    g_test_add_func("/dm-mc02/adc/adc2-regular-conversion-independent",
                    test_adc2_regular_conversion_is_independent);
    g_test_add_func("/dm-mc02/adc/common-ccr-lane-writes-clock",
                    test_common_ccr_lane_writes_reconfigure_clock);
    g_test_add_func("/dm-mc02/adc/common-csr-status-projection",
                    test_common_csr_projects_both_adc_status_blocks);
    g_test_add_func("/dm-mc02/adc/common-cdr-regular-simultaneous",
                    test_common_cdr_packs_regular_simultaneous_results);
    g_test_add_func("/dm-mc02/adc/common-cdr-read-acknowledges-both-eoc",
                    test_common_cdr_read_acknowledges_both_eoc);
    g_test_add_func("/dm-mc02/adc/common-cdr2-regular-interleaved",
                    test_common_cdr2_publishes_interleaved_regular_result);
    g_test_add_func("/dm-mc02/adc/common-master-start-drives-slave",
                    test_common_master_start_drives_slave);
    g_test_add_func("/dm-mc02/adc/common-external-trigger-drives-slave",
                    test_common_external_trigger_drives_slave);
    g_test_add_func("/dm-mc02/adc/common-interleaved-external-trigger-cadence",
                    test_common_interleaved_external_trigger_cadence);
    g_test_add_func("/dm-mc02/adc/common-interleaved-injected-simultaneous",
                    test_common_interleaved_injected_simultaneous);
    g_test_add_func("/dm-mc02/adc/common-cdr2-dma-endpoint",
                    test_common_cdr2_dma_endpoint);
    g_test_add_func("/dm-mc02/adc/common-cdr2-dma-endpoint-reservation-retry",
                    test_common_cdr2_dma_endpoint_reservation_retry);
    g_test_add_func("/dm-mc02/adc/common-cdr2-dma-mmio",
                    test_common_cdr2_dma_mmio);
    g_test_add_func("/dm-mc02/adc/common-interleaved-damdf8-dma-endpoint",
                    test_common_interleaved_damdf8_dma_endpoint);
    g_test_add_func("/dm-mc02/adc/common-interleaved-damdf8-dma-mmio",
                    test_common_interleaved_damdf8_dma_mmio);
    g_test_add_func("/dm-mc02/adc/common-multimode-dma-endpoint",
                    test_common_multimode_dma_endpoint);
    g_test_add_func("/dm-mc02/adc/common-multimode-dma-mmio",
                    test_common_multimode_dma_mmio);
    g_test_add_func("/dm-mc02/adc/common-multimode-dma-endpoint-reservation-retry",
                    test_common_multimode_dma_endpoint_reservation_retry);
    for (unsigned flags = 0; flags < 8; ++flags) {
        g_autofree char *name = g_strdup_printf(
            "/dm-mc02/adc/fifo-%s-%s-%s", flags & 1 ? "cdr2" : "cdr",
            flags & 2 ? "endpoint" : "mmio",
            flags & 4 ? "write-error" : "success");
        g_test_add_data_func(name, GUINT_TO_POINTER(flags),
                             test_common_fifo_consuming);
    }
    g_test_add_data_func("/dm-mc02/adc/common-dma-retry-master-ovr",
                         GUINT_TO_POINTER(1), test_common_dma_overrun_retry);
    g_test_add_data_func("/dm-mc02/adc/common-dma-retry-slave-ovr",
                         GUINT_TO_POINTER(2), test_common_dma_overrun_retry);
    g_test_add_data_func("/dm-mc02/adc/common-dma-retry-both-ovr",
                         GUINT_TO_POINTER(3), test_common_dma_overrun_retry);
    g_test_add_func("/dm-mc02/adc/common-multimode-dma-overrun-gate-endpoint",
                    test_common_multimode_dma_overrun_request_gate_endpoint);
    g_test_add_func("/dm-mc02/adc/common-multimode-dma-overrun-gate-mmio",
                    test_common_multimode_dma_overrun_request_gate_mmio);
    g_test_add_func("/dm-mc02/adc/common-multimode-dma-request-cache-key",
                    test_common_multimode_dma_request_cache_key);
    return g_test_run();
}
