/* Reusable STM32H7 SPI data path with board-independent target callbacks. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_spi.h"
#include "qemu/timer.h"
#include "hw/irq.h"
#include "qemu/log.h"

#define SPI_CR1              0x00
#define SPI_CR2              0x04
#define SPI_CFG1             0x08
#define SPI_CFG2             0x0c
#define SPI_IER              0x10
#define SPI_IFCR             0x18
#define SPI_SR               0x14
#define SPI_TXDR             0x20
#define SPI_RXDR             0x30
#define SPI_SR_RXP           (1u << 0)
#define SPI_SR_TXP           (1u << 1)
#define SPI_SR_EOT           (1u << 3)
#define SPI_IFCR_EOTC        (1u << 3)
#define SPI_CR1_SPE          (1u << 0)
#define SPI_CR2_TSIZE_MASK   0xffffu
#define DM_MC02_SPI_DMA_BATCH_MAX 32
#define SPI_CR1_CSTART (1u << 9)
#define SPI_CR1_CSUSP (1u << 10)
#define SPI_CFG1_TXDMAEN (1u << 15)
#define SPI_CFG2_MASTER (1u << 22)
#define SPI_CFG2_LSBFRST (1u << 23)
#define SPI_CFG2_CPHA (1u << 24)
#define SPI_SR_TXTF (1u << 4)
#define SPI_SR_TXC (1u << 12)

static void spi_wire_kick(DmMc02Spi *s, uint64_t timestamp_ns);
static void spi_wire_irq(DmMc02Spi *s);
static void spi_wire_output(DmMc02Spi *s, uint64_t timestamp_ns);
static bool spi_wire_enqueue(DmMc02Spi *s, uint32_t value, unsigned size);
static uint32_t spi_wire_status(const DmMc02Spi *s);
static uint64_t spi_wire_delay(const DmMc02Spi *s, unsigned half_cycles);

static uint32_t dm_mc02_spi_dma_reg(const DmMc02Dma *dma, hwaddr offset)
{
    return ldl_le_p(dma->regs + offset);
}

static DmMc02SpiTarget *dm_mc02_spi_selected_target(DmMc02Spi *s)
{
    uint32_t mask = s->selected_mask;

    if (!mask || (mask & (mask - 1))) {
        return NULL;
    }
    for (unsigned i = 0; i < DM_MC02_SPI_MAX_TARGETS; ++i) {
        if (mask == (UINT32_C(1) << i)) {
            return &s->targets[i];
        }
    }
    return NULL;
}

static bool dm_mc02_spi_dma_request(DmMc02Spi *s,
                                    const DmMc02SpiDmaChannel *channel)
{
    bool was_active;
    bool moved;
    const DmMc02DmaEndpoint *endpoint = NULL;
    uint64_t timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!channel || !channel->dma || !channel->dmamux) {
        return false;
    }
    if (s->dma_endpoint_enabled) {
        endpoint = (channel == &s->dma_tx || channel == &s->dma_tx_alternate) ?
                   &s->tx_endpoint :
                   channel == &s->dma_rx ? &s->rx_endpoint : NULL;
        if (!endpoint) {
            return false;
        }
    }
    was_active = s->dma_request_active;
    s->dma_request_active = true;
    if (endpoint) {
        moved = dm_mc02_dma_request_endpoint(
            channel->dma, channel->dmamux, channel->request_id,
            channel->peripheral_addr, endpoint, timestamp_ns);
    } else {
        moved = dm_mc02_dma_request(channel->dma, channel->dmamux,
                                    channel->request_id,
                                    channel->peripheral_addr);
    }
    s->dma_request_active = was_active;
    return moved;
}

static bool dm_mc02_spi_dma_tx_pending(const DmMc02Spi *s)
{
    hwaddr base;
    uint32_t cr;
    uint32_t ndtr;

    if (!s->dma_tx.dma) {
        return false;
    }
    base = DM_MC02_DMA_STREAM_BASE +
           s->dma_tx.stream * DM_MC02_DMA_STREAM_STRIDE;
    cr = dm_mc02_spi_dma_reg(s->dma_tx.dma, base + DM_MC02_DMA_SxCR);
    ndtr = dm_mc02_spi_dma_reg(s->dma_tx.dma, base + DM_MC02_DMA_SxNDTR);
    return (cr & 1u) && (ndtr & 0xffffu);
}

static void dm_mc02_spi_arm_dma_tx_timer(DmMc02Spi *s, uint64_t deadline_ns)
{
    if (!s) {
        return;
    }
    s->dma_tx_next_ns = deadline_ns;
    if (s->dma_tx_timer) {
        timer_mod(s->dma_tx_timer, deadline_ns);
    }
}

static void dm_mc02_spi_dma_tx_tick(void *opaque)
{
    DmMc02Spi *s = opaque;
    unsigned batch;

    /* The callback owns the current deadline.  A new one is recorded only
     * if the DMA stream still has work after this bounded batch. */
    s->dma_tx_next_ns = 0;

    for (batch = 0; batch < MIN(DM_MC02_SPI_DMA_BATCH_MAX,
                                s->dma_batch_limit ? s->dma_batch_limit : 1);
         ++batch) {
        if (!dm_mc02_spi_dma_request(s, &s->dma_tx)) {
            break;
        }
        if (!dm_mc02_spi_dma_tx_pending(s)) {
            break;
        }
    }
    if (dm_mc02_spi_dma_tx_pending(s)) {
        uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        uint64_t deadline = now < INT64_MAX ? now + 1 : INT64_MAX;

        dm_mc02_spi_arm_dma_tx_timer(s, deadline);
    }
}

