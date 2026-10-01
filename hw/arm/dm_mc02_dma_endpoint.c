/* Board-independent DMA endpoint dispatch. */
#include "hw/arm/dm_mc02_dma_endpoint.h"

DmMc02DmaEndpointResult dm_mc02_dma_endpoint_transfer_result(
    const DmMc02DmaEndpoint *endpoint, bool memory_to_peripheral,
    uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    if (!endpoint || !data || !size) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    if (memory_to_peripheral) {
        if (endpoint->write_ex) {
            DmMc02DmaEndpointResult result = endpoint->write_ex(
                endpoint->opaque, data, size, timestamp_ns);

            return result <= DM_MC02_DMA_ENDPOINT_RETRY ? result :
                   DM_MC02_DMA_ENDPOINT_ERROR;
        }
        return endpoint->write && endpoint->write(
            endpoint->opaque, data, size, timestamp_ns) ?
            DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
    }
    if (endpoint->read_ex) {
        DmMc02DmaEndpointResult result = endpoint->read_ex(
            endpoint->opaque, data, size, timestamp_ns);

        return result <= DM_MC02_DMA_ENDPOINT_RETRY ? result :
               DM_MC02_DMA_ENDPOINT_ERROR;
    }
    return endpoint->read && endpoint->read(
        endpoint->opaque, data, size, timestamp_ns) ?
        DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
}

bool dm_mc02_dma_endpoint_read_reservation_supported(
    const DmMc02DmaEndpoint *endpoint)
{
    return endpoint && endpoint->read_prepare && endpoint->read_commit &&
           endpoint->read_abort;
}

DmMc02DmaEndpointResult dm_mc02_dma_endpoint_read_prepare_result(
    const DmMc02DmaEndpoint *endpoint, uint8_t *data, unsigned size,
    uint64_t timestamp_ns)
{
    if (!endpoint || !data || !size) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    if (endpoint->read_prepare || endpoint->read_commit ||
        endpoint->read_abort) {
        DmMc02DmaEndpointResult result;

        if (!dm_mc02_dma_endpoint_read_reservation_supported(endpoint)) {
            return DM_MC02_DMA_ENDPOINT_ERROR;
        }
        result = endpoint->read_prepare(endpoint->opaque, data, size,
                                        timestamp_ns);
        return result <= DM_MC02_DMA_ENDPOINT_RETRY ? result :
               DM_MC02_DMA_ENDPOINT_ERROR;
    }
    return dm_mc02_dma_endpoint_transfer_result(endpoint, false, data, size,
                                                 timestamp_ns);
}

void dm_mc02_dma_endpoint_read_commit(const DmMc02DmaEndpoint *endpoint)
{
    endpoint->read_commit(endpoint->opaque);
}

void dm_mc02_dma_endpoint_read_abort(const DmMc02DmaEndpoint *endpoint)
{
    endpoint->read_abort(endpoint->opaque);
}

bool dm_mc02_dma_endpoint_transfer(const DmMc02DmaEndpoint *endpoint,
                                   bool memory_to_peripheral,
                                   uint8_t *data, unsigned size,
                                   uint64_t timestamp_ns)
{
    return dm_mc02_dma_endpoint_transfer_result(
        endpoint, memory_to_peripheral, data, size, timestamp_ns) ==
        DM_MC02_DMA_ENDPOINT_ACCEPTED;
}
