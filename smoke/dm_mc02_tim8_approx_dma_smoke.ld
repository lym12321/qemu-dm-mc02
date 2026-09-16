ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1M
    DTCM  (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text : { *(.text*) *(.rodata*) } > FLASH
    .data : { *(.data*) } > DTCM AT > FLASH
    .bss (NOLOAD) : { *(.bss*) *(COMMON) } > DTCM
    .waveform 0x20000100 : { KEEP(*(.waveform)) } > DTCM
}
