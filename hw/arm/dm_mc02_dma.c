/*
 * Minimal STM32H7 DMA/DMAMUX model for the DM-MC02 machine.
 *
 * The register windows are deliberately backed by byte arrays.  This keeps
 * unimplemented registers harmless while retaining the stream configuration
 * written by HAL.  The model's first purpose is to let real firmware
 * initialize DMA without a bus fault.  The stream model also implements the
 * small, synchronous memory-to-memory path
 * needed by the board firmware.  A narrow, synchronous peripheral-request
 * slice is also provided below: one matching DMAMUX request advances one
 * NDTR item and uses address_space_memory for the peripheral endpoint.
 * Double-buffering is supported for the basic peripheral-request path.  The
 * peripheral-request path also models the four-word FIFO sufficiently for
 * width packing, threshold state, memory/peripheral burst grouping,
 * synchronous request servicing, and the stream priority arbitration needed
 * when request lines compete.  Bus-level timing remains outside this slice.
 */
#include "qemu/osdep.h"
#include "exec/address-spaces.h"
#include "hw/arm/dm_mc02_dma.h"
#include "hw/arm/dm_mc02_dma_endpoint.h"
#include "sysemu/dma.h"

#define DMA_LISR   0x000
#define DMA_HISR   0x004
#define DMA_LIFCR  0x008
#define DMA_HIFCR  0x00c
#define DMA_STREAM_BASE 0x010
#define DMA_STREAM_STRIDE 0x018
#define DMA_STREAM_COUNT DM_MC02_DMA_STREAM_COUNT

/* DMA stream register offsets (RM0433, section  DMA). */
#define DMA_SxCR    0x00
#define DMA_SxNDTR  0x04
#define DMA_SxPAR   0x08
#define DMA_SxM0AR  0x0c
#define DMA_SxM1AR  0x10
#define DMA_SxFCR   0x14

#define DMA_CR_EN       (1u << 0)
#define DMA_CR_DMEIE    (1u << 1)
#define DMA_CR_TEIE     (1u << 2)
#define DMA_CR_HTIE     (1u << 3)
#define DMA_CR_TCIE     (1u << 4)
#define DMA_CR_CIRC     (1u << 8)
#define DMA_CR_DIR_MASK (3u << 6)
#define DMA_CR_DIR_P2M  0u
#define DMA_CR_DIR_M2P  (1u << 6)
#define DMA_CR_DIR_M2M  (2u << 6)
#define DMA_CR_PINC     (1u << 9)
#define DMA_CR_MINC     (1u << 10)
#define DMA_CR_PSIZE_SHIFT 11
#define DMA_CR_MSIZE_SHIFT 13
#define DMA_CR_SIZE_MASK   3u
#define DMA_CR_DBM       (1u << 18)
#define DMA_CR_CT        (1u << 19)
#define DMA_CR_PL_SHIFT  16
#define DMA_CR_PL_MASK   (3u << DMA_CR_PL_SHIFT)
#define DMA_CR_PBURST_SHIFT DM_MC02_DMA_SxCR_PBURST_SHIFT
#define DMA_CR_MBURST_SHIFT DM_MC02_DMA_SxCR_MBURST_SHIFT
#define DMA_CR_BURST_MASK   DM_MC02_DMA_SxCR_BURST_MASK

#define DMAMUX_CXCR_STRIDE 4
#define DMAMUX_REQ_ID_MASK 0x7fu

/* Stream FIFO control (RM0433, DMA_SxFCR).  FS is read-only and reflects the
 * byte FIFO occupancy in four-word quarters. */
#define DMA_FCR_FTH_MASK  (3u << 0)
#define DMA_FCR_DMDIS     (1u << 2)
#define DMA_FCR_FS_SHIFT  3
#define DMA_FCR_FS_MASK   (7u << DMA_FCR_FS_SHIFT)
#define DMA_FCR_FEIE      (1u << 7)
#define DMA_FCR_WRITE_MASK (DMA_FCR_FTH_MASK | DMA_FCR_DMDIS | DMA_FCR_FEIE)

/* The five status bits for each stream occupy a six-bit slot; bit 1 is
 * reserved by the hardware. */
#define DMA_FLAG_FEIF  (1u << 0)
#define DMA_FLAG_DMEIF (1u << 2)
#define DMA_FLAG_TCIF  (1u << 5)
#define DMA_FLAG_HTIF  (1u << 4)
#define DMA_FLAG_TEIF  (1u << 3)
static unsigned dm_mc02_dma_flag_shift(unsigned stream)
{
    /* RM0433 packs stream 0/1/2/3 into status slots 0/6/16/22;
     * streams 4..7 use the same slots in HISR. */
    static const unsigned shifts[DMA_STREAM_COUNT] = { 0, 6, 16, 22,
                                                        0, 6, 16, 22 };

    return stream < DMA_STREAM_COUNT ? shifts[stream] : 0;
}

static hwaddr dm_mc02_dma_stream_base(unsigned stream)
{
    return DMA_STREAM_BASE + stream * DMA_STREAM_STRIDE;
}

static bool dm_mc02_dma_accesses(hwaddr offset, unsigned size, hwaddr reg)
{
    return offset < reg + sizeof(uint32_t) &&
           offset + size > reg;
}

static unsigned dm_mc02_dma_width(unsigned encoding)
{
    switch (encoding & DMA_CR_SIZE_MASK) {
    case 0:
        return 1;
    case 1:
        return 2;
    case 2:
        return 4;
    default:
        return 0;
    }
}

static unsigned dm_mc02_dma_burst_beats(uint32_t cr, unsigned shift)
{
    /* STM32H7 encodes SINGLE, INCR4, INCR8 and INCR16 as 0..3. */
    return 1u << ((cr >> shift) & DMA_CR_BURST_MASK);
}

static uint32_t dm_mc02_dma_load(const uint8_t *regs, hwaddr offset,
                                 unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)regs[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_dma_store(uint8_t *regs, hwaddr offset, uint64_t value,
                              unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        regs[offset + i] = value >> (i * 8);
    }
}

static void dm_mc02_dma_update_irq(DmMc02Dma *s, unsigned stream)
{
    hwaddr status;
    hwaddr base;
    unsigned shift;
    uint32_t cr;
    uint32_t fcr;
    uint32_t flags;
    bool pending;

    if (stream >= DMA_STREAM_COUNT) {
        return;
    }

    base = dm_mc02_dma_stream_base(stream);
    status = stream < 4 ? DMA_LISR : DMA_HISR;
    shift = dm_mc02_dma_flag_shift(stream);
    cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
    fcr = dm_mc02_dma_load(s->regs, base + DMA_SxFCR, 4);
    flags = dm_mc02_dma_load(s->regs, status, 4) >> shift;
    pending = ((flags & DMA_FLAG_DMEIF) && (cr & DMA_CR_DMEIE)) ||
              ((flags & DMA_FLAG_FEIF) && (fcr & DMA_FCR_FEIE)) ||
              ((flags & DMA_FLAG_TCIF) && (cr & DMA_CR_TCIE)) ||
              ((flags & DMA_FLAG_HTIF) && (cr & DMA_CR_HTIE)) ||
              ((flags & DMA_FLAG_TEIF) && (cr & DMA_CR_TEIE));

    if (!s->stream_irq_level_valid[stream] ||
        s->stream_irq_level[stream] != pending) {
        s->stream_irq_level[stream] = pending;
        s->stream_irq_level_valid[stream] = true;
        qemu_set_irq(s->stream_irq[stream], pending);
    }
}

static void dm_mc02_dma_update_irqs(DmMc02Dma *s)
{
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        dm_mc02_dma_update_irq(s, stream);
    }
}

static void dm_mc02_dma_set_status(DmMc02Dma *s, unsigned stream,
                                    uint32_t flags)
{
    hwaddr status = stream < 4 ? DMA_LISR : DMA_HISR;
    unsigned shift = dm_mc02_dma_flag_shift(stream);
    uint32_t value = dm_mc02_dma_load(s->regs, status, 4);

    dm_mc02_dma_store(s->regs, status, value | (flags << shift), 4);
}

static hwaddr dm_mc02_dma_active_m0ar(const DmMc02Dma *s, unsigned stream,
                                      uint32_t cr)
{
    return (cr & DMA_CR_DBM) && (cr & DMA_CR_CT) ?
           s->cursor_m1ar[stream] : s->cursor_m0ar[stream];
}

static void dm_mc02_dma_store_active_m0ar(DmMc02Dma *s, unsigned stream,
                                          uint32_t cr, hwaddr address)
{
    if ((cr & DMA_CR_DBM) && (cr & DMA_CR_CT)) {
        s->cursor_m1ar[stream] = address;
    } else {
        s->cursor_m0ar[stream] = address;
    }
}

