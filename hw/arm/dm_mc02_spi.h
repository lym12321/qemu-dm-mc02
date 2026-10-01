/* Reusable STM32H7 SPI data path with board-independent targets. */
#ifndef HW_ARM_DM_MC02_SPI_H
#define HW_ARM_DM_MC02_SPI_H

#include "exec/memory.h"
#include "hw/arm/dm_mc02_dma.h"
#include "hw/arm/dm_mc02_spi_target.h"
#include "migration/vmstate.h"
#include "qemu/timer.h"

#define DM_MC02_SPI_MAX_TARGETS 4

typedef struct DmMc02SpiDmaChannel {
    DmMc02Dma *dma;
    const DmMc02Dmamux *dmamux;
    unsigned stream;
    uint32_t request_id;
    hwaddr peripheral_addr;
} DmMc02SpiDmaChannel;

typedef struct DmMc02Spi {
    MemoryRegion iomem;
    DmMc02SpiTarget targets[DM_MC02_SPI_MAX_TARGETS];
    uint32_t selected_mask;
    uint32_t cr1;
    uint32_t cr2;
    uint32_t cfg1;
    uint32_t cfg2;
    uint32_t transfer_remaining;
    uint8_t rx;
    bool rx_valid;
    /* Synchronous direct-P2M DMA reservation.  RXDR remains observable
     * until the destination memory write commits. */
    bool rx_dma_reserved;
    uint8_t rx_dma_reserved_value;
    bool eot;
    /* Absolute virtual deadline for the deferred TX-DMA continuation.
     * Zero means that no SPI-owned TX continuation is armed. */
    uint64_t dma_tx_next_ns;
    DmMc02SpiDmaChannel dma_tx;
    DmMc02SpiDmaChannel dma_rx;
    QEMUTimer *dma_tx_timer;
    bool dma_request_active;
    bool dma_endpoint_enabled;
    unsigned dma_batch_limit;
    DmMc02DmaEndpoint tx_endpoint;
    DmMc02DmaEndpoint rx_endpoint;
} DmMc02Spi;

void dm_mc02_spi_state_init(DmMc02Spi *spi);
void dm_mc02_spi_init(DmMc02Spi *spi, Object *owner, const char *name,
                      bool dma_timer);
void dm_mc02_spi_cleanup(DmMc02Spi *spi);
void dm_mc02_spi_reset(DmMc02Spi *spi);

void dm_mc02_spi_set_dma_channels(DmMc02Spi *spi,
                                  const DmMc02SpiDmaChannel *tx,
                                  const DmMc02SpiDmaChannel *rx);
void dm_mc02_spi_set_target(DmMc02Spi *spi, unsigned index,
                            const DmMc02SpiTarget *target);
void dm_mc02_spi_select_mask(DmMc02Spi *spi, uint32_t selected_mask);
void dm_mc02_spi_set_dma_endpoint(DmMc02Spi *spi, bool enabled);

/* Retry a pending RXDR result after the board observes the configured RX DMA
 * stream becoming enabled.  This is a one-beat data-path hook; the DMA stream
 * still owns arbitration, addresses and status. */
void dm_mc02_spi_dma_rx_stream_enabled(DmMc02Spi *spi);

/* Restore a GPIO-derived selection snapshot without invoking target select
 * callbacks.  Board composition must use this only after all target state
 * and framer state has been restored. */
void dm_mc02_spi_restore_selected_mask(DmMc02Spi *spi,
                                       uint32_t selected_mask);

/* Rebuild the optional TX-DMA timer after component state restore.  Target,
 * DMA and GPIO-selection wiring remain caller-owned runtime state. */
void dm_mc02_spi_sync_runtime(DmMc02Spi *spi);

/* Component-only state contract; machine-level migration is not registered. */
const VMStateDescription *dm_mc02_spi_vmstate(void);

/* Raw component description for an enclosing composition.  It has the same
 * data fields as dm_mc02_spi_vmstate(), but deliberately has no post-load;
 * the enclosing composition activates runtime continuations after its other
 * components and selection snapshot are valid. */
const VMStateDescription *dm_mc02_spi_vmstate_raw(void);
extern const VMStateDescription vmstate_dm_mc02_spi_raw;

#endif
