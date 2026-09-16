#include <stdint.h>

#define CRC_BASE 0x58024c00u
#define CRC_DR32 (*(volatile uint32_t *)(CRC_BASE + 0x00u))
#define CRC_DR8  (*(volatile uint8_t *)(CRC_BASE + 0x00u))
#define CRC_CR   (*(volatile uint32_t *)(CRC_BASE + 0x08u))
#define CRC_INIT (*(volatile uint32_t *)(CRC_BASE + 0x10u))
#define CRC_POL  (*(volatile uint32_t *)(CRC_BASE + 0x14u))
#define RESULT   ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static uint32_t crc_bytes(const char *text)
{
    for (const char *p = text; *p; ++p) {
        CRC_DR8 = (uint8_t)*p;
    }
    return CRC_DR32;
}

void Reset_Handler(void)
{
    RESULT[0] = 0x43524331u; /* CRC1 */
    CRC_CR = 1u;
    RESULT[1] = crc_bytes("123456789");

    CRC_POL = 0x07u;
    CRC_INIT = 0;
    CRC_CR = (2u << 3) | 1u; /* CRC-8, reset */
    RESULT[2] = crc_bytes("123456789");

    CRC_POL = 0x1021u;
    CRC_INIT = 0xffffu;
    CRC_CR = (1u << 3) | 1u; /* CRC-16, reset */
    RESULT[3] = crc_bytes("123456789");

    CRC_CR = 1u;
    CRC_DR8 = 'x';
    CRC_CR = 1u;
    RESULT[4] = CRC_DR32;
    RESULT[5] = 1;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
