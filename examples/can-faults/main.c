/* Test-only heartbeat/challenge consumer. FDCAN setup is the existing
 * can-motor fixture's RM0468/CMSIS classic FIFO setup; no driver stack.
 * CAN fault policy lives on the host fabric, never in this firmware.
 */
#include "board.h"

#define FDCAN_CCCR REG32(0x4000a018u)
#define FDCAN_GFC REG32(0x4000a080u)
#define FDCAN_SIDFC REG32(0x4000a084u)
#define FDCAN_RXF0C REG32(0x4000a0a0u)
#define FDCAN_RXF0S REG32(0x4000a0a4u)
#define FDCAN_RXF0A REG32(0x4000a0a8u)
#define FDCAN_RXESC REG32(0x4000a0bcu)
#define FDCAN_TXBC REG32(0x4000a0c0u)
#define FDCAN_TXESC REG32(0x4000a0c8u)
#define FDCAN_TXBAR REG32(0x4000a0d0u)
#define MSGRAM ((volatile uint32_t *)0x4000ac00u)

volatile uint32_t ticks_ms;
void SysTick_Handler(void) { ++ticks_ms; }

#ifndef SIM_CAN_SKIP_RX_TEST
static void fail(void)
{
    uart_text("CAN_FAULT_FAIL\n");
    for (;;) { idle(); }
}

static void reply(uint32_t id, uint32_t nonce)
{
    /* Both words depend on the actual RX payload, not fixture time/sequence.
     * Echoing a fixed PASS or an earlier nonce cannot satisfy host acceptance.
     */
    MSGRAM[0] = (id + 0x100u) << 18;
    MSGRAM[1] = 8u << 16;
    MSGRAM[2] = nonce ^ 0x6d2b79f5u;
    MSGRAM[3] = ((nonce << 7) | (nonce >> 25)) + id;
    FDCAN_TXBAR = 1u;
}
#endif

void firmware_main(void)
{
    board_init();
    RCC_APB1HENR |= 1u << 8;
    GPIOC_MODER = (GPIOC_MODER & ~(3u << 30)) | (1u << 30);
    GPIOC_BSRR = 1u << 15; /* Actual CAN transceiver rail on. */
    FDCAN_CCCR = 3u;
    FDCAN_TXBC = 1u << 24;
    FDCAN_TXESC = 0;
    FDCAN_RXF0C = 128u | (8u << 16);
    FDCAN_RXESC = 0;
    FDCAN_SIDFC = 400u | (2u << 16);
    MSGRAM[100] = (2u << 30) | (1u << 27) | (0x321u << 16) | 0x7ffu;
    MSGRAM[101] = (2u << 30) | (1u << 27) | (0x322u << 16) | 0x7ffu;
    FDCAN_GFC = (2u << 4) | (2u << 2) | 3u;
    FDCAN_CCCR = 0;
    uart_text("SIM_READY can-faults\n");
#ifdef SIM_CAN_SKIP_RX_TEST
    /* Deliberately apparent success, without acknowledging/reading CAN RX.
     * The ordinary verifier must reject the absent nonce-derived CAN replies.
     */
    while (ticks_ms < 10u) { idle(); }
    uart_text("HB_ACTIVE\n");
    while (ticks_ms < 35u) { idle(); }
    GPIOB_BSRR = 1u;
    uart_text("HB_TIMEOUT\n");
    while (ticks_ms < 55u) { idle(); }
    GPIOB_BSRR = 1u << 16;
    uart_text("HB_RECOVERED\n");
    while (ticks_ms < 110u) { idle(); }
    uart_text("CAN_FAULT_PASS\n");
#else
    uint32_t last_heartbeat = 0;
    unsigned active = 0, timed_out = 0, recovered = 0, diagnostics = 0, passed = 0;
    for (;;) {
        while (FDCAN_RXF0S & 0x7fu) {
            const uint32_t index = (FDCAN_RXF0S >> 8) & 0x3fu;
            volatile uint32_t *rx = MSGRAM + 32u + index * 4u;
            const uint32_t id = rx[0] >> 18;
            const uint32_t size = rx[1];
            const uint32_t nonce = rx[2], check = rx[3];
            if ((id != 0x321u && id != 0x322u) ||
                (size & 0x003f0000u) != (8u << 16) ||
                check != (nonce ^ 0xa55a96c3u)) { fail(); }
            FDCAN_RXF0A = index;
            reply(id, nonce);
            if (id == 0x321u) {
                last_heartbeat = ticks_ms;
                if (!active) {
                    active = 1;
                    uart_text("HB_ACTIVE\n");
                } else if (timed_out && !recovered) {
                    recovered = 1;
                    GPIOB_BSRR = 1u << 16;
                    uart_text("HB_RECOVERED\n");
                }
            } else {
                ++diagnostics;
            }
        }
        /* 5ms heartbeat, fixed 14ms consumer watchdog. During the 7ms delay
         * transition the largest inter-arrival gap is 12ms, so only the
         * directional drop should trigger this timeout.
         */
        if (active && !timed_out && ticks_ms - last_heartbeat >= 14u) {
            timed_out = 1;
            GPIOB_BSRR = 1u;
            uart_text("HB_TIMEOUT\n");
        }
        if (!passed && ticks_ms >= 108u && recovered && diagnostics >= 10u) {
            passed = 1;
            uart_text("CAN_FAULT_PASS\n");
        }
        if (ticks_ms >= 118u && !passed) { fail(); }
        idle();
    }
#endif
    for (;;) { idle(); }
}
