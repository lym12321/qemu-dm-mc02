/* Runtime-sync stub for the isolated UART VMState contract test. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_uart.h"

unsigned dm_mc02_uart_sync_calls;

void dm_mc02_uart_sync_runtime(DmMc02Uart *state)
{
    (void)state;
    dm_mc02_uart_sync_calls++;
}