static void dm_mc02_dma_fifo_clear(DmMc02Dma *s, unsigned stream)
{
    if (stream >= DMA_STREAM_COUNT) {
        return;
    }
    s->fifo_head[stream] = 0;
    s->fifo_length[stream] = 0;
}

static bool dm_mc02_dma_fifo_push(DmMc02Dma *s, unsigned stream,
                                  const uint8_t *data, unsigned size)
{
    unsigned head;

    if (stream >= DMA_STREAM_COUNT || size > DM_MC02_DMA_FIFO_BYTES -
        s->fifo_length[stream]) {
        return false;
    }
    head = (s->fifo_head[stream] + s->fifo_length[stream]) %
           DM_MC02_DMA_FIFO_BYTES;
    for (unsigned i = 0; i < size; ++i) {
        s->fifo[stream][(head + i) % DM_MC02_DMA_FIFO_BYTES] = data[i];
    }
    s->fifo_length[stream] += size;
    return true;
}

static void dm_mc02_dma_fifo_peek(const DmMc02Dma *s, unsigned stream,
                                  uint8_t *data, unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        data[i] = s->fifo[stream][(s->fifo_head[stream] + i) %
                                   DM_MC02_DMA_FIFO_BYTES];
    }
}

static void dm_mc02_dma_fifo_drop(DmMc02Dma *s, unsigned stream,
                                  unsigned size)
{
    s->fifo_head[stream] = (s->fifo_head[stream] + size) %
                           DM_MC02_DMA_FIFO_BYTES;
    s->fifo_length[stream] -= size;
    if (!s->fifo_length[stream]) {
        s->fifo_head[stream] = 0;
    }
}

static unsigned dm_mc02_dma_fifo_status(const DmMc02Dma *s, unsigned stream)
{
    unsigned length = s->fifo_length[stream];

    if (!length) {
        return 0;
    }
    if (length <= DM_MC02_DMA_FIFO_BYTES / 4) {
        return 1;
    }
    if (length <= DM_MC02_DMA_FIFO_BYTES / 2) {
        return 2;
    }
    if (length <= DM_MC02_DMA_FIFO_BYTES * 3 / 4) {
        return 3;
    }
    return 4;
}

static unsigned dm_mc02_dma_fifo_threshold(const DmMc02Dma *s,
                                            unsigned stream)
{
    uint32_t fcr = dm_mc02_dma_load(
        s->regs, dm_mc02_dma_stream_base(stream) + DMA_SxFCR, 4);
    unsigned fth = fcr & DMA_FCR_FTH_MASK;

    return (fth == 3) ? DM_MC02_DMA_FIFO_BYTES : (fth + 1) * 4;
}

/* HTIF is a transfer status flag, not an interrupt-enable bit.  HTIE only
 * gates the corresponding IRQ output in dm_mc02_dma_update_irq().  Keeping
 * this distinction matters to polling firmware which observes HTIF with
 * interrupts disabled (and to software which enables HTIE after the event).
 */
static void dm_mc02_dma_maybe_set_half(DmMc02Dma *s, unsigned stream,
                                       uint32_t initial_count,
                                       uint32_t remaining)
{
    /* NDTR is an integer counter; for an odd programmed count the model
     * follows the STM32 convention and uses the truncated half boundary. */
    if (initial_count >= 2 && remaining == initial_count / 2) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_HTIF);
    }
}

static void dm_mc02_dma_capture_reload(DmMc02Dma *s, unsigned stream)
{
    hwaddr base;
    uint32_t count;

    if (stream >= DMA_STREAM_COUNT) {
        return;
    }

    base = dm_mc02_dma_stream_base(stream);
    count = dm_mc02_dma_load(s->regs, base + DMA_SxNDTR, 4) & 0xffff;
    s->reload_ndtr[stream] = count;
    s->reload_par[stream] =
        dm_mc02_dma_load(s->regs, base + DMA_SxPAR, 4);
    s->reload_m0ar[stream] =
        dm_mc02_dma_load(s->regs, base + DMA_SxM0AR, 4);
    s->reload_m1ar[stream] =
        dm_mc02_dma_load(s->regs, base + DMA_SxM1AR, 4);
    s->cursor_m0ar[stream] = s->reload_m0ar[stream];
    s->cursor_m1ar[stream] = s->reload_m1ar[stream];
}

static bool dm_mc02_dma_reload_circular(DmMc02Dma *s, unsigned stream,
                                        uint32_t cr, bool allow_dbm)
{
    hwaddr base;
    uint32_t reload_count;

    /* On STM32H7, enabling DBM selects the two-buffer circular state
     * machine; CIRC is ignored in that mode.  Keep DBM restricted to the
     * peripheral-request path, where the caller explicitly allows it. */
    if ((!(cr & DMA_CR_CIRC) &&
         (!allow_dbm || !(cr & DMA_CR_DBM))) ||
        stream >= DMA_STREAM_COUNT) {
        return false;
    }

    reload_count = s->reload_ndtr[stream];
    if (!reload_count) {
        return false;
    }

    base = dm_mc02_dma_stream_base(stream);
    dm_mc02_dma_store(s->regs, base + DMA_SxPAR,
                      s->reload_par[stream], 4);
    if (allow_dbm && (cr & DMA_CR_DBM)) {
        /* CT identifies the buffer that will be active after this transfer.
         * The just-finished target remains available to software while the
         * next target is reset to its captured base address. */
        cr ^= DMA_CR_CT;
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr, 4);
        dm_mc02_dma_store_active_m0ar(
            s, stream, cr,
            (cr & DMA_CR_CT) ? s->reload_m1ar[stream] :
                                s->reload_m0ar[stream]);
    } else {
        s->cursor_m0ar[stream] = s->reload_m0ar[stream];
    }
    dm_mc02_dma_store(s->regs, base + DMA_SxNDTR, reload_count, 4);
    /* EN remains set for a circular stream.  The TC flag is deliberately
     * retained until software clears it, as on the hardware peripheral. */
    return true;
}

static bool dm_mc02_dma_m2m(DmMc02Dma *s, unsigned stream)
{
    hwaddr base = dm_mc02_dma_stream_base(stream);
    uint32_t cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
    uint32_t count = dm_mc02_dma_load(s->regs, base + DMA_SxNDTR, 4) & 0xffff;
    dma_addr_t source = dm_mc02_dma_load(s->regs, base + DMA_SxPAR, 4);
    dma_addr_t dest = dm_mc02_dma_active_m0ar(s, stream, cr);
    unsigned source_width = dm_mc02_dma_width(cr >> DMA_CR_PSIZE_SHIFT);
    unsigned dest_width = dm_mc02_dma_width(cr >> DMA_CR_MSIZE_SHIFT);
    bool source_increment = cr & DMA_CR_PINC;
    bool dest_increment = cr & DMA_CR_MINC;
    uint8_t source_data[sizeof(uint32_t)];
    uint8_t dest_data[sizeof(uint32_t)];

    if (!(cr & DMA_CR_EN) || (cr & DMA_CR_DIR_MASK) != DMA_CR_DIR_M2M) {
        return false;
    }

    /* PSIZE/MSIZE encodings 3 are reserved on STM32H7. */
    if (!source_width || !dest_width) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
        return true;
    }

    for (uint32_t i = 0; i < count; ++i) {
        MemTxResult result;

        result = dma_memory_read(&address_space_memory, source,
                                 source_data, source_width,
                                 MEMTXATTRS_UNSPECIFIED);
        if (result != MEMTX_OK) {
            dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
            dm_mc02_dma_store(s->regs, base + DMA_SxCR,
                              cr & ~DMA_CR_EN, 4);
            return true;
        }

        /* DMA transfers are little-endian on this little-endian MCU.  When
         * widths differ, preserve the low bytes and zero-extend on writes;
         * equal-width transfers are a byte-for-byte copy. */
        memset(dest_data, 0, sizeof(dest_data));
        memcpy(dest_data, source_data, MIN(source_width, dest_width));
        result = dma_memory_write(&address_space_memory, dest, dest_data,
                                  dest_width, MEMTXATTRS_UNSPECIFIED);
        if (result != MEMTX_OK) {
            dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
            dm_mc02_dma_store(s->regs, base + DMA_SxCR,
                              cr & ~DMA_CR_EN, 4);
            return true;
        }

        if (source_increment) {
            source += source_width;
        }
        if (dest_increment) {
            dest += dest_width;
        }

        dm_mc02_dma_maybe_set_half(s, stream, count, count - i - 1);
    }

    dm_mc02_dma_store(s->regs, base + DMA_SxPAR, source, 4);
    dm_mc02_dma_store_active_m0ar(s, stream, cr, dest);
    if (!dm_mc02_dma_reload_circular(s, stream, cr, false)) {
        dm_mc02_dma_store(s->regs, base + DMA_SxNDTR, 0, 4);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
    }
    dm_mc02_dma_set_status(s, stream, DMA_FLAG_TCIF);
    return true;
}

