#include <stdint.h>

#define USART1_CR1  (*(volatile uint32_t *)0x40011000u)
#define USART1_ISR  (*(volatile uint32_t *)0x4001101cu)
#define USART1_RDR  (*(volatile uint32_t *)0x40011024u)
#define USART1_TDR  (*(volatile uint32_t *)0x40011028u)
#define RESULT      ((volatile uint32_t *)0x20000000u)

#define CR1_RE (1u << 2)
#define CR1_TE (1u << 3)
#define ISR_RXNE (1u << 5)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    USART1_CR1 = CR1_RE | CR1_TE;
    RESULT[0] = 0x55415231u; /* UAR1 */
    USART1_TDR = 'O';
    while ((USART1_ISR & ISR_RXNE) == 0) {
    }
    RESULT[1] = USART1_RDR;
    RESULT[2] = 0x55415232u; /* UAR2 */
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
