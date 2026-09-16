MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 0x100000
  SRAM  (rwx) : ORIGIN = 0x20000000, LENGTH = 0x20000
}

SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
  .text : { *(.text*) *(.rodata*) } > FLASH
  .data : { *(.data*) } > SRAM AT > FLASH
  .bss : { *(.bss*) *(COMMON) } > SRAM
}