static bool dm_mc02_dma_fifo_fail(DmMc02Dma *s, unsigned stream,
                                  uint32_t cr, uint32_t flag)
{
    hwaddr base = dm_mc02_dma_stream_base(stream);

    /* Disabling a stream abandons any prefetched FIFO bytes.  In particular,
     * an endpoint callback may reject a peripheral beat after memory bytes
     * were staged; those bytes must not leak into a later configuration. */
    dm_mc02_dma_fifo_clear(s, stream);
    dm_mc02_dma_set_status(s, stream, flag);
    dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
    return true;
}

static bool dm_mc02_dma_request_fifo(DmMc02Dma *s, unsigned stream,
                                     uint32_t cr, uint32_t count,
                                     dma_addr_t par, dma_addr_t m0ar,
                                     const DmMc02DmaEndpoint *endpoint,
                                     uint64_t timestamp_ns)
{
    hwaddr base = dm_mc02_dma_stream_base(stream);
    unsigned peripheral_width = dm_mc02_dma_width(cr >> DMA_CR_PSIZE_SHIFT);
    unsigned memory_width = dm_mc02_dma_width(cr >> DMA_CR_MSIZE_SHIFT);
    unsigned memory_burst_beats =
        dm_mc02_dma_burst_beats(cr, DMA_CR_MBURST_SHIFT);
    unsigned peripheral_burst_beats =
        dm_mc02_dma_burst_beats(cr, DMA_CR_PBURST_SHIFT);
    bool peripheral_increment = cr & DMA_CR_PINC;
    bool memory_increment = cr & DMA_CR_MINC;
    bool memory_to_peripheral =
        (cr & DMA_CR_DIR_MASK) == DMA_CR_DIR_M2P;
    uint32_t initial_count = s->reload_ndtr[stream] ?: count;
    unsigned threshold = dm_mc02_dma_fifo_threshold(s, stream);
    dma_addr_t memory = m0ar;
    uint8_t data[sizeof(uint32_t)] = { 0 };
    uint8_t transferred[sizeof(uint32_t)] = { 0 };
    uint8_t fifo_head_before = s->fifo_head[stream];
    uint8_t fifo_length_before = s->fifo_length[stream];
    MemTxResult result;
    unsigned memory_burst_remaining = 0;

    if (!peripheral_width || !memory_width || !memory_burst_beats ||
        !peripheral_burst_beats || !count) {
        return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_TEIF);
    }

    if (memory_to_peripheral) {
        uint64_t remaining_bytes = (uint64_t)count * peripheral_width;

        /* Fill to the configured threshold, but never fetch more than the
         * bytes required by the remaining peripheral beats.  A memory beat
         * may still contribute unused tail bytes; those bytes are discarded
         * when this transfer completes, as the hardware FIFO is flushed. */
        while (s->fifo_length[stream] < threshold &&
               s->fifo_length[stream] < remaining_bytes) {
            /* A FIFO fill may contain several memory bursts, but no one
             * burst may exceed the configured MBURST length.  The FIFO
             * capacity can truncate INCR8/INCR16 at its boundary; the next
             * fill starts a fresh burst. */
            if (!memory_burst_remaining) {
                memory_burst_remaining = memory_burst_beats;
            }
            if (s->fifo_length[stream] + memory_width >
                DM_MC02_DMA_FIFO_BYTES) {
                break;
            }
            result = dma_memory_read(&address_space_memory, memory, data,
                                     memory_width, MEMTXATTRS_UNSPECIFIED);
            if (result != MEMTX_OK) {
                return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_TEIF);
            }
            if (!dm_mc02_dma_fifo_push(s, stream, data, memory_width)) {
                return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_FEIF);
            }
            if (memory_increment) {
                memory += memory_width;
            }
            memory_burst_remaining--;
        }
        if (s->fifo_length[stream] < peripheral_width) {
            return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_FEIF);
        }

        dm_mc02_dma_fifo_peek(s, stream, transferred, peripheral_width);
        if (endpoint) {
            DmMc02DmaEndpointResult endpoint_result =
                dm_mc02_dma_endpoint_transfer_result(
                    endpoint, true, transferred, peripheral_width,
                    timestamp_ns);

            if (endpoint_result == DM_MC02_DMA_ENDPOINT_RETRY) {
                /* Memory may have been prefetched while forming this beat.
                 * It is speculative until the endpoint accepts the beat:
                 * roll back only this request's FIFO additions so the
                 * committed FIFO and memory cursor remain unchanged. */
                s->fifo_head[stream] = fifo_head_before;
                s->fifo_length[stream] = fifo_length_before;
                return false;
            }
            result = endpoint_result == DM_MC02_DMA_ENDPOINT_ACCEPTED ?
                     MEMTX_OK : MEMTX_ERROR;
        } else {
            result = dma_memory_write(&address_space_memory, par, transferred,
                                      peripheral_width,
                                      MEMTXATTRS_UNSPECIFIED);
        }
        if (result != MEMTX_OK) {
            return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_TEIF);
        }
        dm_mc02_dma_fifo_drop(s, stream, peripheral_width);
        if (peripheral_increment) {
            par += peripheral_width;
        }
    } else {
        /* A P2M peripheral beat must be accepted by the FIFO before the
         * endpoint callback or peripheral MMIO read.  Otherwise a full FIFO
         * would let the peripheral producer consume/generate an external
         * value even though DMA cannot commit that beat and will report FEIF. */
        if (s->fifo_length[stream] + peripheral_width >
            DM_MC02_DMA_FIFO_BYTES) {
            return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_FEIF);
        }
        if (endpoint) {
            DmMc02DmaEndpointResult endpoint_result =
                dm_mc02_dma_endpoint_transfer_result(
                    endpoint, false, data, peripheral_width,
                    timestamp_ns);

            if (endpoint_result == DM_MC02_DMA_ENDPOINT_RETRY) {
                return false;
            }
            result = endpoint_result == DM_MC02_DMA_ENDPOINT_ACCEPTED ?
                     MEMTX_OK : MEMTX_ERROR;
        } else {
            result = dma_memory_read(&address_space_memory, par, data,
                                     peripheral_width,
                                     MEMTXATTRS_UNSPECIFIED);
        }
        if (result != MEMTX_OK) {
            return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_TEIF);
        }
        if (!dm_mc02_dma_fifo_push(s, stream, data, peripheral_width)) {
            return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_FEIF);
        }
        if (peripheral_increment) {
            par += peripheral_width;
        }

        /* FIFO threshold controls when accumulated peripheral bytes are
         * committed to memory.  At the final peripheral beat, flush all
         * complete memory beats and zero-pad one incomplete tail beat. */
        count--;
        while (s->fifo_length[stream] >= memory_width &&
               (s->fifo_length[stream] >= threshold || !count)) {
            dm_mc02_dma_fifo_peek(s, stream, transferred, memory_width);
            result = dma_memory_write(&address_space_memory, memory,
                                      transferred, memory_width,
                                      MEMTXATTRS_UNSPECIFIED);
            if (result != MEMTX_OK) {
                return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_TEIF);
            }
            dm_mc02_dma_fifo_drop(s, stream, memory_width);
            if (memory_increment) {
                memory += memory_width;
            }
        }
        if (!count && s->fifo_length[stream]) {
            memset(transferred, 0, sizeof(transferred));
            dm_mc02_dma_fifo_peek(s, stream, transferred,
                                  s->fifo_length[stream]);
            result = dma_memory_write(&address_space_memory, memory,
                                      transferred, memory_width,
                                      MEMTXATTRS_UNSPECIFIED);
            if (result != MEMTX_OK) {
                return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_TEIF);
            }
            if (memory_increment) {
                memory += memory_width;
            }
            dm_mc02_dma_fifo_clear(s, stream);
        }
    }

    if (memory_to_peripheral) {
        count--;
    }
    dm_mc02_dma_store(s->regs, base + DMA_SxPAR, par, 4);
    dm_mc02_dma_store_active_m0ar(s, stream, cr, memory);
    dm_mc02_dma_store(s->regs, base + DMA_SxNDTR, count, 4);
    dm_mc02_dma_maybe_set_half(s, stream, initial_count, count);
    if (!count) {
        /* A complete transfer cannot carry buffered bytes into its next
         * configuration or circular epoch. */
        dm_mc02_dma_fifo_clear(s, stream);
        if (!dm_mc02_dma_reload_circular(s, stream, cr, true)) {
            dm_mc02_dma_store(s->regs, base + DMA_SxCR,
                              cr & ~DMA_CR_EN, 4);
        }
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TCIF);
    }
    return true;
}

