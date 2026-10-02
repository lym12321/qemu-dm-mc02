/* SPDX-License-Identifier: GPL-2.0-or-later */

#ifndef DM_MC02_ADC_TEST_COMMON_H
#define DM_MC02_ADC_TEST_COMMON_H

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

void configure_timer_fixture_clock(QTestState *qts);
void configure_adc_pll2p(QTestState *qts);
void start_one_conversion(QTestState *qts, uint32_t ccr);

void dm_mc02_adc_register_clock_tests(void);
void dm_mc02_adc_register_trigger_tests(void);
void dm_mc02_adc_register_common_tests(void);

#endif /* DM_MC02_ADC_TEST_COMMON_H */
