#include <stdint.h>

#define GPIO_BASE(n) (0x58020000u + (n) * 0x400u)
#define GPIO_MODER(n) (*(volatile uint32_t *)(GPIO_BASE(n) + 0x00u))
#define GPIO_IDR(n)  (*(volatile uint32_t *)(GPIO_BASE(n) + 0x10u))
#define GPIO_ODR(n)  (*(volatile uint32_t *)(GPIO_BASE(n) + 0x14u))
#define GPIO_BSRR(n) (*(volatile uint32_t *)(GPIO_BASE(n) + 0x18u))
#define GPIO_AFR0(n) (*(volatile uint32_t *)(GPIO_BASE(n) + 0x20u))
#define GPIO_AFR1(n) (*(volatile uint32_t *)(GPIO_BASE(n) + 0x24u))

#define GPIOC_BSRR GPIO_BSRR(2)
#define SPI_CR1    (*(volatile uint32_t *)0x40003800u)
#define SPI_SR     (*(volatile uint32_t *)0x40003814u)
#define SPI_TXDR   (*(volatile uint32_t *)0x40003820u)
#define SPI_RXDR   (*(volatile uint32_t *)0x40003830u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static uint8_t spi_byte(uint8_t value)
{
    while ((SPI_SR & (1u << 1)) == 0) {
    }
    SPI_TXDR = value;
    while ((SPI_SR & (1u << 0)) == 0) {
    }
    return (uint8_t)SPI_RXDR;
}

static uint8_t bmi_id(uint32_t select)
{
    GPIOC_BSRR = select == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(0x80u);
    if (select == 1) {
        (void)spi_byte(0);
    }
    uint8_t value = spi_byte(0);
    GPIOC_BSRR = select;
    return value;
}

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;
    static const unsigned banks[] = { 0, 1, 2, 3, 4 };
    static const uint32_t set_bits[] = { 1, 2, 4, 8, 16 };

    result[0] = 0x47495031u; /* "GIP1" */

    /* Make A pin 0 an output only; PA15 is the board's pulled-up user key. */
    GPIO_MODER(0) = 1u;
    GPIO_AFR0(0) = 0x12345678u;
    GPIO_AFR1(0) = 0x9abcdef0u;
    for (unsigned i = 1; i < 5; ++i) {
        GPIO_MODER(banks[i]) = 0x55555555u;
    }

    /* Each real bank A-E gets an independent BSRR set and an ODR/IDR read. */
    for (unsigned i = 0; i < 5; ++i) {
        GPIO_BSRR(banks[i]) = set_bits[i];
        result[1 + i * 2] = GPIO_ODR(banks[i]);
        result[2 + i * 2] = GPIO_IDR(banks[i]);
    }

    /* BSRR reset is checked independently; C starts with both CS pins high. */
    for (unsigned i = 0; i < 5; ++i) {
        GPIO_BSRR(banks[i]) = set_bits[i] << 16;
        result[11 + i] = GPIO_ODR(banks[i]);
    }
    result[16] = GPIO_AFR0(0);
    result[17] = GPIO_AFR1(0);
    result[18] = GPIO_MODER(0);

    /* Restore both active-low CS lines high, then select each BMI088 die. */
    GPIOC_BSRR = (1u << 0) | (1u << 3);
    SPI_CR1 = 1;
    result[19] = bmi_id(1); /* PC0: accelerometer */
    result[20] = bmi_id(8); /* PC3: gyroscope */
    result[21] = GPIO_ODR(2);
    result[22] = GPIO_IDR(2);

    /* Sub-word accesses must update only their register lane. */
    *(volatile uint8_t *)(GPIO_BASE(0) + 0x14u) = 0x03u;
    *(volatile uint8_t *)(GPIO_BASE(0) + 0x15u) = 0x02u;
    result[23] = GPIO_ODR(0);
    result[24] = GPIO_IDR(0);
    *(volatile uint16_t *)(GPIO_BASE(0) + 0x1au) = 0x0001u;
    result[25] = GPIO_ODR(0);
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
