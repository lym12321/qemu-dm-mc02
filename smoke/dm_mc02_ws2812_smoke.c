#include <stdint.h>

#define DMA2_S6CR   (*(volatile uint32_t *)0x400204a0u)
#define DMA2_S6NDTR (*(volatile uint32_t *)0x400204a4u)
#define DMA2_S6PAR  (*(volatile uint32_t *)0x400204a8u)
#define DMA2_S6M0AR (*(volatile uint32_t *)0x400204acu)

#define DMA_CR_EN       (1u << 0)
#define DMA_CR_DIR_P2M (1u << 6)
#define DMA_CR_MINC     (1u << 10)
#define DMA_CR_PSIZE_16 (1u << 11)
#define DMA_CR_MSIZE_16 (1u << 13)
#define HIGH 168u
#define LOW 84u

__attribute__((section(".ws2812"))) volatile uint16_t waveform[72];
void Reset_Handler(void);
__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = { 0x30008000u, (uint32_t)(uintptr_t)Reset_Handler };

static void encode_byte(unsigned offset, uint8_t value)
{
    for (unsigned bit = 0; bit < 8; ++bit)
        waveform[offset + bit] = (value & (1u << (7u - bit))) ? HIGH : LOW;
}

static void encode_pixel(uint8_t red, uint8_t green, uint8_t blue)
{
    encode_byte(0, green);
    encode_byte(8, red);
    encode_byte(16, blue);
}

static void configure_dma(void)
{
    DMA2_S6PAR = 0x40010400u;
    DMA2_S6M0AR = (uint32_t)(uintptr_t)waveform;
    DMA2_S6NDTR = 72;
    DMA2_S6CR = DMA_CR_EN | DMA_CR_DIR_P2M | DMA_CR_MINC |
                DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;
}

void Reset_Handler(void)
{
    encode_pixel(0x12, 0xa4, 0x3c);
    for (unsigned i = 24; i < 72; ++i)
        waveform[i] = LOW;
    configure_dma();
    for (volatile unsigned delay = 0; delay < 100000000u; ++delay)
        __asm__ volatile ("nop");
    encode_pixel(0xe7, 0x05, 0x91);
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
