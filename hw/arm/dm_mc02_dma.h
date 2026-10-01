/* Minimal STM32H7 DMA and DMAMUX register models. */
#ifndef HW_ARM_DM_MC02_DMA_H
#define HW_ARM_DM_MC02_DMA_H

#include "exec/memory.h"
#include "hw/irq.h"
#include "hw/arm/dm_mc02_dma_endpoint.h"
#include "qemu/typedefs.h"

#include <stdint.h>

#define DM_MC02_DMA_REGION_SIZE    0x400
#define DM_MC02_DMAMUX_REGION_SIZE 0x400
#define DM_MC02_DMA_STREAM_COUNT   8
#define DM_MC02_DMA_FIFO_BYTES     16
#define DM_MC02_DMA_CONTROLLER_COUNT 2
#define DM_MC02_DMAMUX_COUNT          2

/* Positional children in the composite are heterogeneous.  Keep their
 * identity on the wire so a destination cannot silently bind DMA1 state to
 * DMA2 (or DMAMUX1 state to DMAMUX2). */
#define DM_MC02_DMA_SUBSYSTEM_DMA1_MARKER    UINT32_C(0x444d4431)
#define DM_MC02_DMA_SUBSYSTEM_DMA2_MARKER    UINT32_C(0x444d4432)
#define DM_MC02_DMA_SUBSYSTEM_DMAMUX1_MARKER UINT32_C(0x444d4d31)
#define DM_MC02_DMA_SUBSYSTEM_DMAMUX2_MARKER UINT32_C(0x444d4d32)

/* STM32H7 DMA stream register layout shared by request consumers that need
 * to inspect or configure a stream without duplicating the DMA model. */
#define DM_MC02_DMA_STREAM_BASE    0x010
#define DM_MC02_DMA_STREAM_STRIDE  0x018
#define DM_MC02_DMA_SxCR           0x00
#define DM_MC02_DMA_SxNDTR         0x04
#define DM_MC02_DMA_SxM0AR         0x0c
#define DM_MC02_DMA_SxCR_PBURST_SHIFT 21
#define DM_MC02_DMA_SxCR_MBURST_SHIFT 23
#define DM_MC02_DMA_SxCR_BURST_MASK   0x3u

/* SxCR burst encodings shared by the chip-layer model and fixtures. */
#define DM_MC02_DMA_BURST_SINGLE 0u
#define DM_MC02_DMA_BURST_INCR4  1u
#define DM_MC02_DMA_BURST_INCR8  2u
#define DM_MC02_DMA_BURST_INCR16 3u

typedef void DmMc02DmaStreamEnabled(void *opaque, unsigned stream,
                                    uint32_t control);

typedef struct DmMc02Dma {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_DMA_REGION_SIZE];
    /* DMA1 uses DMAMUX1 channels 0..7; DMA2 uses channels 8..15. */
    unsigned dmamux_channel_offset;
    /* The configuration which a circular stream reloads after a complete
     * transfer.  Keeping this outside the register window also lets NDTR be
     * re-armed in the usual firmware order without overloading the live
     * transfer pointers. */
    uint32_t reload_ndtr[DM_MC02_DMA_STREAM_COUNT];
    hwaddr reload_par[DM_MC02_DMA_STREAM_COUNT];
    hwaddr reload_m0ar[DM_MC02_DMA_STREAM_COUNT];
    hwaddr reload_m1ar[DM_MC02_DMA_STREAM_COUNT];
    /* The hardware address registers retain configured buffer bases.  The
     * current incremented address is internal DMA state and is selected by
     * CT in double-buffer mode. */
    hwaddr cursor_m0ar[DM_MC02_DMA_STREAM_COUNT];
    hwaddr cursor_m1ar[DM_MC02_DMA_STREAM_COUNT];
    /* STM32H7 stream FIFOs are four words deep.  Keep byte storage here so
     * packing/unpacking is independent of the configured memory/peripheral
     * widths and remains reusable by every peripheral request source. */
    uint8_t fifo[DM_MC02_DMA_STREAM_COUNT][DM_MC02_DMA_FIFO_BYTES];
    uint8_t fifo_head[DM_MC02_DMA_STREAM_COUNT];
    uint8_t fifo_length[DM_MC02_DMA_STREAM_COUNT];
    DmMc02DmaStreamEnabled *stream_enabled;
    void *stream_enabled_opaque;
    qemu_irq stream_irq[DM_MC02_DMA_STREAM_COUNT];
    /* Cache all streams carrying a request ID.  More than one DMAMUX channel
     * may legally select the same request, so a single cached stream would
     * silently starve the other channel. */
    uint8_t request_stream_mask[128];
    uint8_t request_cache_seen[128];
    /* A request ID can be raised by more than one peripheral endpoint (for
     * example ADC1->DR and ADC12_COMMON->CDR).  Cache the endpoint key too;
     * otherwise a negative lookup for one endpoint can starve another. */
    hwaddr request_cache_peripheral_addr[128];
    uint64_t request_cache_generation;
    bool request_cache_valid;
    /* DMA status IRQs are level-sensitive.  Avoid re-entering the QEMU IRQ
     * fan-out when a transfer leaves the externally visible level unchanged.
     * The validity bit is needed because a newly connected IRQ sink still
     * needs its initial level driven once. */
    bool stream_irq_level[DM_MC02_DMA_STREAM_COUNT];
    bool stream_irq_level_valid[DM_MC02_DMA_STREAM_COUNT];
} DmMc02Dma;

