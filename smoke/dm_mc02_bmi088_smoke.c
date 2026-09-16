#include <stdint.h>

#define GPIOC_MODER (*(volatile uint32_t *)0x58020800u)
#define GPIOC_BSRR (*(volatile uint32_t *)0x58020818u)
#define SPI_CR1    (*(volatile uint32_t *)0x40003800u)
#define SPI_CR2    (*(volatile uint32_t *)0x40003804u)
#define SPI_SR     (*(volatile uint32_t *)0x40003814u)
#define SPI_IFCR   (*(volatile uint32_t *)0x40003818u)
#define SPI_TXDR   (*(volatile uint32_t *)0x40003820u)
#define SPI_TXDR16 (*(volatile uint16_t *)0x40003820u)
#define SPI_RXDR   (*(volatile uint32_t *)0x40003830u)
#define SPI_CR1_SPE    (1u << 0)
#define SPI_CR1_CSTART (1u << 9)
#define SPI_SR_EOT     (1u << 3)
#define SPI_IFCR_EOTC  (1u << 3)

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

static uint8_t bmi_read(uint32_t deselect, uint8_t reg, int accel)
{
    uint8_t value;

    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(0x80u | reg);
    if (accel) {
        (void)spi_byte(0);
    }
    value = spi_byte(0);
    GPIOC_BSRR = deselect;
    return value;
}

static void bmi_write(uint32_t deselect, uint8_t reg, uint8_t value)
{
    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(reg);
    (void)spi_byte(value);
    GPIOC_BSRR = deselect;
}

static void bmi_write_packed(uint32_t deselect, uint8_t reg, uint8_t value)
{
    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    SPI_CR2 = 2;
    SPI_CR1 = SPI_CR1_SPE;
    SPI_CR1 = SPI_CR1_SPE | SPI_CR1_CSTART;
    while ((SPI_SR & (1u << 1)) == 0) {
    }
    SPI_TXDR16 = (uint16_t)reg | ((uint16_t)value << 8);
    while ((SPI_SR & SPI_SR_EOT) == 0) {
    }
    SPI_IFCR = SPI_IFCR_EOTC;
    SPI_CR1 = 0;
    GPIOC_BSRR = deselect;
}

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;

    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    SPI_CR1 = 0;
    result[0] = 0x424d4932u;
    result[1] = bmi_read(1, 0x00, 1);
    result[2] = bmi_read(8, 0x00, 0);
    result[3] = bmi_read(1, 0x16, 1);
    result[4] = bmi_read(1, 0x17, 1);
    result[5] = bmi_read(8, 0x06, 0);
    /* Exercise the exact two-die soft-reset sequence used by bsp_imu_init(). */
    bmi_write_packed(8, 0x10, 0x85);
    bmi_write(8, 0x14, 0xb6);
    result[6] = bmi_read(8, 0x10, 0);
    bmi_write(1, 0x40, 0x8b);
    bmi_write(1, 0x7e, 0xb6);
    result[7] = bmi_read(1, 0x40, 1);
    /* Default 25 C encodes to raw 16: TEMP_M=2, TEMP_L=0. */
    result[8] = bmi_read(1, 0x22, 1);
    result[9] = bmi_read(1, 0x23, 1);
    for (;;) {
        __asm__ volatile ("wfi");
    }
}
