#include <stdint.h>

#define GPIOC_MODER       (*(volatile uint32_t *)0x58020800u)
#define GPIOC_BSRR        (*(volatile uint32_t *)0x58020818u)
#define SPI_CR1           (*(volatile uint32_t *)0x40003800u)
#define SPI_SR            (*(volatile uint32_t *)0x40003814u)
#define SPI_TXDR          (*(volatile uint32_t *)0x40003820u)
#define SPI_RXDR          (*(volatile uint32_t *)0x40003830u)

#define DMA1_LISR         (*(volatile uint32_t *)0x40020000u)
#define DMA1_LIFCR        (*(volatile uint32_t *)0x40020008u)
#define DMA1_S3CR         (*(volatile uint32_t *)0x40020058u)
#define DMA1_S3NDTR       (*(volatile uint32_t *)0x4002005cu)
#define DMA1_S3PAR        (*(volatile uint32_t *)0x40020060u)
#define DMA1_S3M0AR       (*(volatile uint32_t *)0x40020064u)
#define DMAMUX1_C3CR      (*(volatile uint32_t *)0x4002080cu)

#define DMA_CR_EN         (1u << 0)
#define DMA_CR_MINC       (1u << 10)
#define DMA_FLAG_TEIF_S3  (1u << (22 + 3))
#define DMA_FLAG_ALL_S3   (0x3du << 22)
#define SPI_SR_RXP        (1u << 0)
#define SPI_SR_TXP        (1u << 1)
#define RESULT            ((volatile uint32_t *)0x20000000u)
#define RX_BUFFER         ((volatile uint8_t *)0x20000100u)
#define DMA_INVALID_DEST  0x60000000u

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static void spi_write_byte(uint8_t value)
{
    while (!(SPI_SR & SPI_SR_TXP)) {
    }
    SPI_TXDR = value;
}

static uint8_t spi_read_byte(void)
{
    while (!(SPI_SR & SPI_SR_RXP)) {
    }
    return (uint8_t)SPI_RXDR;
}

void Reset_Handler(void)
{
    unsigned timeout = 1000000;

    RESULT[0] = 0x53525231u; /* "SRR1" */
    RX_BUFFER[0] = 0xa5u;

    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    /* Select the BMI088 gyro on PC3 and leave the accelerometer deselected. */
    GPIOC_BSRR = (1u << 0) | (1u << 3);
    GPIOC_BSRR = (1u << 19);
    SPI_CR1 = 1;

    DMAMUX1_C3CR = 39; /* SPI2_RX */
    DMA1_S3NDTR = 1;
    DMA1_S3PAR = 0x40003830u;
    DMA1_S3M0AR = DMA_INVALID_DEST;
    DMA1_S3CR = DMA_CR_EN;

    /* Establish the BMI088 read command while RX DMA has no pending target.
     * The command response is discarded by the CPU; the following data byte
     * is the source beat under test. */
    spi_write_byte(0x80u);
    (void)spi_read_byte();
    spi_write_byte(0x00u);
    while (timeout-- && !(DMA1_LISR & DMA_FLAG_TEIF_S3)) {
    }

    RESULT[1] = SPI_SR;
    RESULT[2] = DMA1_S3NDTR;
    RESULT[3] = DMA1_S3CR;
    RESULT[4] = DMA1_LISR;
    RESULT[5] = timeout != 0;

    /* Clear only the old stream status and enable the same stream at valid
     * RAM.  The SPI RX stream-enabled hook must submit the still-pending
     * RXDR byte without requiring another SPI clock. */
    DMA1_LIFCR = DMA_FLAG_ALL_S3;
    DMA1_S3M0AR = (uint32_t)(uintptr_t)RX_BUFFER;
    DMA1_S3NDTR = 1;
    DMA1_S3CR = DMA_CR_EN | DMA_CR_MINC;
    timeout = 1000000;
    while (timeout-- && DMA1_S3NDTR) {
    }

    RESULT[6] = RX_BUFFER[0];
    RESULT[7] = DMA1_S3NDTR;
    RESULT[8] = SPI_SR;
    RESULT[9] = DMA1_LISR;
    RESULT[10] = timeout != 0;
    RESULT[0] = 0x444f4e45u; /* "DONE" */
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
