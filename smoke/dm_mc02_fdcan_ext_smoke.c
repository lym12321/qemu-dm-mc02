#include <stdint.h>

#define FDCAN 0x4000a000u
#define REG(off) (*(volatile uint32_t *)(FDCAN + (off)))
#define IR 0x050u
#define IE 0x054u
#define ILE 0x05cu
#define XIDFC 0x088u
#define GFC 0x080u
#define RXF1C 0x0b0u
#define RXF1S 0x0b4u
#define RXF1A 0x0b8u
#define RXESC 0x0bcu
#define CCCR 0x018u
#define MSGRAM ((volatile uint32_t *)0x4000ac00u)
#define RESULT ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = { 0x20020000u, (uint32_t)(uintptr_t)Reset_Handler };

void Reset_Handler(void)
{
    REG(CCCR) = 1;
    REG(RXESC) = 0;
    REG(RXF1C) = 32u | (2u << 16);
    /* Reject non-matching standard and extended frames, matching the
     * board's HAL_FDCAN_ConfigGlobalFilter() policy. */
    REG(GFC) = (2u << 4) | (2u << 2) | (1u << 1) | 1u;
    /* Extended dual-ID filter: EFID1=0x123456, EFID2=0x1abcde,
     * EFT=dual ID, EFEC=FIFO1. */
    /* FLSSA is a 32-bit word address. Filter element 128 is therefore at
     * word 128, i.e. byte offset 512 in message RAM. */
    REG(XIDFC) = 128u | (1u << 16);
    MSGRAM[128] = 0x123456u | (6u << 29); /* FIFO1 + high priority */
    MSGRAM[129] = 0x1abcdeu | (1u << 30);
    REG(IE) = (1u << 4) | (1u << 8); /* RF1N + HPM notifications */
    REG(ILE) = 1;
    REG(CCCR) = 0;

    while ((REG(RXF1S) & 0x7fu) == 0) {
    }
    RESULT[0] = 0x45584631u; /* EXF1 */
    RESULT[1] = REG(RXF1S);
    RESULT[2] = MSGRAM[32];
    RESULT[3] = MSGRAM[34];
    RESULT[4] = MSGRAM[35];
    RESULT[5] = REG(IR);
    RESULT[7] = REG(0x094); /* HPMS */
    REG(RXF1A) = (REG(RXF1S) >> 8) & 0x3f;
    RESULT[6] = REG(RXF1S);
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