static bool dm_mc02_dma_request_stream(DmMc02Dma *s, unsigned stream,
                                       uint32_t cr, uint32_t count,
                                       dma_addr_t par, dma_addr_t m0ar,
                                       const DmMc02DmaEndpoint *endpoint,
                                       uint64_t timestamp_ns)
{
    hwaddr base = dm_mc02_dma_stream_base(stream);
    unsigned peripheral_width = dm_mc02_dma_width(cr >> DMA_CR_PSIZE_SHIFT);
    unsigned memory_width = dm_mc02_dma_width(cr >> DMA_CR_MSIZE_SHIFT);
    bool peripheral_increment = cr & DMA_CR_PINC;
    bool memory_increment = cr & DMA_CR_MINC;
    dma_addr_t source;
    dma_addr_t dest;
    unsigned source_width;
    unsigned dest_width;
    uint8_t source_data[sizeof(uint32_t)] = { 0 };
    uint8_t dest_data[sizeof(uint32_t)] = { 0 };
    bool source_reserved = false;
    MemTxResult result;

    if (!(cr & DMA_CR_EN) || !count ||
        ((cr & DMA_CR_DIR_MASK) != DMA_CR_DIR_P2M &&
         (cr & DMA_CR_DIR_MASK) != DMA_CR_DIR_M2P)) {
        return false;
    }
    if (!peripheral_width || !memory_width) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
        return true;
    }

    if (dm_mc02_dma_load(s->regs, base + DMA_SxFCR, 4) & DMA_FCR_DMDIS) {
        return dm_mc02_dma_request_fifo(s, stream, cr, count, par, m0ar,
                                        endpoint, timestamp_ns);
    }

    /* In STM32 direct mode the FIFO is bypassed and a peripheral/memory
     * width mismatch is reported as a direct-mode error.  FIFO mode can
     * perform the corresponding packing/unpacking transfer; the current
     * synchronous model still completes that item immediately. */
    if (!(dm_mc02_dma_load(s->regs, base + DMA_SxFCR, 4) & DMA_FCR_DMDIS) &&
        peripheral_width != memory_width) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_DMEIF);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
        return true;
    }

    if ((cr & DMA_CR_DIR_MASK) == DMA_CR_DIR_M2P) {
        source = m0ar;
        dest = par;
        source_width = memory_width;
        dest_width = peripheral_width;
    } else {
        source = par;
        dest = m0ar;
        source_width = peripheral_width;
        dest_width = memory_width;
    }

    if ((cr & DMA_CR_DIR_MASK) == DMA_CR_DIR_M2P) {
        result = dma_memory_read(&address_space_memory, source, source_data,
                                 source_width, MEMTXATTRS_UNSPECIFIED);
        if (result == MEMTX_OK) {
            memcpy(dest_data, source_data, MIN(source_width, dest_width));
            if (endpoint) {
                DmMc02DmaEndpointResult endpoint_result =
                    dm_mc02_dma_endpoint_transfer_result(
                        endpoint, true, dest_data, dest_width, timestamp_ns);

                if (endpoint_result == DM_MC02_DMA_ENDPOINT_RETRY) {
                    return false;
                }
                result = endpoint_result == DM_MC02_DMA_ENDPOINT_ACCEPTED ?
                         MEMTX_OK : MEMTX_ERROR;
            } else {
                result = dma_memory_write(&address_space_memory, dest,
                                          dest_data, dest_width,
                                          MEMTXATTRS_UNSPECIFIED);
            }
        }
    } else {
        if (endpoint) {
            DmMc02DmaEndpointResult endpoint_result =
                dm_mc02_dma_endpoint_read_prepare_result(
                    endpoint, source_data, source_width, timestamp_ns);

            if (endpoint_result == DM_MC02_DMA_ENDPOINT_RETRY) {
                return false;
            }
            source_reserved =
                dm_mc02_dma_endpoint_read_reservation_supported(endpoint) &&
                endpoint_result == DM_MC02_DMA_ENDPOINT_ACCEPTED;
            result = endpoint_result == DM_MC02_DMA_ENDPOINT_ACCEPTED ?
                     MEMTX_OK : MEMTX_ERROR;
        } else {
            result = dma_memory_read(&address_space_memory, source,
                                     source_data, source_width,
                                     MEMTXATTRS_UNSPECIFIED);
        }
        if (result == MEMTX_OK) {
            memcpy(dest_data, source_data, MIN(source_width, dest_width));
            result = dma_memory_write(&address_space_memory, dest, dest_data,
                                      dest_width, MEMTXATTRS_UNSPECIFIED);
            if (source_reserved) {
                if (result == MEMTX_OK) {
                    dm_mc02_dma_endpoint_read_commit(endpoint);
                } else {
                    dm_mc02_dma_endpoint_read_abort(endpoint);
                }
            }
        }
    }
    if (result != MEMTX_OK) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
        return true;
    }

    /* This function is reached only for the valid P2M/M2P encodings.  P2M
     * is encoded as zero, so testing DIR_MASK here would incorrectly skip
     * address updates for peripheral-to-memory transfers. */
    if (peripheral_increment) {
        par += peripheral_width;
    }
    if (memory_increment) {
        m0ar += memory_width;
    }
    count--;
    dm_mc02_dma_store(s->regs, base + DMA_SxPAR, par, 4);
    dm_mc02_dma_store_active_m0ar(s, stream, cr, m0ar);
    dm_mc02_dma_store(s->regs, base + DMA_SxNDTR, count, 4);
    /* HTIF records the transfer event regardless of HTIE; HTIE is consumed
     * only by the level-sensitive IRQ calculation above. */
    dm_mc02_dma_maybe_set_half(s, stream, s->reload_ndtr[stream], count);
    if (!count) {
        if (!dm_mc02_dma_reload_circular(s, stream, cr, true)) {
            dm_mc02_dma_store(s->regs, base + DMA_SxCR,
                              cr & ~DMA_CR_EN, 4);
        }
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TCIF);
    }
    return true;
}

/* Select one stream for one peripheral request.  DMAMUX can present the same
 * request to multiple streams, but a DMA controller arbitrates that request;
 * it must not broadcast one peripheral beat to every matching stream. */
static unsigned dm_mc02_dma_pick_request_stream(const DmMc02Dma *s,
                                                uint8_t stream_mask)
{
    unsigned selected = DMA_STREAM_COUNT;
    unsigned selected_priority = 0;

    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        hwaddr base;
        uint32_t cr;
        uint32_t direction;
        unsigned priority;

        if (!(stream_mask & (1u << stream))) {
            continue;
        }
        base = dm_mc02_dma_stream_base(stream);
        cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
        direction = cr & DMA_CR_DIR_MASK;
        if (!(cr & DMA_CR_EN) ||
            !(dm_mc02_dma_load(s->regs, base + DMA_SxNDTR, 4) & 0xffff) ||
            (direction != DMA_CR_DIR_P2M && direction != DMA_CR_DIR_M2P)) {
            continue;
        }
        priority = (cr & DMA_CR_PL_MASK) >> DMA_CR_PL_SHIFT;
        /* The ascending loop provides the lower stream-number tie break. */
        if (selected == DMA_STREAM_COUNT || priority > selected_priority) {
            selected = stream;
            selected_priority = priority;
        }
    }
    return selected;
}

static bool dm_mc02_dma_request_selected(DmMc02Dma *s, unsigned stream,
                                          hwaddr peripheral_addr,
                                          const DmMc02DmaEndpoint *endpoint,
                                          uint64_t timestamp_ns)
{
    hwaddr base = dm_mc02_dma_stream_base(stream);
    uint32_t cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
    dma_addr_t transfer_endpoint = peripheral_addr;
    const DmMc02DmaEndpoint *transfer_callback = endpoint;

    /* The request source identifies a fixed peripheral endpoint, while PAR
     * is the live cursor for peripheral-increment transfers.  Keep the
     * endpoint argument for normal streams and use the cursor only for PINC;
     * request matching has already checked the captured configuration base. */
    if (cr & DMA_CR_PINC) {
        transfer_endpoint = dm_mc02_dma_load(s->regs, base + DMA_SxPAR, 4);
        /* An endpoint represents one fixed peripheral register.  Once PINC
         * moves PAR away from that register, use the ordinary MMIO path so
         * the request still completes with the correct address side effect,
         * but do not deliver an unrelated address to the device callback. */
        if (transfer_endpoint != peripheral_addr) {
            transfer_callback = NULL;
        }
    }

    return dm_mc02_dma_request_stream(
        s, stream, cr,
        dm_mc02_dma_load(s->regs, base + DMA_SxNDTR, 4) & 0xffff,
        transfer_endpoint, dm_mc02_dma_active_m0ar(s, stream, cr),
        transfer_callback,
        timestamp_ns);
}

