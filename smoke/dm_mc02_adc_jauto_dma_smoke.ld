ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 128K
    DTCM  (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text : { *(.text*) *(.rodata*) } > FLASH
    .data : { *(.data*) } > DTCM AT > FLASH
    .dma_buffer ORIGIN(DTCM) + 0x100 :
    {
        . = ALIGN(4);
        KEEP(*(.dma_buffer))
    } > DTCM
    .bss (NOLOAD) : { *(.bss*) *(COMMON) } > DTCM
}