static void dm_mc02_spi_process_tx_byte(DmMc02Spi *s, uint8_t value,
                                         uint64_t timestamp_ns)
{
    DmMc02SpiTarget *target = dm_mc02_spi_selected_target(s);

    s->rx = target && target->transfer ?
            target->transfer(target->opaque, value, timestamp_ns) : 0;
    s->rx_valid = true;
    if (s->transfer_remaining) {
        s->transfer_remaining--;
    }
    if (!s->transfer_remaining) {
        s->eot = true;
    }
    /* A completed SPI data frame is the peripheral-side RX event. */
    (void)dm_mc02_spi_dma_request(s, &s->dma_rx);
}

static bool dm_mc02_spi_dma_endpoint_write(void *opaque, const uint8_t *data,
                                            unsigned size,
                                            uint64_t timestamp_ns)
{
    DmMc02Spi *s = opaque;

    if (!s || !data || size != 1) {
        return false;
    }
    dm_mc02_spi_process_tx_byte(s, data[0], timestamp_ns);
    return true;
}

static bool dm_mc02_spi_dma_endpoint_read(void *opaque, uint8_t *data,
                                           unsigned size,
                                           uint64_t timestamp_ns)
{
    DmMc02Spi *s = opaque;

    (void)timestamp_ns;
    if (!s || !data || size != 1) {
        return false;
    }
    data[0] = s->rx_valid ? s->rx : 0;
    s->rx_valid = false;
    return true;
}

static DmMc02DmaEndpointResult dm_mc02_spi_dma_endpoint_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    DmMc02Spi *s = opaque;

    (void)timestamp_ns;
    if (!s || !data || size != 1 || !s->rx_valid || s->rx_dma_reserved) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    s->rx_dma_reserved = true;
    s->rx_dma_reserved_value = s->rx;
    data[0] = s->rx_dma_reserved_value;
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void dm_mc02_spi_dma_endpoint_read_commit(void *opaque)
{
    DmMc02Spi *s = opaque;

    if (!s || !s->rx_dma_reserved) {
        return;
    }
    /* SPI RXDR is a single pending result.  The callback is synchronous, so
     * no producer can replace it between prepare and commit. */
    if (s->rx_valid && s->rx == s->rx_dma_reserved_value) {
        s->rx_valid = false;
    }
    s->rx_dma_reserved = false;
}

static void dm_mc02_spi_dma_endpoint_read_abort(void *opaque)
{
    DmMc02Spi *s = opaque;

    if (s) {
        /* prepare never consumes RXDR; abort only releases the reservation. */
        s->rx_dma_reserved = false;
    }
}