/* PAR is a live transfer cursor when PINC is set.  Peripheral request sources
 * identify the configured endpoint, not the cursor after the previous beat;
 * use the captured base for routing while request_stream() still uses the
 * live PAR for the actual address-space access. */
static bool dm_mc02_dma_stream_endpoint_matches(const DmMc02Dma *s,
                                                unsigned stream,
                                                hwaddr peripheral_addr)
{
    hwaddr base = dm_mc02_dma_stream_base(stream);
    uint32_t cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
    hwaddr endpoint = dm_mc02_dma_load(s->regs, base + DMA_SxPAR, 4);

    if (cr & DMA_CR_PINC) {
        endpoint = s->reload_ndtr[stream] ? s->reload_par[stream] : endpoint;
    }
    return endpoint == peripheral_addr;
}

static bool dm_mc02_dma_request_internal(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns)
{
    uint32_t requested = request_id & DMAMUX_REQ_ID_MASK;
    uint8_t cached_mask = 0;

    if (state->request_cache_valid &&
        state->request_cache_generation == dmamux->generation &&
        state->request_cache_seen[requested] &&
        state->request_cache_peripheral_addr[requested] == peripheral_addr) {
        cached_mask = state->request_stream_mask[requested];
        if (!cached_mask) {
            return false;
        }
        if (!(cached_mask & (cached_mask - 1))) {
            unsigned stream = ctz32(cached_mask);
            unsigned channel = stream + state->dmamux_channel_offset;

            if (channel * DMAMUX_CXCR_STRIDE + 4 <=
                    DM_MC02_DMAMUX_REGION_SIZE &&
                (dm_mc02_dma_load(dmamux->regs,
                                  channel * DMAMUX_CXCR_STRIDE, 4) &
                 DMAMUX_REQ_ID_MASK) == requested &&
                dm_mc02_dma_stream_endpoint_matches(state, stream,
                                                     peripheral_addr)) {
                bool moved = dm_mc02_dma_request_selected(
                    state, stream, peripheral_addr, endpoint, timestamp_ns);

                dm_mc02_dma_update_irq(state, stream);
                return moved;
            }
            /* The stream tuple changed without a DMAMUX generation change;
             * rebuild the cached endpoint before servicing the request. */
            goto scan_request;
        }
        {
            bool valid = true;

            for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
                unsigned channel;
                uint32_t mux;

                if (!(cached_mask & (1u << stream))) {
                    continue;
                }
                channel = stream + state->dmamux_channel_offset;
                if (channel * DMAMUX_CXCR_STRIDE + 4 >
                        DM_MC02_DMAMUX_REGION_SIZE) {
                    valid = false;
                    break;
                }
                mux = dm_mc02_dma_load(dmamux->regs,
                                       channel * DMAMUX_CXCR_STRIDE, 4);
                if ((mux & DMAMUX_REQ_ID_MASK) != requested ||
                    !dm_mc02_dma_stream_endpoint_matches(
                        state, stream, peripheral_addr)) {
                    valid = false;
                    break;
                }
            }
            if (valid) {
                unsigned stream = dm_mc02_dma_pick_request_stream(
                    state, cached_mask);

                if (stream == DMA_STREAM_COUNT) {
                    return false;
                }
                bool moved = dm_mc02_dma_request_selected(
                    state, stream, peripheral_addr, endpoint, timestamp_ns);

                dm_mc02_dma_update_irq(state, stream);
                return moved;
            }
            /* The stream tuple changed without a DMAMUX generation change;
             * rebuild the cached endpoint before servicing the request. */
            goto scan_request;
        }
    }

scan_request:
    if (!state->request_cache_valid ||
        state->request_cache_generation != dmamux->generation) {
        memset(state->request_stream_mask, 0,
               sizeof(state->request_stream_mask));
        memset(state->request_cache_seen, 0, sizeof(state->request_cache_seen));
        state->request_cache_generation = dmamux->generation;
        state->request_cache_valid = true;
    }

    /* STM32H7 maps DMA1 Stream N to DMAMUX1 channel N and DMA2 Stream N to
     * DMAMUX1 channel N+8.  Looking up the request ID in the channel register
     * keeps endpoint selection out of the DMA engine; the board passes the
     * expected PAR as an additional narrow-slice guard. */
    uint8_t stream_mask = 0;

    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        unsigned channel = stream + state->dmamux_channel_offset;
        uint32_t mux;

        if (channel * DMAMUX_CXCR_STRIDE + 4 > DM_MC02_DMAMUX_REGION_SIZE) {
            continue;
        }
        mux = dm_mc02_dma_load(dmamux->regs,
                               channel * DMAMUX_CXCR_STRIDE, 4);

        if ((mux & DMAMUX_REQ_ID_MASK) != requested ||
            !dm_mc02_dma_stream_endpoint_matches(state, stream,
                                                  peripheral_addr)) {
            continue;
        }
        stream_mask |= 1u << stream;
    }
    state->request_stream_mask[requested] = stream_mask;
    state->request_cache_seen[requested] = 1;
    state->request_cache_peripheral_addr[requested] = peripheral_addr;

    /* Requests are raised by peripheral MMIO events rather than by the DMA
     * register write path, so refresh each matching level-sensitive IRQ. */
    unsigned stream = dm_mc02_dma_pick_request_stream(state, stream_mask);

    if (stream == DMA_STREAM_COUNT) {
        return false;
    }
    bool moved = dm_mc02_dma_request_selected(state, stream,
                                              peripheral_addr, endpoint,
                                              timestamp_ns);

    dm_mc02_dma_update_irq(state, stream);
    return moved;
}

bool dm_mc02_dma_request(DmMc02Dma *state, const DmMc02Dmamux *dmamux,
                         uint32_t request_id, hwaddr peripheral_addr)
{
    return dm_mc02_dma_request_internal(state, dmamux, request_id,
                                        peripheral_addr, NULL, 0);
}

bool dm_mc02_dma_request_endpoint(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns)
{
    if (!endpoint || (!endpoint->read && !endpoint->write &&
                      !endpoint->read_ex && !endpoint->write_ex &&
                      !endpoint->read_prepare)) {
        return false;
    }
    return dm_mc02_dma_request_internal(state, dmamux, request_id,
                                        peripheral_addr, endpoint,
                                        timestamp_ns);
}

static bool dm_mc02_dma_batch_stream_matches(const DmMc02Dma *state,
                                             const DmMc02Dmamux *dmamux,
                                             unsigned stream,
                                             uint32_t requested,
                                             hwaddr peripheral_addr)
{
    unsigned channel = stream + state->dmamux_channel_offset;

    if (channel * DMAMUX_CXCR_STRIDE + 4 > DM_MC02_DMAMUX_REGION_SIZE) {
        return false;
    }
    return (dm_mc02_dma_load(dmamux->regs,
                             channel * DMAMUX_CXCR_STRIDE, 4) &
            DMAMUX_REQ_ID_MASK) == requested &&
           dm_mc02_dma_stream_endpoint_matches(state, stream,
                                                peripheral_addr);
}

