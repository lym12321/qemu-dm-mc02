#include <stdint.h>

#define USART1_CR1       (*(volatile uint32_t *)0x40011000u)
#define USART1_ISR       (*(volatile uint32_t *)0x4001101cu)
#define USART1_ICR       (*(volatile uint32_t *)0x40011020u)
#define NVIC_ISER1       (*(volatile uint32_t *)0xe000e104u)
#define CR1_RE           (1u << 2)
#define CR1_IDLEIE       (1u << 4)
#define ISR_IDLE         (1u << 4)
#define ICR_IDLECF       (1u << 4)
#define RESULT           ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);
void USART1_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16 + 38] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16 + 37] = (uint32_t)(uintptr_t)USART1_IRQHandler,
};

void USART1_IRQHandler(void)
{
    uint32_t isr = USART1_ISR;

    RESULT[0] = isr;
    if (isr & ISR_IDLE) {
        RESULT[1] = 0x49444c45u; /* IDLE */
        USART1_ICR = ICR_IDLECF;
        RESULT[2] = USART1_ISR;
    }
}

void Reset_Handler(void)
{
    RESULT[3] = 0x52454144u; /* READ */
    NVIC_ISER1 = 1u << (37u - 32u);
    USART1_CR1 = CR1_RE | CR1_IDLEIE;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
