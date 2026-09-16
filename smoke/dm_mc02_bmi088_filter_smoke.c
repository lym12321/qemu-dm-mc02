#include <stdint.h>

#define GPIOC_MODER (*(volatile uint32_t *)0x58020800u)
#define GPIOC_BSRR (*(volatile uint32_t *)0x58020818u)
#define SPI_CR1    (*(volatile uint32_t *)0x40003800u)
#define SPI_CR2    (*(volatile uint32_t *)0x40003804u)
#define SPI_SR     (*(volatile uint32_t *)0x40003814u)
#define SPI_IFCR   (*(volatile uint32_t *)0x40003818u)
#define SPI_TXDR   (*(volatile uint32_t *)0x40003820u)
#define SPI_RXDR   (*(volatile uint32_t *)0x40003830u)
#define SPI_CR1_SPE    (1u << 0)
#define SPI_CR1_CSTART (1u << 9)
#define SPI_SR_EOT     (1u << 3)
#define SPI_IFCR_EOTC  (1u << 3)

#define RESULT ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static uint8_t spi_byte(uint8_t value)
{
    SPI_CR2 = 1;
    SPI_CR1 = SPI_CR1_SPE;
    SPI_CR1 = SPI_CR1_SPE | SPI_CR1_CSTART;
    while ((SPI_SR & (1u << 1)) == 0) {
    }
    SPI_TXDR = value;
    while ((SPI_SR & (1u << 0)) == 0) {
    }
    uint8_t result = (uint8_t)SPI_RXDR;
    while ((SPI_SR & SPI_SR_EOT) == 0) {
    }
    SPI_IFCR = SPI_IFCR_EOTC;
    SPI_CR1 = 0;
    return result;
}

static void gyro_select(void)
{
    GPIOC_BSRR = (1u << 19); /* PC3 low, gyro CS active. */
}

static void gyro_deselect(void)
{
    GPIOC_BSRR = (1u << 3);
}

static void gyro_write(uint8_t reg, uint8_t value)
{
    gyro_select();
    (void)spi_byte(reg & 0x7fu);
    (void)spi_byte(value);
    gyro_deselect();
}

static int16_t gyro_read_x(void)
{
    uint8_t low;
    uint8_t high;

    gyro_select();
    (void)spi_byte(0x82u);
    low = spi_byte(0);
    high = spi_byte(0);
    gyro_deselect();
    return (int16_t)((uint16_t)low | ((uint16_t)high << 8));
}

static void configure_gyro(uint8_t bandwidth)
{
    gyro_write(0x14, 0xb6); /* soft reset */
    gyro_write(0x0f, 0x00); /* +/-2000 dps */
    gyro_write(0x10, bandwidth);
}

void Reset_Handler(void)
{
    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    RESULT[0] = 0x46494c54u; /* FILT */
    SPI_CR1 = 0;

    configure_gyro(0x82); /* 1000 Hz ODR, 116 Hz bandwidth */
    RESULT[1] = 1;
    for (unsigned i = 0; i < 8; ++i) {
        for (volatile unsigned delay = 0; delay < 5000000u; ++delay) {
            __asm__ volatile ("nop");
        }
        RESULT[4 + i] = (uint16_t)gyro_read_x();
        RESULT[3] = i + 1;
    }

    configure_gyro(0x84); /* 200 Hz ODR, 23 Hz bandwidth */
    RESULT[1] = 2;
    for (unsigned i = 0; i < 8; ++i) {
        for (volatile unsigned delay = 0; delay < 5000000u; ++delay) {
            __asm__ volatile ("nop");
        }
        RESULT[12 + i] = (uint16_t)gyro_read_x();
        RESULT[3] = 9 + i;
    }

    RESULT[1] = 3;
    for (;;) {
        __asm__ volatile ("wfi");
    }
}
