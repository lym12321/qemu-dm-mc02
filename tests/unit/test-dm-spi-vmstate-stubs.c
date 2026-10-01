/* Runtime-sync stub for the isolated SPI VMState contract test. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_spi.h"

unsigned dm_mc02_spi_sync_calls;

void dm_mc02_spi_sync_runtime(DmMc02Spi *state)
{
    dm_mc02_spi_sync_calls++;
    /* This is the execution-local recursion guard that the real runtime
     * synchronizer clears before rescheduling its optional timer. */
    state->dma_request_active = false;
}