typedef struct DmMc02Dmamux {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_DMAMUX_REGION_SIZE];
    uint64_t generation;
} DmMc02Dmamux;

/* The H723 DMA subsystem has two stream controllers and two DMAMUX windows.
 * The DMA1/DMA2 request paths both consume DMAMUX1 in the current profile;
 * keeping all four components in one reusable state boundary preserves that
 * topology without implying a one-to-one controller/mux relationship. */
typedef struct DmMc02DmaSubsystem {
    uint32_t dma1_marker;
    uint32_t dma2_marker;
    uint32_t dmamux1_marker;
    uint32_t dmamux2_marker;
    DmMc02Dma dma[DM_MC02_DMA_CONTROLLER_COUNT];
    DmMc02Dmamux dmamux[DM_MC02_DMAMUX_COUNT];
} DmMc02DmaSubsystem;

/* DMA peripheral_addr is the fixed request/endpoint identity.  For PINC
 * streams it is matched against the captured initial PAR while live PAR is
 * advanced for each beat; endpoint callbacks are only used at that identity. */

/* Advance one enabled stream selected by a DMAMUX request.  When multiple
 * streams match, the DMA controller selects the highest SxCR.PL priority and
 * uses the lowest stream number as the deterministic tie-break.  The
 * peripheral endpoint is an address in address_space_memory (and may
 * therefore be an MMIO register).  Only one NDTR item is moved per call. */
bool dm_mc02_dma_request(DmMc02Dma *state, const DmMc02Dmamux *dmamux,
                         uint32_t request_id, hwaddr peripheral_addr);

/* Service one request through a board-independent endpoint callback.  The
 * endpoint is used instead of address_space_memory for the peripheral data
 * beat; peripheral_addr remains the configured request-routing identity. */
bool dm_mc02_dma_request_endpoint(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns);

/* Service a bounded run through the same endpoint callback.  DMA still
 * arbitrates one stream and advances one beat at a time; only IRQ fan-out is
 * deferred until the batch boundary. */
bool dm_mc02_dma_request_endpoint_batch(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns, unsigned items);

/* Service a bounded run of identical peripheral requests.  Each item still
 * performs the normal address-space read/write, but request lookup and
 * level-sensitive IRQ propagation happen once per batch.  Callers that need
 * an interrupt-visible boundary for every item must use dm_mc02_dma_request(). */
bool dm_mc02_dma_request_batch(DmMc02Dma *state,
                               const DmMc02Dmamux *dmamux,
                               uint32_t request_id,
                               hwaddr peripheral_addr,
                               unsigned items);

/* Service a repeated request batch while retaining only the final write to a
 * fixed peripheral endpoint.  This is an explicit approximation contract for
 * peripherals whose externally visible state is sampled after the batch;
 * unsupported stream configurations fall back to request_batch(). */
bool dm_mc02_dma_request_batch_coalesced(DmMc02Dma *state,
                                         const DmMc02Dmamux *dmamux,
                                         uint32_t request_id,
                                         hwaddr peripheral_addr,
                                         unsigned items);

/* Advance a known high-rate stream without issuing one MMIO transaction per
 * item.  This is used only by the board's approximate WS2812 path; accurate
 * timing continues to use dm_mc02_dma_request(). */
bool dm_mc02_dma_advance_stream(DmMc02Dma *state, unsigned stream,
                                unsigned items);

void dm_mc02_dma_set_stream_enabled_callback(DmMc02Dma *state,
                                              DmMc02DmaStreamEnabled *callback,
                                              void *opaque);

void dm_mc02_dma_set_dmamux_channel_offset(DmMc02Dma *state,
                                           unsigned channel_offset);

/* Connect one DMA stream's level-sensitive TC/TE interrupt output. */
void dm_mc02_dma_set_stream_irq(DmMc02Dma *state, unsigned stream,
                                qemu_irq irq);

void dm_mc02_dma_init(DmMc02Dma *state, Object *owner, const char *name);
void dm_mc02_dma_reset(DmMc02Dma *state);
bool dm_mc02_dma_state_valid(const DmMc02Dma *state);
/* Rebuild runtime request-cache and level-sensitive IRQ projections after
 * component state has been restored.  Wiring and board metadata remain
 * caller-owned and are not part of the component state stream. */
void dm_mc02_dma_sync_runtime(DmMc02Dma *state);
void dm_mc02_dmamux_reset(DmMc02Dmamux *state);
void dm_mc02_dmamux_init(DmMc02Dmamux *state, Object *owner,
                         const char *name);

/* Install the fixed identities required by the composite wire contract.
 * Component initialization is intentionally separate because each child
 * owns its own MemoryRegion and destination wiring. */
void dm_mc02_dma_subsystem_set_identity(DmMc02DmaSubsystem *state);

/* Component-only state contract; the MemoryRegion is runtime wiring. */
const VMStateDescription *dm_mc02_dma_vmstate(void);
const VMStateDescription *dm_mc02_dma_vmstate_raw(void);
const VMStateDescription *dm_mc02_dmamux_vmstate(void);
const VMStateDescription *dm_mc02_dma_subsystem_vmstate(void);

/* Child descriptions used by the enclosing subsystem composition. */
extern const VMStateDescription vmstate_dm_mc02_dma_raw;
extern const VMStateDescription vmstate_dm_mc02_dmamux;

#endif