static bool dm_mc02_dma_request_batch_internal(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns, unsigned items)
{
    uint32_t requested = request_id & DMAMUX_REQ_ID_MASK;
    uint8_t stream_mask = 0;
    bool moved = false;

    if (!state || !dmamux || !items) {
        return false;
    }

    /* The batch is deliberately resolved once.  It is a bounded fast path
     * for repeated events from one peripheral endpoint; the legacy request()
     * path remains available when each event must expose its own IRQ edge. */
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        if (dm_mc02_dma_batch_stream_matches(state, dmamux, stream,
                                             requested, peripheral_addr)) {
            stream_mask |= 1u << stream;
        }
    }
    if (!stream_mask) {
        return false;
    }

    /* Preserve the order of repeated request() calls: each peripheral event
     * is arbitrated independently.  Read PAR for each item so a selected
     * peripheral-increment stream keeps its endpoint semantics. */
    for (unsigned item = 0; item < items; ++item) {
        unsigned stream = dm_mc02_dma_pick_request_stream(state, stream_mask);

        if (stream == DMA_STREAM_COUNT) {
            break;
        }
        bool item_moved = dm_mc02_dma_request_selected(
            state, stream,
            endpoint ? peripheral_addr :
            dm_mc02_dma_load(state->regs,
                             dm_mc02_dma_stream_base(stream) + DMA_SxPAR, 4),
            endpoint, timestamp_ns);

        if (!item_moved) {
            /* RETRY is deliberately caller-driven.  Do not spin on a
             * backpressured endpoint in a bounded batch, and leave any
             * later peripheral event to submit the same beat again. */
            break;
        }
        moved = true;
    }

    /* Status flags remain latched per item, while the expensive IRQ fan-out
     * is intentionally performed only at the batch boundary. */
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        if (stream_mask & (1u << stream)) {
            dm_mc02_dma_update_irq(state, stream);
        }
    }
    return moved;
}

bool dm_mc02_dma_request_batch(DmMc02Dma *state,
                               const DmMc02Dmamux *dmamux,
                               uint32_t request_id,
                               hwaddr peripheral_addr,
                               unsigned items)
{
    return dm_mc02_dma_request_batch_internal(state, dmamux, request_id,
                                              peripheral_addr, NULL, 0,
                                              items);
}

bool dm_mc02_dma_request_endpoint_batch(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns, unsigned items)
{
    if (!endpoint || (!endpoint->read && !endpoint->write &&
                      !endpoint->read_ex && !endpoint->write_ex &&
                      !endpoint->read_prepare)) {
        return false;
    }
    return dm_mc02_dma_request_batch_internal(
        state, dmamux, request_id, peripheral_addr, endpoint, timestamp_ns,
        items);
}

static bool dm_mc02_dma_request_batch_coalesced_one(
    DmMc02Dma *s, unsigned stream, hwaddr peripheral_addr, unsigned items)
{
    hwaddr base = dm_mc02_dma_stream_base(stream);
    uint32_t cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
    uint32_t count = dm_mc02_dma_load(s->regs, base + DMA_SxNDTR, 4) & 0xffff;
    unsigned peripheral_width = dm_mc02_dma_width(cr >> DMA_CR_PSIZE_SHIFT);
    unsigned memory_width = dm_mc02_dma_width(cr >> DMA_CR_MSIZE_SHIFT);
    dma_addr_t cursor;
    dma_addr_t source;
    uint32_t moved;
    uint32_t span;
    uint8_t data[sizeof(uint32_t)] = { 0 };
    MemTxResult result;

    /* The coalesced contract is deliberately narrow.  DBM and FIFO mode
     * expose additional per-beat state which must retain the exact path. */
    if (!(cr & DMA_CR_EN) || (cr & DMA_CR_DBM) ||
        (cr & DMA_CR_DIR_MASK) != DMA_CR_DIR_M2P ||
        (cr & DMA_CR_PINC) || !items || !count ||
        (dm_mc02_dma_load(s->regs, base + DMA_SxFCR, 4) & DMA_FCR_DMDIS) ||
        !peripheral_width || peripheral_width != memory_width) {
        return false;
    }

    moved = (cr & DMA_CR_CIRC) ? items : MIN(items, count);
    if (!moved) {
        return false;
    }

    cursor = dm_mc02_dma_active_m0ar(s, stream, cr);
    source = cursor;
    if (cr & DMA_CR_MINC) {
        span = s->reload_ndtr[stream] ?: count;
        if (!span || cursor < s->reload_m0ar[stream] ||
            (cursor - s->reload_m0ar[stream]) % memory_width) {
            return false;
        }
        if (cr & DMA_CR_CIRC) {
            uint64_t start_index =
                (cursor - s->reload_m0ar[stream]) / memory_width;
            uint64_t last_index =
                (start_index + (uint64_t)moved - 1) % span;

            source = s->reload_m0ar[stream] + last_index * memory_width;
        } else {
            source += ((uint64_t)moved - 1) * memory_width;
        }
    }

    /* Read the value corresponding to the final abstract beat before
     * advancing the stream.  The endpoint write is intentionally collapsed;
     * all NDTR, HT/TC, circular and IRQ state is still advanced by the shared
     * state machine below. */
    result = dma_memory_read(&address_space_memory, source, data,
                             memory_width, MEMTXATTRS_UNSPECIFIED);
    if (result != MEMTX_OK) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
        dm_mc02_dma_update_irq(s, stream);
        return true;
    }
    result = dma_memory_write(&address_space_memory, peripheral_addr, data,
                              peripheral_width, MEMTXATTRS_UNSPECIFIED);
    if (result != MEMTX_OK) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
        dm_mc02_dma_update_irq(s, stream);
        return true;
    }

    return dm_mc02_dma_advance_stream(s, stream, moved);
}

bool dm_mc02_dma_request_batch_coalesced(DmMc02Dma *state,
                                         const DmMc02Dmamux *dmamux,
                                         uint32_t request_id,
                                         hwaddr peripheral_addr,
                                         unsigned items)
{
    uint32_t requested = request_id & DMAMUX_REQ_ID_MASK;
    uint8_t stream_mask = 0;

    if (!state || !dmamux || !items) {
        return false;
    }
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        if (dm_mc02_dma_batch_stream_matches(state, dmamux, stream,
                                             requested, peripheral_addr)) {
            stream_mask |= 1u << stream;
        }
    }
    if (!stream_mask || (stream_mask & (stream_mask - 1))) {
        return dm_mc02_dma_request_batch(state, dmamux, request_id,
                                         peripheral_addr, items);
    }

    {
        unsigned stream = ctz32(stream_mask);
        bool moved = dm_mc02_dma_request_batch_coalesced_one(
            state, stream, peripheral_addr, items);

        /* A configuration outside the coalesced contract must retain the
         * normal per-item side effects and status transitions. */
        if (!moved && (dm_mc02_dma_load(
                           state->regs,
                           dm_mc02_dma_stream_base(stream) + DMA_SxCR, 4) &
                       DMA_CR_EN)) {
            return dm_mc02_dma_request_batch(state, dmamux, request_id,
                                             peripheral_addr, items);
        }
        return moved;
    }
}

