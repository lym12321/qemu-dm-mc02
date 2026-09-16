#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "dm_mc02_dma_endpoint.h"

typedef struct EndpointProbe {
    unsigned read_calls;
    unsigned write_calls;
    unsigned last_size;
    uint64_t last_timestamp_ns;
    uint8_t read_value[4];
    uint8_t written[4];
    bool fail_read;
    bool fail_write;
    unsigned retry_reads;
    unsigned retry_writes;
    unsigned prepare_calls;
    unsigned commit_calls;
    unsigned abort_calls;
    bool prepared;
} EndpointProbe;

static void check(bool condition, const char *message);

static bool probe_read(void *opaque, uint8_t *data, unsigned size,
                       uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    probe->read_calls++;
    probe->last_size = size;
    probe->last_timestamp_ns = timestamp_ns;
    memcpy(data, probe->read_value, size);
    return !probe->fail_read;
}

static bool probe_write(void *opaque, const uint8_t *data, unsigned size,
                        uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    probe->write_calls++;
    probe->last_size = size;
    probe->last_timestamp_ns = timestamp_ns;
    memcpy(probe->written, data, size);
    return !probe->fail_write;
}

static DmMc02DmaEndpointResult probe_read_ex(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    if (probe->retry_reads) {
        probe->retry_reads--;
        return DM_MC02_DMA_ENDPOINT_RETRY;
    }
    return probe_read(opaque, data, size, timestamp_ns) ?
           DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
}

static DmMc02DmaEndpointResult probe_write_ex(
    void *opaque, const uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    if (probe->retry_writes) {
        probe->retry_writes--;
        return DM_MC02_DMA_ENDPOINT_RETRY;
    }
    return probe_write(opaque, data, size, timestamp_ns) ?
           DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
}

static DmMc02DmaEndpointResult probe_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    if (probe->prepared) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    probe->prepare_calls++;
    probe->last_size = size;
    probe->last_timestamp_ns = timestamp_ns;
    memcpy(data, probe->read_value, size);
    probe->prepared = true;
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void probe_read_commit(void *opaque)
{
    EndpointProbe *probe = opaque;

    check(probe->prepared, "commit follows prepare");
    probe->prepared = false;
    probe->commit_calls++;
}

static void probe_read_abort(void *opaque)
{
    EndpointProbe *probe = opaque;

    check(probe->prepared, "abort follows prepare");
    probe->prepared = false;
    probe->abort_calls++;
}

static void check(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        _Exit(1);
    }
}

