#include <stdint.h>

#define FLASH_BASE 0x08000000u
#define FLASH_KEYR1 (*(volatile uint32_t *)0x52002004u)
#define FLASH_CR1   (*(volatile uint32_t *)0x5200200cu)
#define FLASH_SR1   (*(volatile uint32_t *)0x52002010u)

#define KEY1 0x45670123u
#define KEY2 0xcdef89abu
#define CR_SER   (1u << 2)
#define CR_PG    (1u << 1)
#define CR_LOCK  (1u << 0)
#define CR_START (1u << 7)
#define SR_EOP   (1u << 16)
#define SR_PGSERR (1u << 18)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;
    volatile uint32_t *target = (volatile uint32_t *)(FLASH_BASE + 0x20000u);
    volatile uint32_t *boot_count = (volatile uint32_t *)0x20000020u;

    if (++*boot_count > 1) {
        /* QMP system_reset is a warm reset: Flash contents survive, while
         * the controller must return locked with program writes disabled. */
        result[6] = FLASH_CR1;
        *target = 0x12345678u;
        result[7] = *target;
        result[8] = (FLASH_CR1 & CR_LOCK) != 0;
        result[9] = 0x52535432u; /* "RST2" */
        for (;;) {
            __asm__ volatile ("wfi");
        }
    }

    result[0] = 0x464c5348u;
    FLASH_KEYR1 = KEY1;
    FLASH_KEYR1 = KEY2;
    FLASH_CR1 = CR_PG;
    *target = 0x12345678u;
    result[1] = *target;
    *target = 0xffffffffu;
    result[2] = FLASH_SR1;
    FLASH_SR1 = SR_PGSERR;

    /* Sector 1 is 0x20000..0x3ffff in the 1 MiB model. */
    FLASH_CR1 = CR_SER | (1u << 8) | CR_START;
    result[3] = FLASH_SR1;
    result[4] = *target;
    result[5] = (result[3] & SR_EOP) != 0 &&
                !(result[3] & SR_PGSERR);
    for (;;) {
        __asm__ volatile ("wfi");
    }
}
