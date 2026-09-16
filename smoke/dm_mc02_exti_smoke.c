#include <stdint.h>

#define EXTI_RTSR1  (*(volatile uint32_t *)0x58000000u)
#define EXTI_FTSR1  (*(volatile uint32_t *)0x58000004u)
#define EXTI_SWIER1 (*(volatile uint32_t *)0x58000008u)
#define EXTI_PR1    (*(volatile uint32_t *)0x58000014u)
#define EXTI_C1IMR1 (*(volatile uint32_t *)0x58000080u)
#define SYSCFG_EXTICR4 (*(volatile uint32_t *)0x58000414u)
#define NVIC_ISER1  (*(volatile uint32_t *)0xe000e104u)
#define RESULT      ((volatile uint32_t *)0x20000000u)

#define LINE        (1u << 15)
#define LINE14      (1u << 14)
#define IRQ         40u
#define MARKER      0x45585431u /* EXT1 */
#define IRQ_MARKER  0x45585432u /* EXT2 */
#define DONE        0x444f4e45u

void Reset_Handler(void);
void EXTI15_10_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + IRQ + 1u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + IRQ] = (uint32_t)(uintptr_t)EXTI15_10_IRQHandler,
};

void EXTI15_10_IRQHandler(void)
{
    RESULT[1]++;
    RESULT[2] = IRQ_MARKER;
    RESULT[3] = EXTI_PR1;
    EXTI_PR1 = RESULT[3];
    RESULT[4] = EXTI_PR1;
    if (RESULT[1] == 3) {
        /* Move EXTI14 to GPIOB while the guest is running. */
        EXTI_RTSR1 = LINE14;
        SYSCFG_EXTICR4 = 1u << 8;
    } else if (RESULT[1] == 4) {
        RESULT[5] = DONE;
    }
}

void Reset_Handler(void)
{
    RESULT[0] = MARKER;
    RESULT[1] = 0;
    EXTI_FTSR1 = LINE | LINE14;
    EXTI_C1IMR1 = LINE | LINE14;
    NVIC_ISER1 = 1u << (IRQ - 32u);
    /* Software trigger uses the same pending/IRQ path as an external edge. */
    EXTI_SWIER1 = LINE;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
