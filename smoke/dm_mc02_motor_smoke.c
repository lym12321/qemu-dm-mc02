#include <stdint.h>

#define FDCAN1_TXBC  (*(volatile uint32_t *)0x4000a0c0u)
#define FDCAN1_TXESC (*(volatile uint32_t *)0x4000a0c8u)
#define FDCAN1_TXBAR (*(volatile uint32_t *)0x4000a0d0u)
#define FDCAN1_TXBTO (*(volatile uint32_t *)0x4000a0d8u)
#define FDCAN1_RXF0C (*(volatile uint32_t *)0x4000a0a0u)
#define FDCAN1_RXF0S (*(volatile uint32_t *)0x4000a0a4u)
#define FDCAN1_RXF0A (*(volatile uint32_t *)0x4000a0a8u)
#define FDCAN1_RXESC (*(volatile uint32_t *)0x4000a0bcu)
#define FDCAN1_CCCR  (*(volatile uint32_t *)0x4000a018u)
#define FDCAN1_IR    (*(volatile uint32_t *)0x4000a050u)
#define FDCAN1_IE    (*(volatile uint32_t *)0x4000a054u)
#define FDCAN1_ILE   (*(volatile uint32_t *)0x4000a05cu)
#define MSGRAM       ((volatile uint32_t *)0x4000ac00u)
#define RESULT       ((volatile uint32_t *)0x20000000u)
#define NVIC_ISER0   (*(volatile uint32_t *)0xe000e100u)

#define RESULT_PASS 0x444d4d31u /* DMM1 */
#define RESULT_FAIL 0x444d4d46u /* DMMF */

void Reset_Handler(void);
void FDCAN1_IT0_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[36] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
    [16 + 19] = (uint32_t)(uintptr_t)FDCAN1_IT0_IRQHandler,
};

void FDCAN1_IT0_IRQHandler(void)
{
    volatile uint32_t *rx = MSGRAM + 32;

    if ((FDCAN1_RXF0S & 0x7fu) != 0 &&
        ((rx[0] >> 18) & 0x7ffu) == 0x11u &&
        ((rx[1] >> 16) & 0xfu) == 8u) {
        RESULT[0] = RESULT_PASS;
        RESULT[1] = rx[0];
        RESULT[2] = rx[1];
        RESULT[3] = rx[2];
        RESULT[4] = rx[3];
    }
    FDCAN1_IR = 1u;
}

static void send_frame(uint32_t can_id, const uint8_t data[8])
{
    volatile uint8_t *tx = (volatile uint8_t *)MSGRAM;

    MSGRAM[0] = (can_id & 0x7ffu) << 18;
    MSGRAM[1] = 8u << 16;
    for (unsigned i = 0; i < 8; ++i) {
        tx[8 + i] = data[i];
    }
    FDCAN1_TXBAR = 1u;
    for (volatile unsigned wait = 0; wait < 100000u; ++wait) {
        if (FDCAN1_TXBTO & 1u) {
            return;
        }
    }
    RESULT[0] = RESULT_FAIL;
    for (;;) {
    }
}

void Reset_Handler(void)
{
    static const uint8_t reset_command[8] = {
        0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xfb,
    };
    static const uint8_t enable_command[8] = {
        0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xfc,
    };
    /* MIT command: zero position/speed/Kp/Kd and a small positive torque. */
    static const uint8_t control_command[8] = {
        0x80, 0x00, 0x80, 0x00, 0x00, 0x00, 0x08, 0x00,
    };
    volatile uint32_t *rx = MSGRAM + 32;

    RESULT[0] = 0;
    FDCAN1_CCCR = 1u;
    FDCAN1_TXBC = 3u << 16;
    FDCAN1_TXESC = 0;
    FDCAN1_RXF0C = 32u | (8u << 16);
    FDCAN1_RXESC = 0;
    FDCAN1_IE = 1u;
    FDCAN1_ILE = 1u;
    FDCAN1_CCCR = 0u;
    NVIC_ISER0 = 1u << 19;
    RESULT[0] = 1u;

    send_frame(1u, reset_command);
    RESULT[0] = 2u;
    send_frame(1u, enable_command);
    RESULT[0] = 3u;
    send_frame(1u, control_command);
    RESULT[0] = 4u;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
