#include <stdint.h>

#define CORDIC_BASE 0x58004400u
#define CORDIC_CSR  (*(volatile uint32_t *)(CORDIC_BASE + 0x00u))
#define CORDIC_WDATA (*(volatile uint32_t *)(CORDIC_BASE + 0x04u))
#define CORDIC_RDATA (*(volatile uint32_t *)(CORDIC_BASE + 0x08u))
#define RESULT ((volatile uint32_t *)0x20000000u)

#define CSR_NRES    (1u << 19)
#define CSR_ARGSIZE (1u << 22)
#define CSR_RRDY    (1u << 31)
#define CSR_ERROR   (1u << 30)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    uint32_t csr;

    RESULT[0] = 0x434f5231u; /* COR1 */
    RESULT[1] = CORDIC_CSR;

    /* FUNC=0 is cosine and FUNC=1 is sine.  Q1.31 angle 0.25 is pi/4. */
    CORDIC_CSR = CSR_NRES;
    CORDIC_WDATA = 0x20000000u;
    csr = CORDIC_CSR;
    RESULT[2] = csr;
    RESULT[3] = CORDIC_RDATA;
    RESULT[4] = CORDIC_RDATA;
    RESULT[5] = CORDIC_CSR;

    /* Explicitly exercise HAL's FUNC=1 sine encoding. */
    CORDIC_CSR = 1u;
    CORDIC_WDATA = 0x20000000u;
    RESULT[6] = CORDIC_RDATA;

    /* ARGSize=16-bit is deliberately outside this minimal Q1.31 slice. */
    CORDIC_CSR = CSR_ARGSIZE;
    CORDIC_WDATA = 0;
    RESULT[7] = CORDIC_CSR;
    RESULT[8] = CORDIC_RDATA;

    /* A CSR write resets the input packet and clears the model diagnostic. */
    CORDIC_CSR = 0;
    RESULT[9] = CORDIC_CSR;
    RESULT[10] = 1;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