int main(void)
{
    static const unsigned widths[] = { 1, 2, 4 };
    EndpointProbe probe = {
        .read_value = { 0x11, 0x22, 0x33, 0x44 },
    };
    DmMc02DmaEndpoint endpoint = {
        .read = probe_read,
        .write = probe_write,
        .opaque = &probe,
    };
    uint8_t data[4] = { 0 };

    for (size_t i = 0; i < sizeof(widths) / sizeof(widths[0]); ++i) {
        unsigned width = widths[i];
        uint64_t timestamp = 1000 + width;

        memset(data, 0, sizeof(data));
        check(dm_mc02_dma_endpoint_transfer(&endpoint, false, data, width,
                                            timestamp), "P2M succeeds");
        check(probe.read_calls == i + 1, "P2M callback count");
        check(probe.last_size == width, "P2M width forwarded");
        check(probe.last_timestamp_ns == timestamp, "P2M timestamp forwarded");
        check(!memcmp(data, probe.read_value, width), "P2M data forwarded");

        data[0] = (uint8_t)(0xa0 + width);
        data[1] = 0xb1;
        data[2] = 0xc2;
        data[3] = 0xd3;
        check(dm_mc02_dma_endpoint_transfer(&endpoint, true, data, width,
                                            timestamp + 10), "M2P succeeds");
        check(probe.write_calls == i + 1, "M2P callback count");
        check(probe.last_size == width, "M2P width forwarded");
        check(probe.last_timestamp_ns == timestamp + 10,
              "M2P timestamp forwarded");
        check(!memcmp(probe.written, data, width), "M2P data forwarded");
    }

    probe.fail_read = true;
    check(!dm_mc02_dma_endpoint_transfer(&endpoint, false, data, 1, 2000),
          "read failure propagated");
    probe.fail_read = false;
    probe.fail_write = true;
    check(!dm_mc02_dma_endpoint_transfer(&endpoint, true, data, 1, 2001),
          "write failure propagated");

    endpoint.read_ex = probe_read_ex;
    endpoint.write_ex = probe_write_ex;
    endpoint.read = NULL;
    endpoint.write = NULL;
    probe.fail_read = false;
    probe.fail_write = false;
    probe.retry_reads = 1;
    probe.retry_writes = 1;
    check(dm_mc02_dma_endpoint_transfer_result(
              &endpoint, false, data, 1, 2002) ==
              DM_MC02_DMA_ENDPOINT_RETRY,
          "extended read backpressure propagated");
    check(dm_mc02_dma_endpoint_transfer_result(
              &endpoint, false, data, 1, 2003) ==
              DM_MC02_DMA_ENDPOINT_ACCEPTED,
          "extended read retry accepted");
    check(dm_mc02_dma_endpoint_transfer_result(
              &endpoint, true, data, 1, 2004) ==
              DM_MC02_DMA_ENDPOINT_RETRY,
          "extended write backpressure propagated");
    check(dm_mc02_dma_endpoint_transfer_result(
              &endpoint, true, data, 1, 2005) ==
              DM_MC02_DMA_ENDPOINT_ACCEPTED,
          "extended write retry accepted");

    endpoint = (DmMc02DmaEndpoint) {
        .read_prepare = probe_read_prepare,
        .read_commit = probe_read_commit,
        .read_abort = probe_read_abort,
        .opaque = &probe,
    };
    check(dm_mc02_dma_endpoint_read_reservation_supported(&endpoint),
          "complete P2M reservation tuple recognized");
    check(dm_mc02_dma_endpoint_read_prepare_result(
              &endpoint, data, 2, 2010) ==
              DM_MC02_DMA_ENDPOINT_ACCEPTED,
          "P2M reservation prepare accepted");
    check(probe.prepared && probe.prepare_calls == 1,
          "P2M reservation held after prepare");
    check(!memcmp(data, probe.read_value, 2),
          "P2M reservation data forwarded");
    dm_mc02_dma_endpoint_read_abort(&endpoint);
    check(!probe.prepared && probe.abort_calls == 1 &&
          probe.commit_calls == 0,
          "P2M reservation abort releases source");
    check(dm_mc02_dma_endpoint_read_prepare_result(
              &endpoint, data, 1, 2011) ==
              DM_MC02_DMA_ENDPOINT_ACCEPTED,
          "P2M reservation second prepare accepted");
    dm_mc02_dma_endpoint_read_commit(&endpoint);
    check(!probe.prepared && probe.prepare_calls == 2 &&
          probe.commit_calls == 1,
          "P2M reservation commit releases source");
    endpoint.read_abort = NULL;
    check(!dm_mc02_dma_endpoint_read_reservation_supported(&endpoint),
          "partial P2M reservation tuple rejected");
    check(dm_mc02_dma_endpoint_read_prepare_result(
              &endpoint, data, 1, 2012) ==
              DM_MC02_DMA_ENDPOINT_ERROR,
          "partial P2M reservation does not consume source");

    endpoint.read = NULL;
    endpoint.read_ex = NULL;
    check(!dm_mc02_dma_endpoint_transfer(&endpoint, false, data, 1, 2002),
          "missing read callback rejected");
    endpoint.write = NULL;
    endpoint.write_ex = NULL;
    check(!dm_mc02_dma_endpoint_transfer(&endpoint, true, data, 1, 2003),
          "missing write callback rejected");
    check(!dm_mc02_dma_endpoint_transfer(&endpoint, true, NULL, 1, 2004),
          "null data rejected");
    check(!dm_mc02_dma_endpoint_transfer(&endpoint, true, data, 0, 2005),
          "zero-size transfer rejected");

    puts("RESULT: DMA endpoint smoke passed");
    return 0;
}