static uint64_t dm_mc02_spi_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Spi *s = opaque;

    switch (offset) {
    case SPI_CR1:
        return s->cr1;
    case SPI_CR2:
        return s->cr2;
    case SPI_CFG1:
        return s->cfg1;
    case SPI_CFG2:
        return s->cfg2;
    case SPI_IER:
        return s->ier;
    case SPI_SR:
        if (s->wire_timer) {
            return spi_wire_status(s);
        }
        return SPI_SR_TXP | (s->rx_valid ? SPI_SR_RXP : 0) |
               (s->eot ? SPI_SR_EOT : 0);
    case SPI_RXDR:
        if (s->rx_valid) {
            uint32_t value = s->wire_timer ? s->timed_rx : s->rx;

            /* A synchronous DMA reservation owns the pending result until
             * the destination write commits.  Do not let a re-entrant CPU
             * read consume it. */
            if (!s->rx_dma_reserved) {
                s->rx_valid = false;
                if (s->wire_timer) { spi_wire_irq(s); }
            }
            return value;
        }
        return 0;
    default:
        (void)size;
        return 0;
    }
}

static void dm_mc02_spi_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Spi *s = opaque;

    if (s->wire_timer) {
        uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        switch (offset) {
        case SPI_CR1: {
            uint32_t previous = s->cr1;
            s->cr1 = value;
            if (!(s->cr1 & SPI_CR1_SPE)) {
                timer_del(s->wire_timer);
                s->wire_next_ns = 0;
                s->shifting = false;
                s->tx_head = s->tx_count = 0;
                s->cr1 &= ~SPI_CR1_CSTART;
                s->eot = false;
                s->tx_complete = false;
            } else if ((s->cr1 & SPI_CR1_CSTART) &&
                       !(previous & SPI_CR1_CSTART)) {
                s->transfer_remaining = s->cr2 & SPI_CR2_TSIZE_MASK;
                s->eot = false;
                s->tx_complete = false;
                s->wire_available_ns = now + spi_wire_delay(s, 2 * (s->cfg2 & 15));
            }
            if (s->cr1 & SPI_CR1_CSUSP) {
                s->cr1 &= ~(SPI_CR1_CSTART | SPI_CR1_CSUSP);
            }
            spi_wire_output(s, now);
            break;
        }
        case SPI_CR2:
            s->cr2 = value & SPI_CR2_TSIZE_MASK;
            break;
        case SPI_CFG1:
            if (!(s->cr1 & SPI_CR1_SPE)) {
                s->cfg1 = value;
            } else {
                s->cfg1 = (s->cfg1 & ~0xc000u) | (value & 0xc000u);
            }
            break;
        case SPI_CFG2:
            if (!(s->cr1 & SPI_CR1_SPE)) { s->cfg2 = value; }
            break;
        case SPI_IER:
            s->ier = value;
            break;
        case SPI_TXDR:
            if (!spi_wire_enqueue(s, value, size)) {
                qemu_log_mask(LOG_GUEST_ERROR, "SPI TXDR write exceeds FIFO capacity or data width\n");
            }
            break;
        case SPI_IFCR:
            if (value & SPI_IFCR_EOTC) { s->eot = false; }
            if (value & SPI_SR_TXTF) { s->tx_complete = false; }
            break;
        default:
            return;
        }
        spi_wire_kick(s, now);
        spi_wire_irq(s);
        return;
    }
    switch (offset) {
    case SPI_CR1:
        if ((value & SPI_CR1_SPE) && !(s->cr1 & SPI_CR1_SPE)) {
            /* CR2.TSIZE is programmed immediately before CSTART by the HAL. */
            s->eot = false;
            s->transfer_remaining = s->cr2 & SPI_CR2_TSIZE_MASK;
        }
        s->cr1 = value;
        break;
    case SPI_CR2:
        s->cr2 = value;
        break;
    case SPI_CFG1:
        s->cfg1 = value;
        break;
    case SPI_CFG2:
        s->cfg2 = value;
        break;
    case SPI_TXDR:
        /* A guest write to TXDR is the SPI TX-ready event in this narrow
         * model.  DMA itself is guarded so its endpoint callback cannot kick
         * a second TX request recursively. */
        if (!s->dma_request_active &&
            dm_mc02_spi_dma_request(s, &s->dma_tx)) {
            if (dm_mc02_spi_dma_tx_pending(s)) {
                uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
                uint64_t deadline = now < INT64_MAX ? now + 1 : INT64_MAX;

                dm_mc02_spi_arm_dma_tx_timer(s, deadline);
            }
            break;
        }
        /* The wire device sees one byte per data frame, in little-endian
         * order for this byte-sized setup. */
        {
            unsigned count = s->transfer_remaining ?
                             MIN((uint32_t)size, s->transfer_remaining) : 1;

            for (unsigned i = 0; i < count; ++i) {
                dm_mc02_spi_process_tx_byte(
                    s, (value >> (i * 8)) & 0xff,
                    qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
            }
        }
        break;
    case SPI_IFCR:
        if (value & SPI_IFCR_EOTC) {
            s->eot = false;
        }
        s->rx_valid = false;
        s->rx_dma_reserved = false;
        break;
    default:
        (void)size;
        break;
    }
}

