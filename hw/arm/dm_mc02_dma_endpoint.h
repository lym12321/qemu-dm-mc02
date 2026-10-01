/* Shared endpoint callback contract for the STM32H723 DMA model. */
#ifndef HW_ARM_DM_MC02_DMA_ENDPOINT_H
#define HW_ARM_DM_MC02_DMA_ENDPOINT_H

#include <stdbool.h>
#include <stdint.h>

typedef bool DmMc02DmaEndpointRead(void *opaque, uint8_t *data,
                                   unsigned size, uint64_t timestamp_ns);
typedef bool DmMc02DmaEndpointWrite(void *opaque, const uint8_t *data,
                                    unsigned size, uint64_t timestamp_ns);

/* Result of one endpoint beat.  RETRY is a non-fatal backpressure result:
 * the endpoint did not commit the beat and the caller may submit the same
 * request again.  ERROR is a permanent rejection for the current DMA
 * configuration and is mapped to the DMA transfer-error path. */
typedef enum DmMc02DmaEndpointResult {
    DM_MC02_DMA_ENDPOINT_ERROR = 0,
    DM_MC02_DMA_ENDPOINT_ACCEPTED,
    DM_MC02_DMA_ENDPOINT_RETRY,
} DmMc02DmaEndpointResult;

/* Extended callbacks are optional.  A legacy bool callback remains valid and
 * maps false to ERROR, preserving the existing endpoint ABI.  An extended
 * callback returning RETRY must not consume or modify the beat. */
typedef DmMc02DmaEndpointResult DmMc02DmaEndpointReadEx(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns);
typedef DmMc02DmaEndpointResult DmMc02DmaEndpointWriteEx(
    void *opaque, const uint8_t *data, unsigned size, uint64_t timestamp_ns);

/* An optional direct-P2M transaction.  prepare copies one source beat into
 * data without consuming it.  The DMA caller invokes exactly one of commit
 * or abort after an ACCEPTED prepare, according to the following guest-memory
 * write.  A complete reservation tuple is intentionally required: a partial
 * tuple is rejected instead of silently falling back to a consuming read.
 *
 * This applies only to the direct P2M endpoint path.  The existing read and
 * read_ex callbacks remain the legacy, immediately-consuming compatibility
 * path; FIFO P2M reservation is a separate multi-beat boundary. */
typedef DmMc02DmaEndpointResult DmMc02DmaEndpointReadPrepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns);
typedef void DmMc02DmaEndpointReadCommit(void *opaque);
typedef void DmMc02DmaEndpointReadAbort(void *opaque);

typedef struct DmMc02DmaEndpoint {
    DmMc02DmaEndpointRead *read;
    DmMc02DmaEndpointWrite *write;
    DmMc02DmaEndpointReadEx *read_ex;
    DmMc02DmaEndpointWriteEx *write_ex;
    DmMc02DmaEndpointReadPrepare *read_prepare;
    DmMc02DmaEndpointReadCommit *read_commit;
    DmMc02DmaEndpointReadAbort *read_abort;
    void *opaque;
} DmMc02DmaEndpoint;

DmMc02DmaEndpointResult dm_mc02_dma_endpoint_transfer_result(
    const DmMc02DmaEndpoint *endpoint, bool memory_to_peripheral,
    uint8_t *data, unsigned size, uint64_t timestamp_ns);

/* Direct-P2M reservation helpers.  If the endpoint does not provide any
 * reservation callback, prepare_result uses the legacy consuming read path
 * and read_reservation_supported returns false. */
bool dm_mc02_dma_endpoint_read_reservation_supported(
    const DmMc02DmaEndpoint *endpoint);
DmMc02DmaEndpointResult dm_mc02_dma_endpoint_read_prepare_result(
    const DmMc02DmaEndpoint *endpoint, uint8_t *data, unsigned size,
    uint64_t timestamp_ns);
void dm_mc02_dma_endpoint_read_commit(const DmMc02DmaEndpoint *endpoint);
void dm_mc02_dma_endpoint_read_abort(const DmMc02DmaEndpoint *endpoint);

/* Read from the endpoint for P2M, or write to it for M2P. */
bool dm_mc02_dma_endpoint_transfer(const DmMc02DmaEndpoint *endpoint,
                                   bool memory_to_peripheral,
                                   uint8_t *data, unsigned size,
                                   uint64_t timestamp_ns);

#endif
