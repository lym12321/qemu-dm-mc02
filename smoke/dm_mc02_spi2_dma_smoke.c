#include <stdint.h>

#define GPIOC_MODER       (*(volatile uint32_t *)0x58020800u)
#define GPIOC_BSRR       (*(volatile uint32_t *)0x58020818u)
#define SPI_CR1          (*(volatile uint32_t *)0x40003800u)
#define SPI_SR           (*(volatile uint32_t *)0x40003814u)
#define SPI_TXDR         (*(volatile uint32_t *)0x40003820u)
#define SPI_RXDR         (*(volatile uint32_t *)0x40003830u)

#define DMA1_LISR        (*(volatile uint32_t *)0x40020000u)
#define DMA1_HISR        (*(volatile uint32_t *)0x40020004u)
#define DMA1_S3CR        (*(volatile uint32_t *)0x40020058u)
#define DMA1_S3NDTR      (*(volatile uint32_t *)0x4002005cu)
#define DMA1_S3PAR       (*(volatile uint32_t *)0x40020060u)
#define DMA1_S3M0AR      (*(volatile uint32_t *)0x40020064u)
#define DMA1_S4CR        (*(volatile uint32_t *)0x40020070u)
#define DMA1_S4NDTR      (*(volatile uint32_t *)0x40020074u)
#define DMA1_S4PAR       (*(volatile uint32_t *)0x40020078u)
#define DMA1_S4M0AR      (*(volatile uint32_t *)0x4002007cu)
#define DMAMUX1_C3CR     (*(volatile uint32_t *)0x4002080cu)
#define DMAMUX1_C4CR     (*(volatile uint32_t *)0x40020810u)

#define DMA_CR_EN        (1u << 0)
#define DMA_CR_DIR_P2M   0u
#define DMA_CR_DIR_M2P   (1u << 6)
#define DMA_CR_DIR_M2M   (2u << 6)
#define DMA_CR_PINC      (1u << 9)
#define DMA_CR_MINC      (1u << 10)
#define DMA_FLAG_S3_TC   (1u << 23)
#define DMA_FLAG_S4_TC   (1u << 21)
#define RESULT           ((volatile uint32_t *)0x20000000u)

static uint8_t spi_byte(uint8_t value)
{
    while ((SPI_SR & (1u << 1)) == 0) {
    }
    SPI_TXDR = value;
    while ((SPI_SR & (1u << 0)) == 0) {
    }
    return (uint8_t)SPI_RXDR;
}

static void gyro_write(uint8_t reg, uint8_t value)
{
    GPIOC_BSRR = (1u << 3);
    GPIOC_BSRR = (1u << 19);
    (void)spi_byte(reg);
    (void)spi_byte(value);
    GPIOC_BSRR = (1u << 3);
}

static uint8_t gyro_read(uint8_t reg)
{
    uint8_t value;

    GPIOC_BSRR = (1u << 3);
    GPIOC_BSRR = (1u << 19);
    (void)spi_byte(0x80u | reg);
    value = spi_byte(0);
    GPIOC_BSRR = (1u << 3);
    return value;
}

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    static const uint8_t tx[2] = { 0x80u, 0x00u };
    static volatile uint8_t rx[2] __attribute__((section(".dma_buffer")));
    unsigned timeout = 1000000;

    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    RESULT[0] = 0x53324432u; /* "S2D2" */
    rx[0] = 0xa5u;
    rx[1] = 0xa5u;

    /* Select the BMI088 gyro (PC3 is active low); PC0 stays deasserted. */
    GPIOC_BSRR = (1u << 0) | (1u << 3);
    GPIOC_BSRR = (1u << 19);
    SPI_CR1 = 1;

    /* DMAMUX1 channel N is paired with DMA1 Stream N on STM32H7. */
    DMAMUX1_C3CR = 39; /* SPI2_RX */
    DMAMUX1_C4CR = 40; /* SPI2_TX */

    DMA1_S3NDTR = 2;
    DMA1_S3PAR = 0x40003830u;
    DMA1_S3M0AR = (uint32_t)(uintptr_t)rx;
    DMA1_S3CR = DMA_CR_EN | DMA_CR_DIR_P2M | DMA_CR_MINC;

    DMA1_S4NDTR = 2;
    DMA1_S4PAR = 0x40003820u;
    DMA1_S4M0AR = (uint32_t)(uintptr_t)tx;
    DMA1_S4CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC;

    while (timeout-- && (DMA1_S3NDTR || DMA1_S4NDTR)) {
    }

    RESULT[1] = rx[0];
    RESULT[2] = rx[1];
    RESULT[3] = DMA1_S3NDTR;
    RESULT[4] = DMA1_S4NDTR;
    RESULT[5] = DMA1_LISR;
    RESULT[6] = DMA1_HISR;
    RESULT[7] = DMA1_S3CR | DMA1_S4CR;
    RESULT[8] = DMA1_S3M0AR - (uint32_t)(uintptr_t)rx;
    RESULT[9] = DMA1_S4M0AR - (uint32_t)(uintptr_t)tx;
    RESULT[10] = timeout != 0;

    /* Exercise two single-item SPI DMA requests with a peripheral-increment
     * stream.  The request source remains SPI2_TX, but the second item must
     * access the live PAR cursor rather than repeating TXDR. */
    gyro_write(0x10, 0xa5u);
    static const uint8_t pinc_tx[2] = { 0x10u, 0x55u };
    GPIOC_BSRR = (1u << 19);
    DMA1_S4NDTR = 2;
    DMA1_S4PAR = 0x40003820u;
    DMA1_S4M0AR = (uint32_t)(uintptr_t)pinc_tx;
    DMA1_S4CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_PINC | DMA_CR_MINC;
    timeout = 1000000;
    while (timeout-- && DMA1_S4NDTR) {
    }
    RESULT[11] = gyro_read(0x10);
    RESULT[12] = DMA1_S4PAR - 0x40003820u;
    RESULT[13] = timeout != 0;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
