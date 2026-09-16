#include <stdint.h>

#define USART2_CR1       (*(volatile uint32_t *)0x40004400u)
#define USART2_ISR       (*(volatile uint32_t *)0x4000441cu)
#define USART2_CR3       (*(volatile uint32_t *)0x40004408u)
#define USART2_TDR       (*(volatile uint32_t *)0x40004428u)

#define GPIOD_MODER      (*(volatile uint32_t *)0x58020c00u)
#define GPIOD_ODR       (*(volatile uint32_t *)0x58020c14u)

#define DMA1_S6CR        (*(volatile uint32_t *)0x400200a0u)
#define DMA1_S6NDTR      (*(volatile uint32_t *)0x400200a4u)
#define DMA1_S6PAR       (*(volatile uint32_t *)0x400200a8u)
#define DMA1_S6M0AR      (*(volatile uint32_t *)0x400200acu)
#define DMA2_S1CR        (*(volatile uint32_t *)0x40020428u)
#define DMA2_S1NDTR      (*(volatile uint32_t *)0x4002042cu)
#define DMA2_S1PAR       (*(volatile uint32_t *)0x40020430u)
#define DMA2_S1M0AR      (*(volatile uint32_t *)0x40020434u)
#define DMA2_S1FCR       (*(volatile uint32_t *)0x4002043cu)
#define DMAMUX1_C6CR    (*(volatile uint32_t *)0x40020818u)
#define DMAMUX1_C9CR    (*(volatile uint32_t *)0x40020824u)

#define DMA_CR_EN        (1u << 0)
#define DMA_CR_DIR_M2P   (1u << 6)
#define DMA_CR_MINC      (1u << 10)
#define DMA_CR_MBURST_INCR4 (1u << 23)
#define DMA_FCR_DMDIS    (1u << 2)
#define DMA_FCR_FTH_HALF (1u << 0)
#define USART_CR1_RE     (1u << 2)
#define USART_CR1_TE     (1u << 3)
#define USART_CR3_DMAR   (1u << 6)
#define USART_CR3_DMAT   (1u << 7)
#define USART_CR3_DEM    (1u << 14)
#define USART_ISR_RXNE   (1u << 5)
#define RESULT           ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    static const uint8_t tx[] = { 'D', '2' };
    static volatile uint8_t rx[1] __attribute__((section(".dma_buffer")));
    unsigned timeout = 1000000;

    RESULT[0] = 0x55324432u; /* "U2D2" */
    rx[0] = 0xa5u;

    /* USART2_DE is PD4.  The board's RS485 transceiver is active high. */
    GPIOD_MODER = (GPIOD_MODER & ~(3u << 8)) | (1u << 8);
    GPIOD_ODR |= 1u << 4;
    USART2_CR1 = USART_CR1_RE | USART_CR1_TE;
    USART2_CR3 = USART_CR3_DEM;
    USART2_TDR = 'P';
    while (!(USART2_ISR & USART_ISR_RXNE)) {
    }

    /* USART2 RX request 43 uses DMA1 S6/C6; TX request 44 uses DMA2 S1/C9. */
    DMAMUX1_C6CR = 43;
    DMAMUX1_C9CR = 44;
    DMA1_S6NDTR = 1;
    DMA1_S6PAR = 0x40004424u;
    DMA1_S6M0AR = (uint32_t)(uintptr_t)rx;
    DMA1_S6CR = DMA_CR_EN | DMA_CR_MINC;
    DMA2_S1NDTR = 2;
    DMA2_S1PAR = (uint32_t)(uintptr_t)&USART2_TDR;
    DMA2_S1M0AR = (uint32_t)(uintptr_t)tx;
    DMA2_S1FCR = DMA_FCR_DMDIS | DMA_FCR_FTH_HALF;
    DMA2_S1CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC |
                DMA_CR_MBURST_INCR4;
    USART2_CR3 = USART_CR3_DEM | USART_CR3_DMAR | USART_CR3_DMAT;

    while (timeout-- && DMA1_S6NDTR) {
    }
    RESULT[1] = rx[0];
    RESULT[2] = DMA1_S6NDTR;
    RESULT[3] = DMA2_S1NDTR;
    RESULT[4] = DMA2_S1M0AR - (uint32_t)(uintptr_t)tx;
    RESULT[5] = timeout != 0;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
