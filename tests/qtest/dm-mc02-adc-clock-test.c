/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "dm-mc02-adc-test-common.h"

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

static void assert_one_conversion_at(QTestState *qts, uint32_t ccr,
                                     int64_t period_ns)
{
    start_one_conversion(qts, ccr);
    qtest_clock_step(qts, period_ns - 1);
    g_assert_false(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
    qtest_clock_step(qts, 1);
    g_assert_true(qtest_readl(qts, ADC1_ISR) & ADC_ISR_EOC);
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

void dm_mc02_adc_register_clock_tests(void)
{
    g_test_add_func("/dm-mc02/adc/cpu-pll1-independent",
                    test_cpu_pll1_does_not_disable_adc_clock);
    g_test_add_func("/dm-mc02/adc/kernel-clock-source-timing",
                    test_kernel_clock_source_timing);
    g_test_add_func("/dm-mc02/adc/common-clock-prescaler-boundaries",
                    test_common_clock_prescaler_boundaries);
    g_test_add_func("/dm-mc02/adc/common-clock-prescaler",
                    test_common_clock_prescaler);
    g_test_add_func("/dm-mc02/adc/common-clock-hpre-modes",
                    test_common_clock_hpre_modes);
    g_test_add_func("/dm-mc02/adc/common-clock-hpre-live-change",
                    test_common_clock_hpre_live_change_preserves_remaining_work);
    g_test_add_func("/dm-mc02/adc/16bit-rank-conversion-deadline",
                    test_16bit_rank_conversion_deadline);
    g_test_add_func("/dm-mc02/adc/resolution-processing-deadlines",
                    test_resolution_processing_deadlines);
    g_test_add_func("/dm-mc02/adc/16bit-rank-dma-consumer-deadline",
                    test_16bit_rank_dma_consumer_deadline);
    g_test_add_func("/dm-mc02/adc/calibration-effective-clock",
                    test_calibration_uses_effective_clock);
    g_test_add_func("/dm-mc02/adc/active-clock-change-remaining-rank",
                    test_active_adc_clock_change_preserves_remaining_work);
    g_test_add_func("/dm-mc02/adc/common-ccr-lane-writes-clock",
                    test_common_ccr_lane_writes_reconfigure_clock);
}
