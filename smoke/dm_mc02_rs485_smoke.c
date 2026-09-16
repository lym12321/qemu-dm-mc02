#include <stdint.h>

#define USART2_BASE 0x40004400u
#define USART2_CR1 (*(volatile uint32_t *)(USART2_BASE + 0x00u))
#define USART2_CR3 (*(volatile uint32_t *)(USART2_BASE + 0x08u))
#define USART2_ISR (*(volatile uint32_t *)(USART2_BASE + 0x1cu))
#define USART2_RDR (*(volatile uint32_t *)(USART2_BASE + 0x24u))
#define USART2_TDR (*(volatile uint32_t *)(USART2_BASE + 0x28u))

#define GPIOD_BASE 0x58020c00u
#define GPIOD_MODER (*(volatile uint32_t *)(GPIOD_BASE + 0x00u))
#define GPIOD_ODR (*(volatile uint32_t *)(GPIOD_BASE + 0x14u))
#define GPIOD_AFR0 (*(volatile uint32_t *)(GPIOD_BASE + 0x20u))

#define RESULT ((volatile uint32_t *)0x20000000u)
#define USART_CR1_RE (1u << 2)
#define USART_CR1_TE (1u << 3)
#define USART_CR3_DEM (1u << 14)
#define USART_CR3_DEP (1u << 15)
#define USART_ISR_RXNE (1u << 5)
#define RS485_DE_PIN 4u
#define RS485_DE_MODER_MASK (3u << (RS485_DE_PIN * 2u))
#define RS485_DE_OUTPUT (1u << (RS485_DE_PIN * 2u))
#define RS485_DE_AF (2u << (RS485_DE_PIN * 2u))
#define RS485_DE_AFR_MASK (0xfu << (RS485_DE_PIN * 4u))
#define RS485_DE_AFR7 (7u << (RS485_DE_PIN * 4u))

#define MARKER 0x52343831u /* R481 */
#define LOW_READY 0x4c4f5731u /* LOW1 */
#define HIGH_READY 0x48494731u /* HIG1 */
#define FINAL_READY 0x46494e31u /* FIN1 */
#define AUTO_READY 0x41555431u /* AUT1 */
#define AUTO_TX_READY 0x41545831u /* ATX1 */

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static void wait_for_host(uint8_t expected)
{
    while (!(USART2_ISR & USART_ISR_RXNE)) {
    }
    if ((uint8_t)USART2_RDR != expected) {
        RESULT[8] = 0x42414452u; /* BADR */
        RESULT[9] = expected;
        RESULT[10] = USART2_RDR;
        for (;;) {
            __asm__ volatile ("wfi" ::: "memory");
        }
    }
}

void Reset_Handler(void)
{
    RESULT[0] = MARKER;

    /* PD4 is USART2_DE in the board netlist; configure it as a GPIO output. */
    GPIOD_MODER = (GPIOD_MODER & ~RS485_DE_MODER_MASK) | RS485_DE_OUTPUT;
    GPIOD_ODR &= ~(1u << RS485_DE_PIN);

    /* RS485 driver enable is active high: DEM=1, DEP=0. */
    USART2_CR1 = USART_CR1_RE | USART_CR1_TE;
    USART2_CR3 = USART_CR3_DEM;
    RESULT[1] = USART2_CR3;
    RESULT[2] = GPIOD_MODER;
    RESULT[3] = GPIOD_ODR;

    /* DE low: this byte must not reach the USART2 chardev. */
    USART2_TDR = 'L';
    RESULT[4] = LOW_READY;
    wait_for_host('1');

    /* DE high: this byte must reach the USART2 chardev. */
    GPIOD_ODR |= 1u << RS485_DE_PIN;
    RESULT[5] = GPIOD_ODR;
    USART2_TDR = 'H';
    RESULT[6] = HIGH_READY;
    wait_for_host('2');

    /* DE low again: a second low-gated byte must also be suppressed. */
    GPIOD_ODR &= ~(1u << RS485_DE_PIN);
    RESULT[8] = GPIOD_ODR;
    USART2_TDR = 'l';
    RESULT[7] = FINAL_READY;
    wait_for_host('3');

    /* DEP reverses the automatic DE polarity.  The board transceiver is
     * active-high, so this byte must be suppressed while DEM|DEP is set. */
    USART2_CR3 = USART_CR3_DEM | USART_CR3_DEP;
    USART2_TDR = 'N';
    USART2_CR3 = USART_CR3_DEM;

    /* AF stage: PD4 is USART2_DE AF7.  ODR remains low and must not gate the
     * USART's automatic driver-enable waveform. */
    GPIOD_MODER = (GPIOD_MODER & ~RS485_DE_MODER_MASK) | RS485_DE_AF;
    GPIOD_AFR0 = (GPIOD_AFR0 & ~RS485_DE_AFR_MASK) | RS485_DE_AFR7;
    RESULT[11] = GPIOD_MODER;
    RESULT[12] = GPIOD_AFR0;
    RESULT[13] = GPIOD_ODR;
    RESULT[9] = AUTO_READY;
    USART2_TDR = 'A';
    RESULT[10] = AUTO_TX_READY;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
