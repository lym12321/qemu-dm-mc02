/* Test-only direct consumer for the Virtual Lab channel adapter.
 * Register configuration reuses dm_mc02_uart_smoke.c, dm_mc02_fdcan_smoke.c
 * and dm_mc02_adc_irq_smoke.c. Addresses/fields are STM32H723 CMSIS / RM0468
 * USART, GPIO, ADC and FDCAN definitions. This is a digital simulator fixture,
 * not a physical calibration or a complete hardware initialization sequence.
 */
#include <stdint.h>

#define REG32(address) (*(volatile uint32_t *)(address))
#define USART1_CR1  REG32(0x40011000u)
#define USART1_BRR  REG32(0x4001100cu)
#define USART1_ISR  REG32(0x4001101cu)
#define USART1_RDR  REG32(0x40011024u)
#define USART1_TDR  REG32(0x40011028u)
#define GPIOA_MODER REG32(0x58020000u)
#define GPIOA_IDR   REG32(0x58020010u)
#define GPIOA_BSRR  REG32(0x58020018u)
#define ADC1_ISR    REG32(0x40022000u)
#define ADC1_CR     REG32(0x40022008u)
#define ADC1_CFGR   REG32(0x4002200cu)
#define ADC1_SMPR1  REG32(0x40022014u)
#define ADC1_SQR1   REG32(0x40022030u)
#define ADC1_DR     REG32(0x40022040u)
#define FDCAN1_CCCR REG32(0x4000a018u)
#define FDCAN1_GFC  REG32(0x4000a080u)
#define FDCAN1_SIDFC REG32(0x4000a084u)
#define FDCAN1_RXF0C REG32(0x4000a0a0u)
#define FDCAN1_RXF0S REG32(0x4000a0a4u)
#define FDCAN1_RXF0A REG32(0x4000a0a8u)
#define FDCAN1_RXESC REG32(0x4000a0bcu)
#define FDCAN1_TXBC REG32(0x4000a0c0u)
#define FDCAN1_TXESC REG32(0x4000a0c8u)
#define FDCAN1_TXBAR REG32(0x4000a0d0u)
#define MSGRAM ((volatile uint32_t *)0x4000ac00u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static void uart_char(uint8_t value)
{
    /* TC means the preceding byte actually left the USART, rather than only
     * being admitted to the model's bounded host queue. */
    while ((USART1_ISR & (1u << 6)) == 0) {
    }
    USART1_TDR = value;
}

static void uart_text(const char *text)
{
    while (*text) {
        uart_char((uint8_t)*text++);
    }
}

static void uart_decimal(uint32_t value)
{
    char digits[10];
    unsigned count = 0;
    do {
        digits[count++] = '0' + value % 10u;
        value /= 10u;
    } while (value);
    while (count) {
        uart_char((uint8_t)digits[--count]);
    }
    uart_char('\n');
}

void Reset_Handler(void)
{
    uint32_t last_key = 2u;
    uint32_t last_adc = 0x10000u;
    volatile uint32_t *tx = MSGRAM;
    volatile uint32_t *rx = MSGRAM + 32;

    USART1_BRR = 556u; /* Nominal 64 MHz kernel / 115200 baud, digital 8N1. */
    USART1_CR1 = 1u | (1u << 2) | (1u << 3); /* UE, RE, TE. */
    GPIOA_MODER = (GPIOA_MODER & ~((3u << 14) | (3u << 30))) |
                  (1u << 14); /* A7 output, A15 input. */
    GPIOA_BSRR = 1u << (7u + 16u);

    FDCAN1_CCCR = 3u; /* INIT + configuration-change enable. */
    FDCAN1_TXBC = 1u << 24; /* TFQS=1, no dedicated buffers, FIFO at byte 0. */
    FDCAN1_TXESC = 0;
    FDCAN1_RXF0C = 128u | (1u << 16); /* One 8-byte RX element at byte 128. */
    FDCAN1_RXESC = 0;
    FDCAN1_SIDFC = 400u | (1u << 16); /* One standard filter at byte 400. */
    MSGRAM[100] = (2u << 30) | (1u << 27) | (0x321u << 16) | 0x7ffu;
    FDCAN1_GFC = (2u << 4) | (2u << 2) | 3u; /* Reject unmatched and RTR. */
    FDCAN1_CCCR = 0;

    ADC1_SQR1 = 4u << 6; /* One regular rank, channel 4. */
    ADC1_SMPR1 = 7u << 12; /* Long sample period reduces fixture polling load. */
    ADC1_CFGR = (1u << 13) | (1u << 12); /* 16-bit, continuous, overwrite OVR. */
    ADC1_CR = 1u;
    ADC1_CR = 1u | (1u << 2);
    uart_text("LAB_READY\n");

    for (;;) {
        uint32_t key = (GPIOA_IDR >> 15) & 1u;
        if (key != last_key) {
            GPIOA_BSRR = key ? 1u << 23 : 1u << 7;
            uart_text("KEY=");
            uart_decimal(key);
            last_key = key;
        }
        if (USART1_ISR & (1u << 5)) {
            uint8_t value = USART1_RDR;
            uart_text("UART=");
            uart_char(value);
            uart_char('\n');
        }
        if (ADC1_ISR & (1u << 2)) {
            uint32_t value = ADC1_DR;
            ADC1_ISR = (1u << 3) | (1u << 4); /* W1C EOS/OVR after DR read. */
            if (value != last_adc) {
                uart_text("ADC=");
                uart_decimal(value);
                last_adc = value;
            }
        }
        if (FDCAN1_RXF0S & 0x7fu) {
            /* Reject unexpected native headers rather than echoing any input.
             * Timestamp/filter metadata in RX word 1 is independent of DLC. */
            if (rx[0] == (0x321u << 18) &&
                (rx[1] & 0x003f0000u) == (8u << 16)) {
                tx[0] = 0x322u << 18;
                tx[1] = 8u << 16;
                tx[2] = rx[2];
                tx[3] = rx[3];
                FDCAN1_TXBAR = 1u;
            } else {
                uart_text("CAN_BAD\n");
            }
            FDCAN1_RXF0A = (FDCAN1_RXF0S >> 8) & 0x3fu;
        }
    }
}
