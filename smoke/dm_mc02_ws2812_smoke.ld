ENTRY(Reset_Handler)
MEMORY
{
    FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 1M
    D2 (rwx) : ORIGIN = 0x30000000, LENGTH = 32K
}
SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text : { *(.text*) *(.rodata*) } > FLASH
    .ws2812 (NOLOAD) : ALIGN(4) { *(.ws2812) } > D2
}
