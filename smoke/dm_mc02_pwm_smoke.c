#include <stdint.h>

#define GPIOB_MODER (*(volatile uint32_t *)0x58020400u)
#define GPIOB_AFR1  (*(volatile uint32_t *)0x58020424u)
#define TIM12_CR1   (*(volatile uint32_t *)0x40001800u)
#define TIM12_CCMR1 (*(volatile uint32_t *)0x40001818u)
#define TIM12_CCER  (*(volatile uint32_t *)0x40001820u)
#define TIM12_PSC   (*(volatile uint32_t *)0x40001828u)
#define TIM12_ARR   (*(volatile uint32_t *)0x4000182cu)
#define TIM12_CCR2  (*(volatile uint32_t *)0x40001838u)

#define RESULT (*(volatile uint32_t *)0x20000000u)
#define RESULT_MARKER 0x50574d31u /* "PWM1" */

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    /* PB15 = AF2/TIM12_CH2.  D2CFGR reset leaves APB1 at /1, so the reset
     * 64 MHz HCLK also feeds TIM12.  This gives 64 MHz / (640 * 100) =
     * 1 kHz and a 25% PWM duty cycle. */
    GPIOB_MODER = 2u << 30;
    GPIOB_AFR1 = 2u << 28;
    TIM12_PSC = 639;
    TIM12_ARR = 99;
    TIM12_CCMR1 = (6u << 12) | (1u << 11); /* PWM1 + OC2PE */
    TIM12_CCER = 1u << 4;                  /* CC2E */
    TIM12_CCR2 = 25;
    TIM12_CR1 = 1;                         /* CEN */
    RESULT = RESULT_MARKER;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
