#include <stdint.h>

#define CALIBRATION_BASE 0x1ff1e000u
#define RESULT ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static volatile uint32_t *calibration_word(uint32_t offset)
{
    return (volatile uint32_t *)(uintptr_t)(CALIBRATION_BASE + offset);
}

static volatile uint16_t *calibration_halfword(uint32_t offset)
{
    return (volatile uint16_t *)(uintptr_t)(CALIBRATION_BASE + offset);
}

void Reset_Handler(void)
{
    volatile uint32_t *uid0 = calibration_word(0x800u);
    volatile uint32_t *uid1 = calibration_word(0x804u);
    volatile uint32_t *uid2 = calibration_word(0x808u);
    volatile uint16_t *cal0 = calibration_halfword(0x820u);
    volatile uint16_t *cal1 = calibration_halfword(0x840u);
    volatile uint16_t *cal2 = calibration_halfword(0x860u);

    RESULT[0] = 0x43414c31u; /* CAL1 */
    RESULT[1] = *uid0;
    RESULT[2] = *uid1;
    RESULT[3] = *uid2;
    RESULT[4] = *cal0;
    RESULT[5] = *cal1;
    RESULT[6] = *cal2;

    /* Exercise both access widths. A real calibration ROM must ignore all
     * CPU stores while preserving its factory contents. */
    *uid0 = 0xa5a5a5a5u;
    *uid1 = 0x5a5a5a5au;
    *uid2 = 0xffffffffu;
    *cal0 = 0x1111u;
    *cal1 = 0x2222u;
    *cal2 = 0x3333u;

    RESULT[7] = *uid0;
    RESULT[8] = *uid1;
    RESULT[9] = *uid2;
    RESULT[10] = *cal0;
    RESULT[11] = *cal1;
    RESULT[12] = *cal2;
    RESULT[13] = 0x43414c32u; /* CAL2 */

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