static const MemoryRegionOps dm_mc02_spi_ops = {
    .read = dm_mc02_spi_read,
    .write = dm_mc02_spi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_spi_state_init(DmMc02Spi *spi)
{
    memset(spi, 0, sizeof(*spi));
    spi->dma_endpoint_enabled = true;
    spi->dma_batch_limit = DM_MC02_SPI_DMA_BATCH_MAX;
    spi->tx_endpoint.write = dm_mc02_spi_dma_endpoint_write;
    spi->tx_endpoint.opaque = spi;
    spi->rx_endpoint.read = dm_mc02_spi_dma_endpoint_read;
    spi->rx_endpoint.read_prepare =
        dm_mc02_spi_dma_endpoint_read_prepare;
    spi->rx_endpoint.read_commit = dm_mc02_spi_dma_endpoint_read_commit;
    spi->rx_endpoint.read_abort = dm_mc02_spi_dma_endpoint_read_abort;
    spi->rx_endpoint.opaque = spi;
}

void dm_mc02_spi_init(DmMc02Spi *spi, Object *owner, const char *name,
                      bool dma_timer)
{
    memory_region_init_io(&spi->iomem, owner, &dm_mc02_spi_ops, spi, name,
                          0x400);
    if (dma_timer) {
        spi->dma_tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                         dm_mc02_spi_dma_tx_tick, spi);
    }
}

void dm_mc02_spi_cleanup(DmMc02Spi *spi)
{
    if (!spi) {
        return;
    }
    timer_free(spi->dma_tx_timer);
    timer_free(spi->wire_timer);
    spi->dma_tx_timer = spi->wire_timer = NULL;
}

