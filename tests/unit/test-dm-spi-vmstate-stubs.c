/* Runtime-sync stub for the isolated SPI VMState contract test. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_spi.h"

#ifdef _WIN32
/* The Windows ftruncate wrapper lives in the block driver, which this
 * isolated test does not link. Keep the fixture's real file truncation. */
int qemu_ftruncate64(int fd, int64_t length)
{
    int error = _chsize_s(fd, length);

    if (error) {
        errno = error;
        return -1;
    }
    return 0;
}
#endif

unsigned dm_mc02_spi_sync_calls;

void dm_mc02_spi_sync_runtime(DmMc02Spi *state)
{
    dm_mc02_spi_sync_calls++;
    /* This is the execution-local recursion guard that the real runtime
     * synchronizer clears before rescheduling its optional timer. */
    state->dma_request_active = false;
}
