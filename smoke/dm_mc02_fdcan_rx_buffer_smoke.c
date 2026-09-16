#include <stdint.h>

#define FDCAN_RXBC  (*(volatile uint32_t *)0x4000a0acu)
#define FDCAN_CCCR  (*(volatile uint32_t *)0x4000a018u)
#define FDCAN_NDAT1 (*(volatile uint32_t *)0x4000a098u)
#define FDCAN_NDAT2 (*(volatile uint32_t *)0x4000a09cu)
#define FDCAN_RXF0S (*(volatile uint32_t *)0x4000a0a4u)
#define FDCAN_RXESC (*(volatile uint32_t *)0x4000a0bcu)
#define FDCAN_SIDFC (*(volatile uint32_t *)0x4000a084u)
#define FDCAN_XIDFC (*(volatile uint32_t *)0x4000a088u)
#define FDCAN_IE    (*(volatile uint32_t *)0x4000a054u)
#define FDCAN_ILE   (*(volatile uint32_t *)0x4000a05cu)
#define FDCAN_IR    (*(volatile uint32_t *)0x4000a050u)
#define MSGRAM      ((volatile uint32_t *)0x4000ac00u)
#define RESULT      ((volatile uint32_t *)0x20000000u)
#define NVIC_ISER0  (*(volatile uint32_t *)0xe000e100u)

#define RESULT_PASS 0x52425831u /* RBX1 */
#define RESULT_STD  0x52425832u /* RBX2 */
#define RESULT_STD2 0x52425833u /* RBX3: buffer released */
#define RESULT_STD3 0x52425834u /* RBX4: second frame accepted */
#define RESULT_FAIL 0x52425846u /* RBXF */
#define FDCAN_IR_DRX (1u << 19)
#define BUFFER_INDEX_STD 5u
#define BUFFER_INDEX_RELEASE 6u
#define BUFFER_INDEX_EXT 32u
#define BUFFER_WORD_OFFSET_STD (0x200u / 4u + BUFFER_INDEX_STD * 4u)
#define BUFFER_WORD_OFFSET_RELEASE (0x200u / 4u + BUFFER_INDEX_RELEASE * 4u)
#define BUFFER_WORD_OFFSET_EXT (0x200u / 4u + BUFFER_INDEX_EXT * 4u)

void Reset_Handler(void);
__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[36] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    volatile uint32_t *filter = MSGRAM + 0x100u / 4u;
    volatile uint32_t *filter_ext = MSGRAM + 0x120u / 4u;

    RESULT[0] = 0;
    FDCAN_CCCR = 1u;
    /* Exact standard ID 0x321 -> Rx Buffer 5. */
    *filter = (0x321u << 16) | (7u << 27) | BUFFER_INDEX_STD;
    /* A second buffer acts as an ordered host-side release handshake. */
    filter[1] = (0x322u << 16) | (7u << 27) | BUFFER_INDEX_RELEASE;
    FDCAN_SIDFC = (0x100u / 4u) | (2u << 16);
    /* Exact extended ID 0x1234567 -> Rx Buffer 32. EFT is ignored. */
    filter_ext[0] = (7u << 29) | 0x1234567u;
    filter_ext[1] = BUFFER_INDEX_EXT;
    FDCAN_XIDFC = (0x120u / 4u) | (1u << 16);
    FDCAN_RXBC = 0x200u / 4u;
    FDCAN_RXESC = 0;
    FDCAN_CCCR = 0;
    /* Polling keeps NDAT set while the host injects a duplicate frame. */
    FDCAN_IE = 0;
    FDCAN_ILE = 0;
    RESULT[0] = 1u;
    for (;;) {
        volatile uint32_t *rx_std = MSGRAM + BUFFER_WORD_OFFSET_STD;
        volatile uint32_t *rx_ext = MSGRAM + BUFFER_WORD_OFFSET_EXT;

        if (RESULT[0] == 1u && (FDCAN_NDAT1 & (1u << BUFFER_INDEX_STD))) {
            if (((rx_std[0] >> 18) & 0x7ffu) != 0x321u ||
                ((rx_std[1] >> 16) & 0xfu) != 8u ||
                rx_std[2] != 0x44332211u || rx_std[3] != 0x88776655u) {
                RESULT[0] = RESULT_FAIL;
                continue;
            }
            RESULT[1] = rx_std[0];
            RESULT[2] = rx_std[1];
            RESULT[3] = FDCAN_NDAT1;
            RESULT[0] = RESULT_STD;
            /* The host sends a duplicate followed by the release frame in
             * one ordered stream.  This leaves the target NDAT asserted
             * while the duplicate is accepted by the model, without making
             * the test depend on host scheduling latency. */
        } else if (RESULT[0] == RESULT_STD &&
                   (FDCAN_NDAT1 & (1u << BUFFER_INDEX_RELEASE))) {
            if ((FDCAN_NDAT1 & (1u << BUFFER_INDEX_STD)) == 0 ||
                rx_std[2] != 0x44332211u || rx_std[3] != 0x88776655u ||
                (FDCAN_RXF0S & 0x7fu) != 0) {
                RESULT[0] = RESULT_FAIL;
                continue;
            }
            FDCAN_NDAT1 = (1u << BUFFER_INDEX_STD) |
                          (1u << BUFFER_INDEX_RELEASE);
            RESULT[0] = RESULT_STD2;
        } else if (RESULT[0] == RESULT_STD2 && (FDCAN_NDAT1 &
                                                (1u << BUFFER_INDEX_STD))) {
            if (rx_std[2] != 0xddccbbaaU || rx_std[3] != 0x1100ffeeU) {
                RESULT[0] = RESULT_FAIL;
                continue;
            }
            RESULT[0] = RESULT_STD3;
        } else if (RESULT[0] == RESULT_STD3 && (FDCAN_NDAT2 & 1u)) {
            if ((rx_ext[0] & 0x1fffffffu) != 0x1234567u ||
                !(rx_ext[0] & (1u << 30)) ||
                ((rx_ext[1] >> 16) & 0xfu) != 8u ||
                rx_ext[2] != 0x44332211u || rx_ext[3] != 0x88776655u) {
                RESULT[0] = RESULT_FAIL;
                continue;
            }
            RESULT[1] = rx_ext[0];
            RESULT[2] = rx_ext[1];
            RESULT[3] = FDCAN_NDAT2;
            RESULT[0] = RESULT_PASS;
        }
    }
}