void dm_mc02_spi_reset(DmMc02Spi *spi)
{
    if (!spi) {
        return;
    }
    dm_mc02_spi_select_mask(spi, 0);
    spi->cr1 = 0;
    spi->cr2 = 0;
    spi->cfg1 = spi->wire_timer ? 0x00070007 : 0;
    spi->cfg2 = 0;
    spi->transfer_remaining = 0;
    spi->selected_mask = 0;
    spi->rx = 0;
    spi->rx_valid = false;
    spi->rx_dma_reserved = false;
    spi->rx_dma_reserved_value = 0;
    spi->eot = false;
    spi->dma_tx_next_ns = 0;
    spi->dma_request_active = false;
    spi->ier = 0;
    spi->tx_count = spi->tx_head = 0;
    spi->shifting = spi->mosi_level = spi->tx_complete = false;
    spi->wire_next_ns = 0;
    spi->wire_available_ns = 0;
    if (spi->wire_timer) {
        timer_del(spi->wire_timer);
        spi_wire_output(spi, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        spi_wire_irq(spi);
    }
    if (spi->dma_tx_timer) {
        timer_del(spi->dma_tx_timer);
    }
}

void dm_mc02_spi_sync_runtime(DmMc02Spi *spi)
{
    uint64_t now;

    if (!spi) {
        return;
    }

    /* Recursive request protection is an execution-local guard, not guest
     * state.  Never let a snapshot restore leave the peripheral wedged. */
    spi->dma_request_active = false;
    if (!spi->dma_tx_timer) {
        return;
    }

    timer_del(spi->dma_tx_timer);
    if (!spi->dma_tx_next_ns) {
        return;
    }

    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (spi->dma_tx_next_ns < now) {
        spi->dma_tx_next_ns = now;
    }
    timer_mod(spi->dma_tx_timer, spi->dma_tx_next_ns);
}

void dm_mc02_spi_set_dma_channels(DmMc02Spi *spi,
                                  const DmMc02SpiDmaChannel *tx,
                                  const DmMc02SpiDmaChannel *rx)
{
    if (!spi) {
        return;
    }
    spi->dma_tx = tx ? *tx : (DmMc02SpiDmaChannel) { 0 };
    spi->dma_rx = rx ? *rx : (DmMc02SpiDmaChannel) { 0 };
}

void dm_mc02_spi_set_target(DmMc02Spi *spi, unsigned index,
                            const DmMc02SpiTarget *target)
{
    if (!spi || index >= DM_MC02_SPI_MAX_TARGETS) {
        return;
    }
    spi->targets[index] = target ? *target : (DmMc02SpiTarget) { 0 };
}

void dm_mc02_spi_select_mask(DmMc02Spi *spi, uint32_t selected_mask)
{
    if (!spi || spi->selected_mask == selected_mask) {
        return;
    }
    spi->selected_mask = selected_mask;
    spi->rx_valid = false;
    spi->rx_dma_reserved = false;
    for (unsigned i = 0; i < DM_MC02_SPI_MAX_TARGETS; ++i) {
        DmMc02SpiTarget *target = &spi->targets[i];

        if (target->select) {
            target->select(target->opaque,
                           (selected_mask & (UINT32_C(1) << i)) != 0,
                           selected_mask);
        }
    }
}

void dm_mc02_spi_restore_selected_mask(DmMc02Spi *spi,
                                       uint32_t selected_mask)
{
    if (spi) {
        /* This is intentionally not expressed through
         * dm_mc02_spi_select_mask(): restoring a snapshot must not call a
         * BMI088 target's transaction-reset callback while its framer state
         * is still being loaded. */
        spi->selected_mask = selected_mask;
    }
}

void dm_mc02_spi_set_dma_endpoint(DmMc02Spi *spi, bool enabled)
{
    if (spi) {
        spi->dma_endpoint_enabled = enabled;
    }
}

void dm_mc02_spi_dma_rx_stream_enabled(DmMc02Spi *spi)
{
    if (!spi || !spi->rx_valid || !spi->dma_rx.dma ||
        !spi->dma_rx.dmamux) {
        return;
    }
    (void)dm_mc02_spi_dma_request(spi, &spi->dma_rx);
}

static unsigned spi_wire_bits(const DmMc02Spi *s)
{
    return (s->cfg1 & 31) + 1;
}

static unsigned spi_wire_slot(const DmMc02Spi *s)
{
    unsigned bits = spi_wire_bits(s);
    return bits <= 8 ? 1 : bits <= 16 ? 2 : 4;
}

static uint64_t spi_wire_delay(const DmMc02Spi *s, unsigned half_cycles)
{
    unsigned divider = 2u << ((s->cfg1 >> 28) & 7);
    __uint128_t numerator = (__uint128_t)half_cycles * divider * 1000000000U;
    uint64_t denominator = s->kernel_hz * 2;

    return denominator ? (numerator + denominator - 1) / denominator : 0;
}

static uint32_t spi_wire_status(const DmMc02Spi *s)
{
    unsigned capacity = 16 / spi_wire_slot(s);
    unsigned threshold = ((s->cfg1 >> 5) & 15) + 1;
    bool txp = (s->cr1 & SPI_CR1_SPE) &&
               s->tx_count + threshold <= capacity;

    return (txp ? SPI_SR_TXP : 0) | (s->rx_valid ? SPI_SR_RXP : 0) |
           (s->eot ? SPI_SR_EOT : 0) | (s->tx_complete ? SPI_SR_TXTF : 0) |
           (!s->shifting && !s->tx_count ? SPI_SR_TXC : 0);
}

static void spi_wire_irq(DmMc02Spi *s)
{
    if (s->irq) {
        qemu_set_irq(s->irq, (spi_wire_status(s) & s->ier & 0x1f) != 0);
    }
}

static void spi_wire_output(DmMc02Spi *s, uint64_t timestamp_ns)
{
    bool driven = (s->cfg2 & SPI_CFG2_MASTER) &&
                  (s->cr1 & SPI_CR1_SPE || s->cfg2 & (1u << 31)) &&
                  !(s->cfg2 & (1u << 15)) && /* IOSWP */
                  ((s->cfg2 >> 17) & 3) != 2;
    if (s->mosi) {
        s->mosi(s->mosi_opaque, timestamp_ns, driven, s->mosi_level);
    }
}

static bool spi_wire_enqueue(DmMc02Spi *s, uint32_t value, unsigned size)
{
    unsigned slot = spi_wire_slot(s);
    unsigned count = size / slot;

    if (spi_wire_bits(s) < 4 || !count || size % slot ||
        s->tx_count + count > 16 / slot) {
        return false;
    }
    for (unsigned i = 0; i < count; ++i) {
        s->tx_fifo[(s->tx_head + s->tx_count++) % 16] = value >> (i * slot * 8);
    }
    return true;
}

static DmMc02DmaEndpointResult spi_wire_dma_write(
    void *opaque, const uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    DmMc02Spi *s = opaque;
    uint32_t value;

    if (size != 1 && size != 2 && size != 4) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    if (size < spi_wire_slot(s) || spi_wire_bits(s) < 4) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    value = size == 1 ? data[0] : size == 2 ? lduw_le_p(data) : ldl_le_p(data);
    return spi_wire_enqueue(s, value, size) ? DM_MC02_DMA_ENDPOINT_ACCEPTED :
                                            DM_MC02_DMA_ENDPOINT_RETRY;
}

static void spi_wire_fill(DmMc02Spi *s, uint64_t timestamp_ns)
{
    const DmMc02SpiDmaChannel *channels[] = { &s->dma_tx, &s->dma_tx_alternate };

    if (!(s->cfg1 & SPI_CFG1_TXDMAEN) || s->dma_request_active) {
        return;
    }
    s->dma_request_active = true;
    for (unsigned beat = 0; beat < 16 && (spi_wire_status(s) & SPI_SR_TXP); ++beat) {
        bool moved = false;
        if ((s->cr2 & SPI_CR2_TSIZE_MASK) &&
            s->tx_count + s->shifting >= s->transfer_remaining) {
            break;
        }
        for (unsigned i = 0; i < G_N_ELEMENTS(channels); ++i) {
            const DmMc02SpiDmaChannel *c = channels[i];
            if (c->dma && c->dmamux && dm_mc02_dma_request_endpoint(
                    c->dma, c->dmamux, c->request_id, c->peripheral_addr,
                    &s->tx_endpoint, timestamp_ns)) {
                moved = true;
                break;
            }
        }
        if (!moved) { break; }
    }
    s->dma_request_active = false;
}

static void spi_wire_kick(DmMc02Spi *s, uint64_t timestamp_ns)
{
    if (!s->wire_timer || !s->kernel_hz ||
        (s->cr1 & (SPI_CR1_SPE | SPI_CR1_CSTART)) !=
                  (SPI_CR1_SPE | SPI_CR1_CSTART) ||
        !(s->cfg2 & SPI_CFG2_MASTER) || (s->cfg2 & (7u << 19)) ||
        spi_wire_bits(s) < 4) {
        return;
    }
    spi_wire_fill(s, timestamp_ns);
    if (!s->shifting && s->tx_count) {
        s->shift_value = s->tx_fifo[s->tx_head];
        s->tx_head = (s->tx_head + 1) % 16;
        s->tx_count--;
        s->shifting = true;
        s->shift_bit = s->shift_origin_bit = 0;
        s->wire_origin_ns = MAX(timestamp_ns, s->wire_available_ns);
        s->wire_next_ns = s->wire_origin_ns +
            spi_wire_delay(s, !!(s->cfg2 & SPI_CFG2_CPHA));
        timer_mod(s->wire_timer, s->wire_next_ns);
        spi_wire_fill(s, timestamp_ns);
    }
    if ((s->cr2 & SPI_CR2_TSIZE_MASK) &&
        s->tx_count + s->shifting >= s->transfer_remaining) {
        s->tx_complete = true;
    }
    spi_wire_irq(s);
}

static void spi_wire_tick(void *opaque)
{
    DmMc02Spi *s = opaque;
    uint64_t timestamp_ns = s->wire_next_ns;
    unsigned bits = spi_wire_bits(s);

    if (!s->shifting) { return; }
    if (s->shift_bit == bits) {
        DmMc02SpiTarget *target = dm_mc02_spi_selected_target(s);
        s->shifting = false;
        s->wire_next_ns = 0;
        s->timed_rx = target && target->transfer && bits <= 8 ?
            target->transfer(target->opaque, s->shift_value, timestamp_ns) : 0;
        s->rx = s->timed_rx;
        if (((s->cfg2 >> 17) & 3) == 0) { s->rx_valid = true; }
        if (s->transfer_remaining && --s->transfer_remaining == 0) {
            s->eot = true;
            s->cr1 &= ~SPI_CR1_CSTART;
        }
        s->wire_available_ns = timestamp_ns + spi_wire_delay(s, 2 * ((s->cfg2 >> 4) & 15));
        spi_wire_kick(s, timestamp_ns);
    } else {
        unsigned position = s->cfg2 & SPI_CFG2_LSBFRST ? s->shift_bit :
                            bits - 1 - s->shift_bit;
        s->mosi_level = (s->shift_value >> position) & 1;
        spi_wire_output(s, timestamp_ns);
        s->shift_bit++;
        s->wire_next_ns = s->wire_origin_ns + spi_wire_delay(s,
            2 * (s->shift_bit - s->shift_origin_bit) +
            (s->shift_bit < bits && (s->cfg2 & SPI_CFG2_CPHA) ? 1 : 0));
        timer_mod(s->wire_timer, s->wire_next_ns);
    }
    spi_wire_irq(s);
}

void dm_mc02_spi_enable_timing(DmMc02Spi *s)
{
    if (!s->wire_timer) {
        s->wire_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, spi_wire_tick, s);
        s->tx_endpoint.write_ex = spi_wire_dma_write;
        s->cfg1 = 0x00070007;
    }
}

