/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "dm-mc02-adc-test-common.h"

void configure_timer_fixture_clock(QTestState *qts)
{
    /* Keep timer edge timestamps independent from the HSI reset rate.  The
     * H723 fixture uses HCLK=32 MHz and APB2=/2, so TIM8 runs at 32 MHz. */
    qtest_writel(qts, RCC_D1CFGR, 0x8);
    qtest_writel(qts, RCC_D2CFGR, 0x440);
}

void start_one_conversion(QTestState *qts, uint32_t ccr)
{
    /* A direct SQR1 write selects the one-rank regular sequence. */
    qtest_writel(qts, ADC12_CCR, ccr);
    qtest_writel(qts, ADC1_SQR1, 0);
    qtest_writel(qts, ADC1_CR, ADC_CR_ADEN | ADC_CR_ADSTART);
}

void configure_adc_pll2p(QTestState *qts)
{
    /* HSE / 2 * 40 / 120 = 4 MHz. */
    qtest_writel(qts, RCC_PLLCKSELR, 2u | (2u << 12));
    qtest_writel(qts, RCC_PLL2DIVR, 39u | (119u << 9));
    qtest_writel(qts, RCC_PLLCFGR, RCC_PLLCFGR_PLL2DIVPEN);
    qtest_writel(qts, RCC_CR, RCC_CR_HSION | RCC_CR_HSEON | RCC_CR_PLL2ON);
    qtest_writel(qts, RCC_D3CCIPR, 0u << RCC_D3CCIPR_ADCSEL_SHIFT);
}

