/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "dm-mc02-adc-test-common.h"

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

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/adc/adrdy-lifecycle", test_adrdy_lifecycle);
    g_test_add_func("/dm-mc02/adc/deeppwd-advregen-low-power-lifecycle",
                    test_deeppwd_advregen_low_power_lifecycle);
    g_test_add_func("/dm-mc02/adc/calibration-command-constraints",
                    test_calibration_command_constraints);
    g_test_add_func("/dm-mc02/adc/command-rs-bits-preserve-zero-writes",
                    test_command_rs_bits_preserve_zero_writes);
    g_test_add_func("/dm-mc02/adc/jadstart-regular-calibration-boundaries",
                    test_jadstart_regular_and_calibration_boundaries);
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
    g_test_add_func("/dm-mc02/adc/regular-discontinuous-software-full-sequence",
                    test_regular_discontinuous_software_is_full_sequence);
    g_test_add_func("/dm-mc02/adc/calfact-res13-mask",
                    test_calfact_res13_mask);
    g_test_add_func("/dm-mc02/adc/calfact2-res14-mask",
                    test_calfact2_res14_mask);
    g_test_add_func("/dm-mc02/adc/calfact-reset", test_calfact_reset);
    g_test_add_func("/dm-mc02/adc/calfact-difsel-regular-injected",
                    test_calibration_factors_follow_difsel_regular_and_injected);
    g_test_add_func("/dm-mc02/adc/linear-calibration-window-protocol",
                    test_linear_calibration_window_protocol);
    dm_mc02_adc_register_clock_tests();
    dm_mc02_adc_register_trigger_tests();
    dm_mc02_adc_register_common_tests();
    return g_test_run();
}