bool dm_mc02_dma_advance_stream(DmMc02Dma *s, unsigned stream,
                                unsigned items)
{
    hwaddr base;
    uint32_t cr;
    unsigned memory_width;
    unsigned peripheral_width;
    bool moved = false;

    if (!s || stream >= DMA_STREAM_COUNT || !items) {
        return false;
    }
    base = dm_mc02_dma_stream_base(stream);
    cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
    if (!(cr & DMA_CR_EN) ||
        ((cr & DMA_CR_DIR_MASK) != DMA_CR_DIR_P2M &&
         (cr & DMA_CR_DIR_MASK) != DMA_CR_DIR_M2P)) {
        return false;
    }
    memory_width = dm_mc02_dma_width(cr >> DMA_CR_MSIZE_SHIFT);
    peripheral_width = dm_mc02_dma_width(cr >> DMA_CR_PSIZE_SHIFT);
    if (!memory_width || !peripheral_width) {
        dm_mc02_dma_set_status(s, stream, DMA_FLAG_TEIF);
        dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr & ~DMA_CR_EN, 4);
        dm_mc02_dma_update_irq(s, stream);
        return true;
    }

    if (dm_mc02_dma_load(s->regs, base + DMA_SxFCR, 4) & DMA_FCR_DMDIS) {
        uint32_t initial_count = s->reload_ndtr[stream];
        bool memory_to_peripheral =
            (cr & DMA_CR_DIR_MASK) == DMA_CR_DIR_M2P;
        dma_addr_t memory = dm_mc02_dma_active_m0ar(s, stream, cr);
        uint8_t zero[sizeof(uint32_t)] = { 0 };

        /* This API intentionally has no peripheral endpoint, so it can only
         * represent the state side of a FIFO M2P stream.  P2M callers must
         * use dm_mc02_dma_request(), which supplies the peripheral data. */
        if (!memory_to_peripheral) {
            return false;
        }
        if (!initial_count) {
            initial_count = dm_mc02_dma_load(s->regs, base + DMA_SxNDTR,
                                             4) & 0xffff;
        }
        while (items && (cr & DMA_CR_EN)) {
            uint32_t ndtr = dm_mc02_dma_load(s->regs, base + DMA_SxNDTR, 4) &
                             0xffff;
            uint64_t remaining_bytes = (uint64_t)ndtr * peripheral_width;
            unsigned threshold = dm_mc02_dma_fifo_threshold(s, stream);

            if (!ndtr) {
                cr &= ~DMA_CR_EN;
                dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr, 4);
                break;
            }
            while (s->fifo_length[stream] < threshold &&
                   s->fifo_length[stream] < remaining_bytes) {
                if (s->fifo_length[stream] + memory_width >
                    DM_MC02_DMA_FIFO_BYTES) {
                    break;
                }
                if (!dm_mc02_dma_fifo_push(s, stream, zero, memory_width)) {
                    return dm_mc02_dma_fifo_fail(s, stream, cr,
                                                 DMA_FLAG_FEIF);
                }
                if (cr & DMA_CR_MINC) {
                    memory += memory_width;
                }
            }
            if (s->fifo_length[stream] < peripheral_width) {
                return dm_mc02_dma_fifo_fail(s, stream, cr, DMA_FLAG_FEIF);
            }
            dm_mc02_dma_fifo_drop(s, stream, peripheral_width);
            if (cr & DMA_CR_PINC) {
                uint32_t par = dm_mc02_dma_load(s->regs,
                                                 base + DMA_SxPAR, 4);
                dm_mc02_dma_store(s->regs, base + DMA_SxPAR,
                                  par + peripheral_width, 4);
            }
            ndtr--;
            dm_mc02_dma_store(s->regs, base + DMA_SxNDTR, ndtr, 4);
            dm_mc02_dma_maybe_set_half(s, stream, initial_count, ndtr);
            items--;
            moved = true;
            if (!ndtr) {
                dm_mc02_dma_fifo_clear(s, stream);
                dm_mc02_dma_set_status(s, stream, DMA_FLAG_TCIF);
                if (!dm_mc02_dma_reload_circular(s, stream, cr, true)) {
                    cr &= ~DMA_CR_EN;
                    dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr, 4);
                } else {
                    cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
                    memory = dm_mc02_dma_active_m0ar(s, stream, cr);
                }
            }
        }
        dm_mc02_dma_store_active_m0ar(s, stream, cr, memory);
        dm_mc02_dma_update_irq(s, stream);
        return moved;
    }

    /* This path deliberately models the state transition, not the omitted
     * high-rate peripheral side effects.  It is used for the WS2812 stream,
     * whose actual output is observed from the firmware buffer. */
    while (items && (cr & DMA_CR_EN)) {
        uint32_t ndtr = dm_mc02_dma_load(s->regs, base + DMA_SxNDTR, 4) &
                         0xffff;
        uint32_t count;
        uint32_t before;

        if (!ndtr) {
            cr &= ~DMA_CR_EN;
            break;
        }
        count = MIN((uint32_t)items, ndtr);
        before = ndtr;
        ndtr -= count;
        items -= count;
        moved = true;

        if (cr & DMA_CR_PINC) {
            uint32_t par = dm_mc02_dma_load(s->regs, base + DMA_SxPAR, 4);
            dm_mc02_dma_store(s->regs, base + DMA_SxPAR,
                              par + count * peripheral_width, 4);
        }
        if (cr & DMA_CR_MINC) {
            hwaddr maddr = dm_mc02_dma_active_m0ar(s, stream, cr);
            dm_mc02_dma_store_active_m0ar(s, stream, cr,
                                          maddr + count * memory_width);
        }
        dm_mc02_dma_store(s->regs, base + DMA_SxNDTR, ndtr, 4);
        if (s->reload_ndtr[stream] >= 2 &&
            before > s->reload_ndtr[stream] / 2 &&
            ndtr <= s->reload_ndtr[stream] / 2) {
            dm_mc02_dma_set_status(s, stream, DMA_FLAG_HTIF);
        }
        if (!ndtr) {
            dm_mc02_dma_set_status(s, stream, DMA_FLAG_TCIF);
            if (!dm_mc02_dma_reload_circular(s, stream, cr, true)) {
                cr &= ~DMA_CR_EN;
                dm_mc02_dma_store(s->regs, base + DMA_SxCR, cr, 4);
            } else {
                /* DBM toggles CT in the register window.  Keep the local
                 * control value in sync when one batched call crosses a
                 * buffer boundary. */
                cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);
            }
        }
    }
    dm_mc02_dma_update_irq(s, stream);
    return moved;
}

static void dm_mc02_dma_maybe_run(DmMc02Dma *s, hwaddr offset)
{
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        hwaddr base = dm_mc02_dma_stream_base(stream);

        if (offset == base + DMA_SxCR) {
            dm_mc02_dma_m2m(s, stream);
            return;
        }
    }
}

static uint64_t dm_mc02_dma_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Dma *s = opaque;

    if (offset + size > DM_MC02_DMA_REGION_SIZE || size > 4) {
        return 0;
    }
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        if (offset == dm_mc02_dma_stream_base(stream) + DMA_SxFCR &&
            size == 4) {
            uint32_t value = dm_mc02_dma_load(s->regs, offset, size);

            value &= ~DMA_FCR_FS_MASK;
            value |= dm_mc02_dma_fifo_status(s, stream) << DMA_FCR_FS_SHIFT;
            return value;
        }
    }
    return dm_mc02_dma_load(s->regs, offset, size);
}

static void dm_mc02_dma_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    DmMc02Dma *s = opaque;
    uint32_t clear;
    uint32_t old_cr = 0;

    if (offset + size > DM_MC02_DMA_REGION_SIZE || size > 4) {
        return;
    }

    /* A changed CR or PAR can change both eligibility and endpoint matching;
     * invalidate the request index before the next peripheral event. */
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        hwaddr base = dm_mc02_dma_stream_base(stream);

        if (dm_mc02_dma_accesses(offset, size, base + DMA_SxCR) ||
            dm_mc02_dma_accesses(offset, size, base + DMA_SxPAR)) {
            s->request_cache_valid = false;
            break;
        }
    }

    /* LIFCR/HIFCR are write-one-to-clear views of the flag registers. */
    if (offset == DMA_LIFCR && size == 4) {
        clear = value;
        dm_mc02_dma_store(s->regs, DMA_LISR,
                          dm_mc02_dma_load(s->regs, DMA_LISR, 4) & ~clear, 4);
        dm_mc02_dma_update_irqs(s);
        return;
    }
    if (offset == DMA_HIFCR && size == 4) {
        clear = value;
        dm_mc02_dma_store(s->regs, DMA_HISR,
                          dm_mc02_dma_load(s->regs, DMA_HISR, 4) & ~clear, 4);
        dm_mc02_dma_update_irqs(s);
        return;
    }
    if (size == 4) {
        for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
            if (offset == dm_mc02_dma_stream_base(stream) + DMA_SxFCR) {
                /* FTH, DMDIS and FEIE are writable; FS and reserved bits are
                 * not configuration state and must not be latched. */
                value &= DMA_FCR_WRITE_MASK;
                break;
            }
        }
    }
    if (size == 4) {
        for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
            if (offset == dm_mc02_dma_stream_base(stream) + DMA_SxCR) {
                old_cr = dm_mc02_dma_load(s->regs, offset, 4);
                break;
            }
        }
    }
    dm_mc02_dma_store(s->regs, offset, value, size);

    /* Firmware normally writes PAR/M0AR, then NDTR, then enables the
     * stream.  Capture the complete initial tuple when NDTR is written;
     * while disabled, address writes also refresh the tuple so either
     * common ordering remains useful. */
    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        hwaddr base = dm_mc02_dma_stream_base(stream);

        if (offset == base + DMA_SxNDTR && size == 4) {
            if (!(dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4) &
                  DMA_CR_EN)) {
                dm_mc02_dma_fifo_clear(s, stream);
            }
            dm_mc02_dma_capture_reload(s, stream);
            break;
        }
        if ((offset == base + DMA_SxPAR ||
             offset == base + DMA_SxM0AR ||
             offset == base + DMA_SxM1AR) && size == 4) {
            uint32_t cr = dm_mc02_dma_load(s->regs, base + DMA_SxCR, 4);

            if ((cr & DMA_CR_DBM) && (cr & DMA_CR_EN) &&
                (offset == base + DMA_SxM0AR ||
                 offset == base + DMA_SxM1AR)) {
                /* In DBM mode MxAR is the configuration base, while the
                 * current transfer address lives in the private cursor.  A
                 * running stream may replace a buffer base without
                 * disturbing the buffer currently in use.  Reset the
                 * inactive cursor immediately so the next CT transition
                 * starts at the newly programmed address. */
                bool target_m1 = offset == base + DMA_SxM1AR;
                bool active_m1 = (cr & DMA_CR_CT) != 0;

                if (target_m1 != active_m1) {
                    if (target_m1) {
                        s->reload_m1ar[stream] = value;
                        s->cursor_m1ar[stream] = value;
                    } else {
                        s->reload_m0ar[stream] = value;
                        s->cursor_m0ar[stream] = value;
                    }
                } else if (target_m1) {
                    /* The active cursor remains authoritative until the
                     * controller switches away and back to this target. */
                    s->reload_m1ar[stream] = value;
                } else {
                    s->reload_m0ar[stream] = value;
                }
            } else if (!(cr & DMA_CR_EN) && s->reload_ndtr[stream]) {
                if (offset == base + DMA_SxPAR) {
                    s->reload_par[stream] = value;
                } else if (offset == base + DMA_SxM0AR) {
                    s->reload_m0ar[stream] = value;
                    s->cursor_m0ar[stream] = value;
                } else {
                    s->reload_m1ar[stream] = value;
                    s->cursor_m1ar[stream] = value;
                }
            }
            break;
        }
    }

    /* A stream may be enabled after all configuration registers were
     * written with a sub-word access pattern.  Ensure there is still a
     * reload tuple before the first circular transfer. */
    if (size == 4) {
        for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
            hwaddr base = dm_mc02_dma_stream_base(stream);

            if (offset == base + DMA_SxCR &&
                value & DMA_CR_EN) {
                if (!(old_cr & DMA_CR_EN)) {
                    dm_mc02_dma_fifo_clear(s, stream);
                }
                if (!s->reload_ndtr[stream]) {
                    dm_mc02_dma_capture_reload(s, stream);
                }
                break;
            }
            if (offset == base + DMA_SxCR && !(value & DMA_CR_EN)) {
                dm_mc02_dma_fifo_clear(s, stream);
                break;
            }
        }
    }
    dm_mc02_dma_maybe_run(s, offset);

    /* TC/TE interrupt output is level-sensitive.  Re-evaluate it after a
     * CR write as well as after a transfer, so enabling/disabling TCIE/TEIE
     * while a status flag is already set has the expected effect. */
    dm_mc02_dma_update_irqs(s);

    /* The board's SPI TX path uses this as its explicit request source.  The
     * register has already been committed before the callback runs, and the
     * callback only schedules a timer; it never transfers from this MMIO
     * write stack. */
    if (size == 4 && !(old_cr & DMA_CR_EN) && (value & DMA_CR_EN) &&
        s->stream_enabled) {
        for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
            if (offset == dm_mc02_dma_stream_base(stream) + DMA_SxCR) {
                s->stream_enabled(s->stream_enabled_opaque, stream,
                                  (uint32_t)value);
                break;
            }
        }
    }
}

