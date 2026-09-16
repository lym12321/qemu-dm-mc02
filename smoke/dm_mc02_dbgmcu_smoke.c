#include <stdint.h>

#define DBGMCU_BASE 0x5c001000u
#define DBGMCU_IDCODE (*(volatile uint32_t *)(DBGMCU_BASE + 0x00u))
#define DBGMCU_CR     (*(volatile uint32_t *)(DBGMCU_BASE + 0x04u))
#define RESULT        ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    uint32_t idcode = DBGMCU_IDCODE;

    RESULT[0] = 0x44424731u; /* DBG1 */
    RESULT[1] = idcode;
    DBGMCU_IDCODE = 0xffffffffu;
    RESULT[2] = DBGMCU_IDCODE;
    DBGMCU_CR = 0xa5a55a5au;
    RESULT[3] = DBGMCU_CR;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