void dm_mc02_spi_set_clock_hz(DmMc02Spi *s, uint64_t hz)
{
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (hz == s->kernel_hz) { return; }
    s->kernel_hz = hz;
    if (!s->wire_timer) { return; }
    if (!hz) {
        timer_del(s->wire_timer);
        s->wire_next_ns = 0;
    } else if (s->shifting) {
        s->wire_origin_ns = now;
        s->shift_origin_bit = s->shift_bit;
        s->wire_next_ns = now + spi_wire_delay(s, 2);
        /* Resume the outstanding bit after one new clock period. */
        s->wire_origin_ns = s->wire_next_ns;
        timer_mod(s->wire_timer, s->wire_next_ns);
    } else {
        spi_wire_kick(s, now);
    }
}

void dm_mc02_spi_set_mosi(DmMc02Spi *s, DmDigitalOutput *output, void *opaque)
{
    s->mosi = output;
    s->mosi_opaque = opaque;
    spi_wire_output(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

void dm_mc02_spi_set_irq(DmMc02Spi *s, qemu_irq irq)
{
    s->irq = irq;
    spi_wire_irq(s);
}

void dm_mc02_spi_set_alternate_tx_dma(DmMc02Spi *s, const DmMc02SpiDmaChannel *c)
{
    s->dma_tx_alternate = c ? *c : (DmMc02SpiDmaChannel) { 0 };
}

void dm_mc02_spi_dma_tx_stream_enabled(DmMc02Spi *s)
{
    spi_wire_kick(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}