static const MemoryRegionOps dm_mc02_dma_ops = {
    .read = dm_mc02_dma_read,
    .write = dm_mc02_dma_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static uint64_t dm_mc02_dmamux_read(void *opaque, hwaddr offset,
                                    unsigned size)
{
    DmMc02Dmamux *s = opaque;

    if (offset + size > DM_MC02_DMAMUX_REGION_SIZE || size > 4) {
        return 0;
    }
    return dm_mc02_dma_load(s->regs, offset, size);
}

static void dm_mc02_dmamux_write(void *opaque, hwaddr offset, uint64_t value,
                                 unsigned size)
{
    DmMc02Dmamux *s = opaque;

    if (offset + size > DM_MC02_DMAMUX_REGION_SIZE || size > 4) {
        return;
    }
    dm_mc02_dma_store(s->regs, offset, value, size);
    s->generation++;
}

static const MemoryRegionOps dm_mc02_dmamux_ops = {
    .read = dm_mc02_dmamux_read,
    .write = dm_mc02_dmamux_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_dma_init(DmMc02Dma *state, Object *owner, const char *name)
{
    memset(state, 0, sizeof(*state));
    memset(state->request_stream_mask, 0,
           sizeof(state->request_stream_mask));
    memset(state->request_cache_seen, 0, sizeof(state->request_cache_seen));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_dma_ops, state, name,
                          DM_MC02_DMA_REGION_SIZE);
}

void dm_mc02_dma_reset(DmMc02Dma *s)
{
    if (!s) {
        return;
    }
    memset(s->regs, 0, sizeof(s->regs));
    memset(s->reload_ndtr, 0, sizeof(s->reload_ndtr));
    memset(s->reload_par, 0, sizeof(s->reload_par));
    memset(s->reload_m0ar, 0, sizeof(s->reload_m0ar));
    memset(s->reload_m1ar, 0, sizeof(s->reload_m1ar));
    memset(s->cursor_m0ar, 0, sizeof(s->cursor_m0ar));
    memset(s->cursor_m1ar, 0, sizeof(s->cursor_m1ar));
    memset(s->request_stream_mask, 0, sizeof(s->request_stream_mask));
    memset(s->request_cache_seen, 0, sizeof(s->request_cache_seen));
    memset(s->request_cache_peripheral_addr, 0,
           sizeof(s->request_cache_peripheral_addr));
    memset(s->stream_irq_level, 0, sizeof(s->stream_irq_level));
    memset(s->stream_irq_level_valid, 0,
           sizeof(s->stream_irq_level_valid));
    s->request_cache_generation = 0;
    s->request_cache_valid = false;
    dm_mc02_dma_update_irqs(s);
}

bool dm_mc02_dma_state_valid(const DmMc02Dma *state)
{
    if (!state) {
        return false;
    }

    for (unsigned stream = 0; stream < DMA_STREAM_COUNT; ++stream) {
        if (state->reload_ndtr[stream] > UINT16_MAX ||
            state->fifo_head[stream] >= DM_MC02_DMA_FIFO_BYTES ||
            state->fifo_length[stream] > DM_MC02_DMA_FIFO_BYTES ||
            (!state->fifo_length[stream] && state->fifo_head[stream])) {
            return false;
        }
    }
    return true;
}

void dm_mc02_dma_sync_runtime(DmMc02Dma *s)
{
    if (!s) {
        return;
    }

    /* The request index is derived from the restored stream registers and
     * the separately restored DMAMUX component.  Never carry a cache across
     * the component boundary; the next request rebuilds it against the
     * destination wiring and DMAMUX generation. */
    memset(s->request_stream_mask, 0, sizeof(s->request_stream_mask));
    memset(s->request_cache_seen, 0, sizeof(s->request_cache_seen));
    memset(s->request_cache_peripheral_addr, 0,
           sizeof(s->request_cache_peripheral_addr));
    s->request_cache_generation = 0;
    s->request_cache_valid = false;

    /* IRQ levels are projections of restored status/control registers and
     * destination IRQ handles.  Force the first update so a destination that
     * already had a stale level cannot retain it after load. */
    memset(s->stream_irq_level_valid, 0,
           sizeof(s->stream_irq_level_valid));
    dm_mc02_dma_update_irqs(s);
}

void dm_mc02_dmamux_reset(DmMc02Dmamux *s)
{
    if (s) {
        memset(s->regs, 0, sizeof(s->regs));
        s->generation++;
    }
}

void dm_mc02_dma_set_stream_enabled_callback(DmMc02Dma *state,
                                              DmMc02DmaStreamEnabled *callback,
                                              void *opaque)
{
    state->stream_enabled = callback;
    state->stream_enabled_opaque = opaque;
}

void dm_mc02_dma_set_dmamux_channel_offset(DmMc02Dma *state,
                                           unsigned channel_offset)
{
    state->dmamux_channel_offset = channel_offset;
    state->request_cache_valid = false;
}

void dm_mc02_dma_set_stream_irq(DmMc02Dma *state, unsigned stream,
                                qemu_irq irq)
{
    if (stream >= DMA_STREAM_COUNT) {
        return;
    }

    state->stream_irq[stream] = irq;
    state->stream_irq_level_valid[stream] = false;
    dm_mc02_dma_update_irq(state, stream);
}

void dm_mc02_dmamux_init(DmMc02Dmamux *state, Object *owner,
                         const char *name)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_dmamux_ops, state,
                          name, DM_MC02_DMAMUX_REGION_SIZE);
}

void dm_mc02_dma_subsystem_set_identity(DmMc02DmaSubsystem *state)
{
    if (!state) {
        return;
    }

    state->dma1_marker = DM_MC02_DMA_SUBSYSTEM_DMA1_MARKER;
    state->dma2_marker = DM_MC02_DMA_SUBSYSTEM_DMA2_MARKER;
    state->dmamux1_marker = DM_MC02_DMA_SUBSYSTEM_DMAMUX1_MARKER;
    state->dmamux2_marker = DM_MC02_DMA_SUBSYSTEM_DMAMUX2_MARKER;
}
