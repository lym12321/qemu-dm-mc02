ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1M
    DTCM  (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

SECTIONS
{
    .isr_vector : { . = ALIGN(4); KEEP(*(.isr_vector)) . = ALIGN(4); } > FLASH
    .text : { . = ALIGN(4); *(.text*) *(.rodata*) . = ALIGN(4); } > FLASH
    .data : { . = ALIGN(4); *(.data*) . = ALIGN(4); } > DTCM AT > FLASH
    .bss : { . = ALIGN(4); *(.bss*) *(COMMON) . = ALIGN(4); } > DTCM
    .dma_m0 ORIGIN(DTCM) + 0x100 :
    { . = ALIGN(4); KEEP(*(.dma_m0)) . = ALIGN(4); } > DTCM
    .dma_m1 : { . = ALIGN(4); KEEP(*(.dma_m1)) . = ALIGN(4); } > DTCM
    .dma_replacement :
    { . = ALIGN(4); KEEP(*(.dma_replacement)) . = ALIGN(4); } > DTCM
}
