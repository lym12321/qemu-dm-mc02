/*
 * Minimal Bosch M_CAN/FDCAN model.
 *
 * The register model is intentionally small, but the TX/RX FIFO path uses
 * the real M_CAN message-element layout. An optional chardev carries one
 * fixed 84-byte frame:
 *   can_id:u32 flags:u32 dlc:u8 reserved[3] time_ns:u64 data[64]
 *
 * flags bit 0 is extended ID, bit 1 RTR, bit 2 CAN-FD and bit 3 BRS.
 * This is a host integration transport, not a SocketCAN ABI.
 */
#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "qemu/host-utils.h"
#include "qemu/timer.h"
#include "qemu/error-report.h"
#include "hw/arm/dm_mc02_fdcan.h"

#define FDCAN_CCCR  0x018
#define FDCAN_DBTP  0x00c
#define FDCAN_NBTP  0x01c
#define FDCAN_ECR   0x040
#define FDCAN_PSR   0x044
#define FDCAN_IR    0x050
#define FDCAN_IE    0x054
#define FDCAN_ILS   0x058
#define FDCAN_ILE   0x05c
#define FDCAN_RXF0C 0x0a0
#define FDCAN_RXF0S 0x0a4
#define FDCAN_RXF0A 0x0a8
#define FDCAN_RXBC  0x0ac
#define FDCAN_RXF1C 0x0b0
#define FDCAN_RXF1S 0x0b4
#define FDCAN_RXF1A 0x0b8
#define FDCAN_NDAT1 0x098
#define FDCAN_NDAT2 0x09c
#define FDCAN_HPMS  0x094
#define FDCAN_XIDFC 0x088
#define FDCAN_XIDAM 0x090
#define FDCAN_TXBC  0x0c0
#define FDCAN_TXBRP 0x0cc
#define FDCAN_TXESC 0x0c8
#define FDCAN_RXESC 0x0bc
#define FDCAN_GFC   0x080
#define FDCAN_SIDFC 0x084
#define FDCAN_TXFQS 0x0c4
#define FDCAN_TXBAR 0x0d0
#define FDCAN_TXBTO 0x0d8

#define FDCAN_CCCR_INIT (1u << 0)
#define FDCAN_CCCR_CCE  (1u << 1)
#define FDCAN_IR_RF0N   (1u << 0)
#define FDCAN_IR_RF0F   (1u << 2)
#define FDCAN_IR_RF1N   (1u << 4)
#define FDCAN_IR_RF1F   (1u << 6)
#define FDCAN_IR_HPM    (1u << 8)
#define FDCAN_IR_DRX    (1u << 19)
#define FDCAN_IR_TC     (1u << 9)
#define FDCAN_IR_BO     (1u << 25)

#define FDCAN_PSR_LEC_MASK 0x7u
#define FDCAN_PSR_BO       (1u << 7)

#define FDCAN_GFC_RRFE  (1u << 0)
#define FDCAN_GFC_RRFS  (1u << 1)
#define FDCAN_GFC_ANFE  (3u << 2)
#define FDCAN_GFC_ANFS  (3u << 4)

/* Standard filter element encoding from Bosch M_CAN.  The STM32H7 HAL
 * writes one 32-bit element per standard filter into message RAM. */
#define FDCAN_SIDFC_FLSSA_MASK 0x0000fffcu
#define FDCAN_SIDFC_LSS_SHIFT  16
#define FDCAN_SIDFC_LSS_MASK   0x00ff0000u
#define FDCAN_SF_ID1_MASK       0x07ff0000u
#define FDCAN_SF_ID2_MASK       0x000007ffu
#define FDCAN_SF_SFT_SHIFT      30
#define FDCAN_SF_SFEC_SHIFT     27
#define FDCAN_SF_SFEC_MASK      0x38000000u
#define FDCAN_SFEC_DISABLE      0u
#define FDCAN_SFEC_FIFO0        1u
#define FDCAN_SFEC_FIFO0_HP     5u
#define FDCAN_SFEC_FIFO1        2u
#define FDCAN_SFEC_FIFO1_HP     6u
#define FDCAN_SFEC_HP           4u
#define FDCAN_SFEC_RX_BUFFER    7u

#define FDCAN_XIDFC_FLSSA_MASK  0x0000fffcu
#define FDCAN_XIDFC_LSE_SHIFT   16
#define FDCAN_XIDFC_LSE_MASK    0x00ff0000u
#define FDCAN_EF_ID_MASK        0x1fffffffu
#define FDCAN_EF_EFT_SHIFT      30
#define FDCAN_EF_EFEC_SHIFT     29
#define FDCAN_EF_EFEC_MASK      (7u << FDCAN_EF_EFEC_SHIFT)
#define FDCAN_EFEC_HP           4u
#define FDCAN_EFEC_RX_BUFFER    7u
#define FDCAN_RX_BUFFER_INDEX_MASK 0x3fu

enum DmMc02FdcanRxDestination {
    FDCAN_RX_DROP,
    /* A priority-only filter raises HPMS/HPM but does not store a FIFO
     * element.  Keep it distinct from DROP so the filter hit remains
     * observable to firmware. */
    FDCAN_RX_HIGH_PRIORITY,
    FDCAN_RX_FIFO0,
    FDCAN_RX_FIFO1,
    FDCAN_RX_BUFFER,
};

static uint32_t fdcan_load(const DmMc02Fdcan *s, hwaddr offset,
                           unsigned size);
static unsigned dlc_to_length(unsigned dlc);

static bool fdcan_in_init(const DmMc02Fdcan *s)
{
    return fdcan_load(s, FDCAN_CCCR, 4) & FDCAN_CCCR_INIT;
}

static void fdcan_flush_tx(DmMc02Fdcan *s);

static void fdcan_tx_retry(void *opaque)
{
    DmMc02Fdcan *s = opaque;

    s->tx_next_ns = 0;
    fdcan_flush_tx(s);
}

static void fdcan_schedule_retry(DmMc02Fdcan *s)
{
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    s->tx_next_ns = now < INT64_MAX - 1000000 ? now + 1000000 : INT64_MAX;
    if (s->tx_timer) {
        timer_mod(s->tx_timer, s->tx_next_ns);
    }
}

static void fdcan_flush_tx(DmMc02Fdcan *s)
{
    while (s->tx_queue_count != 0) {
        unsigned slot = s->tx_queue_head;
        unsigned offset = s->tx_queue_offset[slot];
        int remaining = DM_MC02_CAN_WIRE_SIZE - offset;
        int written;

        if (!s->chr_enabled || !qemu_chr_fe_backend_open(&s->chr)) {
            return;
        }
        written = qemu_chr_fe_write(&s->chr, s->tx_queue[slot] + offset,
                                    remaining);
        if (written <= 0) {
            fdcan_schedule_retry(s);
            return;
        }
        if (written < remaining) {
            s->tx_short_writes++;
        }
        s->tx_queue_offset[slot] += written;
        if (s->tx_queue_offset[slot] < DM_MC02_CAN_WIRE_SIZE) {
            fdcan_schedule_retry(s);
            return;
        }
        s->tx_queue_head = (s->tx_queue_head + 1) %
                           DM_MC02_FDCAN_TX_QUEUE_SIZE;
        s->tx_queue_count--;
    }
    if (s->tx_timer) {
        timer_del(s->tx_timer);
    }
    s->tx_next_ns = 0;
}

static void fdcan_update_irq(DmMc02Fdcan *s)
{
    uint32_t pending = fdcan_load(s, FDCAN_IR, 4) &
                       fdcan_load(s, FDCAN_IE, 4);
    uint32_t line_select = fdcan_load(s, FDCAN_ILS, 4);
    uint32_t line_enable = fdcan_load(s, FDCAN_ILE, 4);

    for (unsigned line = 0; line < 2; ++line) {
        uint32_t line_pending = line ? pending & line_select :
                                       pending & ~line_select;
        bool asserted = line_pending != 0 && (line_enable & (1u << line));

        if (s->irq[line] && (!s->irq_level_valid[line] ||
                             s->irq_level[line] != asserted)) {
            s->irq_level[line] = asserted;
            s->irq_level_valid[line] = true;
            qemu_set_irq(s->irq[line], asserted);
        }
    }
}

static uint32_t fdcan_load(const DmMc02Fdcan *s, hwaddr offset,
                           unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)s->regs[offset + i] << (i * 8);
    }
    return value;
}

static void fdcan_store(DmMc02Fdcan *s, hwaddr offset, uint64_t value,
                        unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        s->regs[offset + i] = value >> (i * 8);
    }
}

static void fdcan_finish_tx_slots(DmMc02Fdcan *s, uint32_t slots)
{
    if (!slots) {
        return;
    }

    s->tx_pending_mask &= ~slots;
    fdcan_store(s, FDCAN_TXBTO,
                fdcan_load(s, FDCAN_TXBTO, 4) | slots, 4);
    fdcan_store(s, FDCAN_IR, fdcan_load(s, FDCAN_IR, 4) | FDCAN_IR_TC, 4);
}

static void fdcan_tx_complete(DmMc02Fdcan *s,
                              const DmMc02CanFrame *frame,
                              bool acknowledged)
{
    uint32_t index = frame->tx_index & 31u;
    uint32_t slot = UINT32_C(1) << index;

    fdcan_finish_tx_slots(s, slot);
    if (!acknowledged) {
        uint32_t ecr = fdcan_load(s, FDCAN_ECR, 4);
        uint32_t tec = ecr & 0xffu;
        uint32_t psr = fdcan_load(s, FDCAN_PSR, 4);
        uint32_t next_tec;

        /* A transmit error normally increases TEC by eight.  This is a
         * frame-level approximation; individual CAN error frames and retry
         * bits are intentionally outside this model. */
        if (s->bus_off) {
            s->tx_no_ack++;
            goto error_done;
        }
        next_tec = tec + 8;
        tec = MIN(next_tec, 255u);
        fdcan_store(s, FDCAN_ECR, (ecr & ~0xffu) | tec, 4);
        /* LEC=3 is the M_CAN acknowledgement-error code.  This is only a
         * coarse diagnostic; no bit-level error/recovery model is implied. */
        fdcan_store(s, FDCAN_PSR, (psr & ~FDCAN_PSR_LEC_MASK) | 3u, 4);
        s->tx_no_ack++;

        /* CAN enters bus-off when the attempted error takes TEC beyond 255.
         * ECR exposes the saturated value 255; BO is the state that prevents
         * subsequent participation in the configured CAN bus. */
        if (next_tec >= 256u) {
            uint32_t pending = s->tx_pending_mask;
            uint32_t cccr;

            s->bus_off = true;
            psr = fdcan_load(s, FDCAN_PSR, 4) | FDCAN_PSR_BO;
            fdcan_store(s, FDCAN_PSR, psr, 4);
            fdcan_store(s, FDCAN_IR,
                        fdcan_load(s, FDCAN_IR, 4) | FDCAN_IR_BO, 4);
            cccr = fdcan_load(s, FDCAN_CCCR, 4) |
                   FDCAN_CCCR_INIT | FDCAN_CCCR_CCE;
            fdcan_store(s, FDCAN_CCCR, cccr, 4);

            fdcan_finish_tx_slots(s, pending);
            s->tx_fifo_get = s->tx_fifo_put;
        }
error_done:
        ;
    } else if (!s->bus_off) {
        /* Successful transmission lowers TEC by one in the normal error
         * active/passive state. */
        uint32_t ecr = fdcan_load(s, FDCAN_ECR, 4);
        uint32_t tec = ecr & 0xffu;

        if (tec) {
            fdcan_store(s, FDCAN_ECR, (ecr & ~0xffu) | (tec - 1), 4);
        }
    }
    fdcan_update_irq(s);
    if (index < s->tx_fifo_size) {
        s->tx_fifo_get = (index + 1) % s->tx_fifo_size;
    }
}

static unsigned dlc_to_length(unsigned dlc)
{
    static const unsigned lengths[16] = {
        0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64,
    };

    return lengths[dlc & 0xf];
}

static bool fdcan_wire_frame_valid(const uint8_t *wire)
{
    uint32_t can_id;
    uint32_t flags;
    unsigned dlc;

    /* Keep the fixed-width host wire strict.  Without this check a malformed
     * peer could silently turn a reserved field or an invalid classic DLC
     * into a different valid M_CAN frame. */
    if (wire[9] || wire[10] || wire[11]) {
        return false;
    }
    can_id = ldl_le_p(wire + 0);
    flags = ldl_le_p(wire + 4);
    if (wire[8] & 0xf0) {
        return false;
    }
    dlc = wire[8] & 0xf;
    if (flags & ~CAN_FLAG_MASK) {
        return false;
    }
    if ((flags & CAN_FLAG_EXTENDED) ? can_id > 0x1fffffffu :
                                      can_id > 0x7ffu) {
        return false;
    }
    if (!(flags & CAN_FLAG_FD) && dlc > 8) {
        return false;
    }
    /* CAN-FD has no remote-transmission-request frame type. */
    if ((flags & CAN_FLAG_FD) && (flags & CAN_FLAG_RTR)) {
        return false;
    }
    if (!(flags & CAN_FLAG_FD) && (flags & CAN_FLAG_BRS)) {
        return false;
    }
    return true;
}

static unsigned element_bytes(uint32_t esc)
{
    /* Two header words precede the configured data payload. */
    static const unsigned lengths[8] = { 16, 20, 24, 28, 32, 40, 56, 72 };

    return lengths[esc & 7];
}

static bool msg_ram_write(DmMc02Fdcan *s, size_t offset,
                          const uint8_t *data, size_t size)
{
    if (!s->msg_ram || offset > s->msg_ram_size ||
        size > s->msg_ram_size - offset) {
        return false;
    }
    memcpy(s->msg_ram + offset, data, size);
    return true;
}

static uint32_t msg_ram_read32(const DmMc02Fdcan *s, size_t offset)
{
    if (!s->msg_ram || offset > s->msg_ram_size ||
        sizeof(uint32_t) > s->msg_ram_size - offset) {
        return 0;
    }
    return ldl_le_p(s->msg_ram + offset);
}

/* Apply the M_CAN acceptance path used by the board.  Standard and extended
 * filters can route to either FIFO; global non-matching and remote-frame
 * policy must still be honored so rejected traffic never consumes RX FIFO
 * space or raises RF0N/RF1N. */
static enum DmMc02FdcanRxDestination fdcan_global_destination(uint32_t gfc,
                                                               bool extended)
{
    unsigned value = (gfc >> (extended ? 2 : 4)) & 3;

    return value == 0 ? FDCAN_RX_FIFO0 :
           value == 1 ? FDCAN_RX_FIFO1 : FDCAN_RX_DROP;
}

static enum DmMc02FdcanRxDestination fdcan_filter_destination(unsigned efec)
{
    return efec == FDCAN_SFEC_FIFO0 || efec == FDCAN_SFEC_FIFO0_HP ?
               FDCAN_RX_FIFO0 :
           efec == FDCAN_SFEC_FIFO1 || efec == FDCAN_SFEC_FIFO1_HP ?
               FDCAN_RX_FIFO1 : FDCAN_RX_DROP;
}

static void fdcan_set_hpms(DmMc02Fdcan *s, bool extended,
                           uint8_t filter_index, unsigned storage,
                           unsigned message_index)
{
    uint32_t hpms = (message_index & 0x3f) |
                    ((storage & 3u) << 6) |
                    ((uint32_t)filter_index << 8) |
                    (extended ? (1u << 15) : 0);

    fdcan_store(s, FDCAN_HPMS, hpms, 4);
    fdcan_store(s, FDCAN_IR, fdcan_load(s, FDCAN_IR, 4) |
                FDCAN_IR_HPM, 4);
}

static enum DmMc02FdcanRxDestination fdcan_accept_rx(const DmMc02Fdcan *s,
                                                     const DmMc02CanFrame *frame,
                                                     uint8_t *filter_index,
                                                     bool *nonmatching,
                                                     unsigned *buffer_index,
                                                     bool *high_priority)
{
    uint32_t gfc = fdcan_load(s, FDCAN_GFC, 4);
    bool extended = frame->flags & CAN_FLAG_EXTENDED;

    *filter_index = 0x7f;
    *nonmatching = true;
    *buffer_index = 0;
    *high_priority = false;

    if (extended) {
        if ((frame->flags & CAN_FLAG_RTR) && (gfc & FDCAN_GFC_RRFE)) {
            return FDCAN_RX_DROP;
        }

        uint32_t xidfc = fdcan_load(s, FDCAN_XIDFC, 4);
        unsigned count = (xidfc & FDCAN_XIDFC_LSE_MASK) >>
                         FDCAN_XIDFC_LSE_SHIFT;
        size_t offset = (xidfc & FDCAN_XIDFC_FLSSA_MASK) * 4u;

        count = MIN(count, s->msg_ram_size / (2 * sizeof(uint32_t)));
        for (unsigned index = 0; index < count; ++index, offset += 8) {
            uint32_t element1;
            uint32_t element2;
            uint32_t efec;
            uint32_t eft;
            bool match;

            if (offset > s->msg_ram_size - 2 * sizeof(uint32_t)) {
                break;
            }
            element1 = msg_ram_read32(s, offset);
            element2 = msg_ram_read32(s, offset + 4);
            efec = (element1 & FDCAN_EF_EFEC_MASK) >> FDCAN_EF_EFEC_SHIFT;
            if (efec == FDCAN_SFEC_DISABLE) {
                continue;
            }
            if (efec == FDCAN_EFEC_RX_BUFFER) {
                /* In Rx-buffer mode EFID1 is the exact identifier and
                 * EFID2[5:0] selects the destination buffer.  EFID2[10:9]
                 * selects the optional debug-message destinations; only the
                 * normal dedicated-buffer destination is modelled here. */
                unsigned index_buffer = element2 & FDCAN_RX_BUFFER_INDEX_MASK;
                unsigned target = (element2 >> 9) & 3;
                uint32_t effective_id = frame->can_id &
                    (fdcan_load(s, FDCAN_XIDAM, 4) & FDCAN_EF_ID_MASK);
                bool buffer_match = target == 0 && effective_id ==
                    (element1 & FDCAN_EF_ID_MASK);
                uint32_t ndat = fdcan_load(s, index_buffer < 32 ?
                                            FDCAN_NDAT1 : FDCAN_NDAT2, 4);

                if (buffer_match) {
                    /* A matching dedicated-buffer filter has consumed the
                     * acceptance decision even when its buffer is still
                     * occupied.  M_CAN drops that frame; it must not fall
                     * through to GFC and appear in an unrelated FIFO. */
                    if (!(ndat & (1u << (index_buffer % 32)))) {
                        *filter_index = index;
                        *nonmatching = false;
                        *buffer_index = index_buffer;
                        return FDCAN_RX_BUFFER;
                    }
                    return FDCAN_RX_DROP;
                }
                continue;
            }
            eft = element2 >> FDCAN_EF_EFT_SHIFT;
            switch (eft & 3) {
            case 0: /* range */
                match = frame->can_id >= (element1 & FDCAN_EF_ID_MASK) &&
                        frame->can_id <= (element2 & FDCAN_EF_ID_MASK);
                break;
            case 1: /* dual ID */
                match = frame->can_id == (element1 & FDCAN_EF_ID_MASK) ||
                        frame->can_id == (element2 & FDCAN_EF_ID_MASK);
                break;
            case 2: /* classic mask */
                match = (frame->can_id & (element2 & FDCAN_EF_ID_MASK)) ==
                        ((element1 & FDCAN_EF_ID_MASK) &
                         (element2 & FDCAN_EF_ID_MASK));
                break;
            default:
                match = false;
                break;
            }
            if (match) {
                *filter_index = index;
                *nonmatching = false;
                *high_priority = efec == FDCAN_EFEC_HP ||
                                 efec == FDCAN_SFEC_FIFO0_HP ||
                                 efec == FDCAN_SFEC_FIFO1_HP;
                if (efec == FDCAN_EFEC_HP) {
                    return FDCAN_RX_HIGH_PRIORITY;
                }
                return fdcan_filter_destination(efec);
            }
        }
        return fdcan_global_destination(gfc, true);
    }

    if ((frame->flags & CAN_FLAG_RTR) && (gfc & FDCAN_GFC_RRFS)) {
            return FDCAN_RX_DROP;
    }

    uint32_t sidfc = fdcan_load(s, FDCAN_SIDFC, 4);
    unsigned count = (sidfc & FDCAN_SIDFC_LSS_MASK) >>
                     FDCAN_SIDFC_LSS_SHIFT;
    size_t offset = (sidfc & FDCAN_SIDFC_FLSSA_MASK) * 4u;

    count = MIN(count, s->msg_ram_size / sizeof(uint32_t));
    for (unsigned index = 0; index < count; ++index, offset += 4) {
        uint32_t element;
        uint32_t sfec;
        uint32_t sft;
        uint32_t id1;
        uint32_t id2;
        bool match;

        if (offset > s->msg_ram_size - sizeof(uint32_t)) {
            break;
        }
        element = msg_ram_read32(s, offset);
        sfec = (element & FDCAN_SF_SFEC_MASK) >> FDCAN_SF_SFEC_SHIFT;
        id1 = (element & FDCAN_SF_ID1_MASK) >> 16;
        id2 = element & FDCAN_SF_ID2_MASK;
        if (sfec == FDCAN_SFEC_DISABLE) {
            continue;
        }
        if (sfec == FDCAN_SFEC_RX_BUFFER) {
            /* SFID1 is an exact ID in this mode; SFID2[5:0] is the
             * dedicated Rx Buffer index and SFID2[10:9] selects debug
             * destinations. */
            unsigned index_buffer = id2 & FDCAN_RX_BUFFER_INDEX_MASK;
            unsigned target = (id2 >> 9) & 3;
            uint32_t ndat = fdcan_load(s, index_buffer < 32 ? FDCAN_NDAT1 :
                                        FDCAN_NDAT2, 4);

            if (target == 0 && frame->can_id == id1) {
                /* As with the extended filter path, a busy matching buffer
                 * is a consumed filter hit and the frame is discarded. */
                if (!(ndat & (1u << (index_buffer % 32)))) {
                    *filter_index = index;
                    *nonmatching = false;
                    *buffer_index = index_buffer;
                    return FDCAN_RX_BUFFER;
                }
                return FDCAN_RX_DROP;
            }
            continue;
        }
        sft = element >> 30;
        switch (sft) {
        case 0: /* range */
            match = frame->can_id >= id1 && frame->can_id <= id2;
            break;
        case 1: /* dual ID */
            match = frame->can_id == id1 || frame->can_id == id2;
            break;
        case 2: /* classic mask */
            match = (frame->can_id & id2) == (id1 & id2);
            break;
        default:
            match = false;
            break;
        }
        if (!match) {
            continue;
        }
        *filter_index = index;
        *nonmatching = false;
        *high_priority = sfec == FDCAN_SFEC_HP ||
                         sfec == FDCAN_SFEC_FIFO0_HP ||
                         sfec == FDCAN_SFEC_FIFO1_HP;
        if (sfec == FDCAN_SFEC_HP) {
            return FDCAN_RX_HIGH_PRIORITY;
        }
        /* SFEC 1/5 are FIFO0 (the latter also marks high priority). FIFO1 and
         * reject are handled here; SFEC=7 was handled as a dedicated buffer
         * above and must not be silently redirected to FIFO0. */
        return fdcan_filter_destination(sfec);
    }

    return fdcan_global_destination(gfc, false);
}

static bool msg_ram_write32(DmMc02Fdcan *s, size_t offset, uint32_t value)
{
    uint8_t bytes[sizeof(value)];

    stl_le_p(bytes, value);
    return msg_ram_write(s, offset, bytes, sizeof(bytes));
}

static unsigned tx_base_bytes(const DmMc02Fdcan *s)
{
    return (fdcan_load(s, FDCAN_TXBC, 4) & 0xfffcu) * 4u;
}

static unsigned rx_base_bytes(const DmMc02Fdcan *s, unsigned fifo)
{
    return (fdcan_load(s, fifo ? FDCAN_RXF1C : FDCAN_RXF0C, 4) &
            FDCAN_SIDFC_FLSSA_MASK) * 4u;
}

static unsigned rx_fifo_element_bytes(const DmMc02Fdcan *s, unsigned fifo)
{
    uint32_t esc = fdcan_load(s, FDCAN_RXESC, 4);

    /* RXESC.F0DS occupies bits 2:0 and F1DS occupies bits 6:4. */
    return element_bytes((esc >> (fifo ? 4 : 0)) & 7u);
}

static unsigned rx_buffer_base_bytes(const DmMc02Fdcan *s)
{
    return (fdcan_load(s, FDCAN_RXBC, 4) & 0xfffcu) * 4u;
}

static unsigned rx_buffer_element_bytes(const DmMc02Fdcan *s)
{
    return element_bytes((fdcan_load(s, FDCAN_RXESC, 4) >> 8) & 7u);
}

static void refresh_sizes(DmMc02Fdcan *s)
{
    uint32_t txbc = fdcan_load(s, FDCAN_TXBC, 4);
    uint32_t rxf0c = fdcan_load(s, FDCAN_RXF0C, 4);
    uint32_t rxf1c = fdcan_load(s, FDCAN_RXF1C, 4);

    s->tx_fifo_size = (txbc >> 16) & 0x3f;
    s->rx_fifo_size = (rxf0c >> 16) & 0x3f;
    s->rx_fifo1_size = (rxf1c >> 16) & 0x3f;
    if (!s->tx_fifo_size) {
        s->tx_fifo_size = 3;
    }
    if (!s->rx_fifo_size) {
        s->rx_fifo_size = 3;
    }
    if (!s->rx_fifo1_size) {
        s->rx_fifo1_size = 3;
    }
    if (s->tx_fifo_put >= s->tx_fifo_size) {
        s->tx_fifo_put = 0;
    }
    if (s->rx_fifo_get >= s->rx_fifo_size) {
        s->rx_fifo_get = 0;
    }
    if (s->rx_fifo_put >= s->rx_fifo_size) {
        s->rx_fifo_put = 0;
    }
    if (s->rx_fifo1_get >= s->rx_fifo1_size) {
        s->rx_fifo1_get = 0;
    }
    if (s->rx_fifo1_put >= s->rx_fifo1_size) {
        s->rx_fifo1_put = 0;
    }
}

static uint32_t fdcan_rx_fifo_status(const DmMc02Fdcan *s)
{
    uint32_t value = fdcan_load(s, FDCAN_RXF0S, 4);

    value &= ~UINT32_C(0x3f3f7f);
    value |= s->rx_fifo_fill & 0x7f;
    value |= (uint32_t)(s->rx_fifo_get & 0x3f) << 8;
    value |= (uint32_t)(s->rx_fifo_put & 0x3f) << 16;
    if (s->rx_fifo_fill >= s->rx_fifo_size) {
        value |= UINT32_C(1) << 24;
    }
    return value;
}

static uint32_t fdcan_rx_fifo1_status(const DmMc02Fdcan *s)
{
    uint32_t value = fdcan_load(s, FDCAN_RXF1S, 4);

    value &= ~UINT32_C(0x3f3f7f);
    value |= s->rx_fifo1_fill & 0x7f;
    value |= (uint32_t)(s->rx_fifo1_get & 0x3f) << 8;
    value |= (uint32_t)(s->rx_fifo1_put & 0x3f) << 16;
    if (s->rx_fifo1_fill >= s->rx_fifo1_size) {
        value |= UINT32_C(1) << 24;
    }
    return value;
}

static uint32_t fdcan_tx_fifo_status(const DmMc02Fdcan *s)
{
    uint32_t value = fdcan_load(s, FDCAN_TXFQS, 4);
    uint32_t valid_mask;
    unsigned used;
    unsigned free_level;

    value &= ~UINT32_C(0x3f1f3f);
    valid_mask = s->tx_fifo_size >= 32 ? UINT32_MAX :
                 ((UINT32_C(1) << s->tx_fifo_size) - 1);
    used = ctpop32(s->tx_pending_mask & valid_mask);
    free_level = used < s->tx_fifo_size ? s->tx_fifo_size - used : 0;
    value |= free_level;
    value |= (uint32_t)(s->tx_fifo_get & 0x1f) << 8;
    value |= (uint32_t)(s->tx_fifo_put & 0x1f) << 16;
    if (free_level == 0) {
        value |= UINT32_C(1) << 21;
    }
    return value;
}

static bool send_can_wire(DmMc02Fdcan *s, const DmMc02CanFrame *frame)
{
    uint8_t wire[DM_MC02_CAN_WIRE_SIZE] = { 0 };

    /* The chardev is an optional host wire.  Lack of a host backend must not
     * prevent the standard QEMU CAN bus from delivering a frame. */
    if (!s->chr_enabled) {
        return true;
    }
    if (!s->transceiver_powered) {
        return true;
    }
    stl_le_p(wire + 0, frame->can_id);
    stl_le_p(wire + 4, frame->flags);
    wire[8] = frame->dlc & 0xf;
    stq_le_p(wire + 12, frame->timestamp_ns);
    memcpy(wire + 20, frame->data, dlc_to_length(frame->dlc));
    if (s->tx_queue_count == DM_MC02_FDCAN_TX_QUEUE_SIZE) {
        /* CAN data is ordered: preserve older frames and drop the newest
         * frame as a whole when the bounded queue is full. */
        s->tx_dropped++;
        return false;
    }
    unsigned slot = (s->tx_queue_head + s->tx_queue_count) %
                    DM_MC02_FDCAN_TX_QUEUE_SIZE;
    memcpy(s->tx_queue[slot], wire, sizeof(wire));
    s->tx_queue_offset[slot] = 0;
    s->tx_queue_count++;
    fdcan_flush_tx(s);
    return true;
}

static void transmit_element(DmMc02Fdcan *s, unsigned index,
                             uint64_t timestamp_ns)
{
    uint32_t word0;
    uint32_t word1;
    DmMc02CanFrame frame = { 0 };
    unsigned bytes;
    unsigned data_capacity;
    unsigned offset;
    unsigned element_size;
    qemu_can_frame qframe = { 0 };
    ssize_t bus_result;

    refresh_sizes(s);
    if (index >= s->tx_fifo_size) {
        return;
    }
    element_size = element_bytes(fdcan_load(s, FDCAN_TXESC, 4));
    offset = tx_base_bytes(s) + index * element_size;
    if (!s->msg_ram || offset > s->msg_ram_size ||
        element_size > s->msg_ram_size - offset) {
        s->tx_pending_mask &= ~(UINT32_C(1) << index);
        return;
    }
    word0 = msg_ram_read32(s, offset);
    word1 = msg_ram_read32(s, offset + 4);
    frame.can_id = word0 & 0x1fffffffu;
    if (word0 & (1u << 30)) {
        frame.flags |= CAN_FLAG_EXTENDED;
    } else {
        frame.can_id = (word0 >> 18) & 0x7ffu;
    }
    if (word0 & (1u << 29)) {
        frame.flags |= CAN_FLAG_RTR;
    }
    if (word1 & (1u << 21)) {
        frame.flags |= CAN_FLAG_FD;
    }
    if (word1 & (1u << 20)) {
        frame.flags |= CAN_FLAG_BRS;
    }
    frame.dlc = (word1 >> 16) & 0xf;
    /* TXESC selects the number of data bytes physically present in each
     * message element.  A malformed/inconsistent guest configuration must
     * not make a large DLC read into the next element in message RAM. */
    data_capacity = element_size > 8 ? element_size - 8 : 0;
    bytes = MIN(dlc_to_length(frame.dlc), data_capacity);
    frame.tx_index = index;
    frame.timestamp_ns = timestamp_ns;
    for (unsigned i = 0; i < bytes; ++i) {
        size_t byte_offset = offset + 8 + i;

        if (s->msg_ram && byte_offset < s->msg_ram_size) {
            frame.data[i] = s->msg_ram[byte_offset];
        }
    }
    if (!s->transceiver_powered) {
        /* The M_CAN core may still be clocked while the external
         * transceiver is unpowered, but no frame can reach the bus. */
        s->tx_dropped++;
        fdcan_tx_complete(s, &frame, false);
        return;
    }
    if (s->bus_off) {
        /* The controller may observe a TX request while bus-off, but it
         * cannot place a frame on the bus. */
        fdcan_tx_complete(s, &frame, false);
        return;
    }
    qframe.can_id = frame.can_id;
    if (frame.flags & CAN_FLAG_EXTENDED) {
        qframe.can_id |= QEMU_CAN_EFF_FLAG;
    }
    if (frame.flags & CAN_FLAG_RTR) {
        qframe.can_id |= QEMU_CAN_RTR_FLAG;
    }
    qframe.can_dlc = dlc_to_length(frame.dlc);
    if (frame.flags & CAN_FLAG_FD) {
        qframe.flags |= QEMU_CAN_FRMF_TYPE_FD;
    }
    if (frame.flags & CAN_FLAG_BRS) {
        qframe.flags |= QEMU_CAN_FRMF_BRS;
    }
    memcpy(qframe.data, frame.data, qframe.can_dlc);
    bus_result = dm_can_bus_adapter_send(&s->can_bus, &qframe);
    /* The fixed chardev is an optional host wire. It does not replace the
     * standard QEMU bus and contributes ACK only under explicit policy. */
    bool host_sent = send_can_wire(s, &frame);
    bool host_ack = s->host_ack && host_sent && s->chr_enabled &&
                    qemu_chr_fe_backend_open(&s->chr);

    fdcan_tx_complete(s, &frame, bus_result > 0 || host_ack);
}

static void receive_frame(DmMc02Fdcan *s, const DmMc02CanFrame *frame)
{
    uint32_t can_id = frame->can_id;
    uint32_t flags = frame->flags;
    unsigned dlc = frame->dlc & 0xf;
    unsigned bytes = dlc_to_length(dlc);
    uint8_t filter_index;
    bool nonmatching;
    bool high_priority;
    unsigned offset;
    unsigned fifo;
    unsigned buffer_index;
    uint8_t *fill;
    uint8_t *put;
    uint8_t fifo_size;
    uint32_t new_event;
    unsigned element_size;
    uint32_t word0;
    uint32_t word1;

    if (!s->transceiver_powered || fdcan_in_init(s)) {
        s->rx_dropped++;
        return;
    }

    refresh_sizes(s);
    switch (fdcan_accept_rx(s, frame, &filter_index, &nonmatching,
                             &buffer_index, &high_priority)) {
    case FDCAN_RX_HIGH_PRIORITY:
        /* HPMS.MSI=0 means that the matching message was not stored in a
         * FIFO or dedicated Rx Buffer.  BIDX is consequently zero; the
         * filter index and HPM interrupt are still reported. */
        fdcan_set_hpms(s, (flags & CAN_FLAG_EXTENDED) != 0,
                       filter_index, 0, 0);
        fdcan_update_irq(s);
        return;
    case FDCAN_RX_BUFFER:
        element_size = rx_buffer_element_bytes(s);
        offset = rx_buffer_base_bytes(s) + buffer_index * element_size;
        if (!s->msg_ram || offset > s->msg_ram_size ||
            element_size > s->msg_ram_size - offset) {
            s->rx_dropped++;
            return;
        }
        word0 = flags & CAN_FLAG_EXTENDED ? can_id & 0x1fffffffu :
                 (can_id & 0x7ffu) << 18;
        if (flags & CAN_FLAG_EXTENDED) {
            word0 |= 1u << 30;
        }
        if (flags & CAN_FLAG_RTR) {
            word0 |= 1u << 29;
        }
        word1 = (uint32_t)dlc << 16;
        word1 |= (uint32_t)filter_index << 24;
        if (flags & CAN_FLAG_FD) {
            word1 |= 1u << 21;
        }
        if (flags & CAN_FLAG_BRS) {
            word1 |= 1u << 20;
        }
        if (!msg_ram_write32(s, offset, word0) ||
            !msg_ram_write32(s, offset + 4, word1)) {
            s->rx_dropped++;
            return;
        }
        {
            size_t copy = MIN((size_t)bytes, (size_t)element_size - 8);

            memset(s->msg_ram + offset + 8, 0, element_size - 8);
            memcpy(s->msg_ram + offset + 8, frame->data, copy);
        }
        fdcan_store(s, buffer_index < 32 ? FDCAN_NDAT1 : FDCAN_NDAT2,
                    fdcan_load(s, buffer_index < 32 ? FDCAN_NDAT1 :
                               FDCAN_NDAT2, 4) |
                    (1u << (buffer_index % 32)), 4);
        fdcan_store(s, FDCAN_IR, fdcan_load(s, FDCAN_IR, 4) |
                    FDCAN_IR_DRX, 4);
        fdcan_update_irq(s);
        return;
    case FDCAN_RX_FIFO0:
        fifo = 0;
        fill = &s->rx_fifo_fill;
        put = &s->rx_fifo_put;
        fifo_size = s->rx_fifo_size;
        new_event = FDCAN_IR_RF0N;
        break;
    case FDCAN_RX_FIFO1:
        fifo = 1;
        fill = &s->rx_fifo1_fill;
        put = &s->rx_fifo1_put;
        fifo_size = s->rx_fifo1_size;
        new_event = FDCAN_IR_RF1N;
        break;
    default:
        s->rx_dropped++;
        return;
    }

    if (*fill >= fifo_size) {
        s->rx_dropped++;
        fdcan_store(s, FDCAN_IR, fdcan_load(s, FDCAN_IR, 4) |
                    (fifo ? FDCAN_IR_RF1F : FDCAN_IR_RF0F), 4);
        return;
    }
    element_size = rx_fifo_element_bytes(s, fifo);
    offset = rx_base_bytes(s, fifo) + *put * element_size;
    if (!s->msg_ram || offset > s->msg_ram_size ||
        element_size > s->msg_ram_size - offset) {
        s->rx_dropped++;
        return;
    }
    word0 = flags & CAN_FLAG_EXTENDED ? can_id & 0x1fffffffu :
             (can_id & 0x7ffu) << 18;
    if (flags & CAN_FLAG_EXTENDED) {
        word0 |= 1u << 30;
    }
    if (flags & CAN_FLAG_RTR) {
        word0 |= 1u << 29;
    }
    word1 = (uint32_t)dlc << 16;
    word1 |= (uint32_t)filter_index << 24;
    if (nonmatching) {
        word1 |= UINT32_C(1) << 31;
    }
    if (flags & CAN_FLAG_FD) {
        word1 |= 1u << 21;
    }
    if (flags & CAN_FLAG_BRS) {
        word1 |= 1u << 20;
    }
    if (!msg_ram_write32(s, offset, word0) ||
        !msg_ram_write32(s, offset + 4, word1)) {
        s->rx_dropped++;
        return;
    }
    size_t copy = MIN((size_t)bytes, (size_t)element_size - 8);

    memset(s->msg_ram + offset + 8, 0, element_size - 8);
    memcpy(s->msg_ram + offset + 8, frame->data, copy);
    *put = (*put + 1) % fifo_size;
    (*fill)++;
    if (high_priority) {
        fdcan_set_hpms(s, (flags & CAN_FLAG_EXTENDED) != 0,
                       filter_index, fifo ? 3u : 2u,
                       (uint8_t)((*put + fifo_size - 1) % fifo_size));
    }
    fdcan_store(s, FDCAN_IR, fdcan_load(s, FDCAN_IR, 4) | new_event, 4);
    fdcan_update_irq(s);
}

static bool fdcan_can_receive(void *opaque)
{
    DmMc02Fdcan *s = opaque;

    /* ACK is a controller/bus-level property, independent of whether the
     * receiver's acceptance filters currently have storage configured. A
     * configured TX-only node is still visible to QEMU's bus as a listener;
     * receive_frame() may discard the frame when no FIFO/buffer is available.
     * INIT is a controller state in which the node is disconnected. */
    return s->transceiver_powered && !s->bus_off && !fdcan_in_init(s) &&
           (s->rx_fifo_configured || s->rx_buffer_configured ||
            s->tx_fifo_configured);
}

static void fdcan_can_receive_frame(void *opaque,
                                    const qemu_can_frame *qframe)
{
    DmMc02Fdcan *s = opaque;
    DmMc02CanFrame frame = { 0 };
    unsigned length;
    unsigned dlc;

    if (!qframe || (qframe->can_id & QEMU_CAN_ERR_FLAG) ||
        (qframe->flags & ~(QEMU_CAN_FRMF_BRS | QEMU_CAN_FRMF_TYPE_FD))) {
        s->rx_dropped++;
        return;
    }
    if (qframe->can_id & QEMU_CAN_EFF_FLAG) {
        frame.flags |= CAN_FLAG_EXTENDED;
        frame.can_id = qframe->can_id & QEMU_CAN_EFF_MASK;
    } else {
        frame.can_id = qframe->can_id & QEMU_CAN_SFF_MASK;
    }
    if (qframe->can_id & QEMU_CAN_RTR_FLAG) {
        frame.flags |= CAN_FLAG_RTR;
    }
    if (qframe->flags & QEMU_CAN_FRMF_TYPE_FD) {
        frame.flags |= CAN_FLAG_FD;
    }
    if (qframe->flags & QEMU_CAN_FRMF_BRS) {
        frame.flags |= CAN_FLAG_BRS;
    }
    length = qframe->can_dlc;
    if (length > 64 || (!(frame.flags & CAN_FLAG_FD) && length > 8) ||
        ((frame.flags & CAN_FLAG_FD) && (frame.flags & CAN_FLAG_RTR)) ||
        (!(frame.flags & CAN_FLAG_FD) && (frame.flags & CAN_FLAG_BRS))) {
        s->rx_dropped++;
        return;
    }
    dlc = can_len2dlc(length);
    if (can_dlc2len(dlc) != length ||
        (!(frame.flags & CAN_FLAG_FD) && dlc > 8)) {
        s->rx_dropped++;
        return;
    }
    frame.dlc = dlc;
    frame.timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    memcpy(frame.data, qframe->data, length);
    receive_frame(s, &frame);
}

static void receive_wire(DmMc02Fdcan *s, const uint8_t *wire)
{
    DmMc02CanFrame frame = { 0 };

    if (!fdcan_wire_frame_valid(wire)) {
        s->rx_dropped++;
        return;
    }

    frame.can_id = ldl_le_p(wire + 0);
    frame.flags = ldl_le_p(wire + 4);
    frame.dlc = wire[8] & 0xf;
    /* Preserve the timestamp assigned by the external plant.  Replacing it
     * with the callback's current QEMU time destroys cross-channel ordering
     * and prevents the worker from scheduling commands against IMU time. */
    frame.timestamp_ns = ldq_le_p(wire + 12);
    memcpy(frame.data, wire + 20, dlc_to_length(frame.dlc));
    receive_frame(s, &frame);
}

static int can_can_receive(void *opaque)
{
    DmMc02Fdcan *s = opaque;

    return sizeof(s->rx_wire) - s->rx_wire_len;
}

static void can_receive(void *opaque, const uint8_t *buf, int size)
{
    DmMc02Fdcan *s = opaque;
    size_t incoming = size > 0 ? (size_t)size : 0;
    size_t copy = MIN(incoming, sizeof(s->rx_wire) - s->rx_wire_len);

    memcpy(s->rx_wire + s->rx_wire_len, buf, copy);
    s->rx_wire_len += copy;
    if (incoming != copy) {
        s->rx_dropped++;
    }
    while (s->rx_wire_len >= DM_MC02_CAN_WIRE_SIZE) {
        receive_wire(s, s->rx_wire);
        s->rx_wire_len -= DM_MC02_CAN_WIRE_SIZE;
        memmove(s->rx_wire, s->rx_wire + DM_MC02_CAN_WIRE_SIZE,
                s->rx_wire_len);
    }
    qemu_chr_fe_accept_input(&s->chr);
}

static void can_event(void *opaque, QEMUChrEvent event)
{
    DmMc02Fdcan *s = opaque;

    if (event == CHR_EVENT_CLOSED) {
        s->rx_wire_len = 0;
    } else if (event == CHR_EVENT_OPENED) {
        fdcan_flush_tx(s);
    }
}

static uint64_t dm_mc02_fdcan_read(void *opaque, hwaddr offset,
                                   unsigned size)
{
    DmMc02Fdcan *s = opaque;

    if (size > 4 || offset >= DM_MC02_FDCAN_REGION_SIZE ||
        size > DM_MC02_FDCAN_REGION_SIZE - offset) {
        return 0;
    }
    if (size == 4 && offset == FDCAN_RXF0S) {
        return fdcan_rx_fifo_status(s);
    }
    if (size == 4 && offset == FDCAN_RXF1S) {
        return fdcan_rx_fifo1_status(s);
    }
    if (size == 4 && offset == FDCAN_TXFQS) {
        return fdcan_tx_fifo_status(s);
    }
    if (size == 4 && offset == FDCAN_TXBRP) {
        return s->tx_pending_mask;
    }
    return fdcan_load(s, offset, size);
}

static void dm_mc02_fdcan_write(void *opaque, hwaddr offset, uint64_t value,
                                unsigned size)
{
    DmMc02Fdcan *s = opaque;

    if (size > 4 || offset >= DM_MC02_FDCAN_REGION_SIZE ||
        size > DM_MC02_FDCAN_REGION_SIZE - offset) {
        return;
    }
    if (size == 4 && offset == FDCAN_IR) {
        fdcan_store(s, offset, fdcan_load(s, offset, 4) & ~(uint32_t)value,
                    4);
        fdcan_update_irq(s);
        return;
    }
    if (size == 4 && (offset == FDCAN_NDAT1 || offset == FDCAN_NDAT2)) {
        /* NDAT is read-only status in hardware and is cleared by writing
         * ones to the corresponding bits. */
        fdcan_store(s, offset, fdcan_load(s, offset, 4) &
                    ~(uint32_t)value, 4);
        return;
    }
    if (size == 4 && offset == FDCAN_RXF0A) {
        refresh_sizes(s);
        if (s->rx_fifo_fill != 0 &&
            ((uint32_t)value & 0x3f) == s->rx_fifo_get) {
            s->rx_fifo_get = (s->rx_fifo_get + 1) % s->rx_fifo_size;
            s->rx_fifo_fill--;
        }
        fdcan_store(s, offset, value, size);
        return;
    }
    if (size == 4 && offset == FDCAN_RXF1A) {
        refresh_sizes(s);
        if (s->rx_fifo1_fill != 0 &&
            ((uint32_t)value & 0x3f) == s->rx_fifo1_get) {
            s->rx_fifo1_get = (s->rx_fifo1_get + 1) % s->rx_fifo1_size;
            s->rx_fifo1_fill--;
        }
        fdcan_store(s, offset, value, size);
        return;
    }
    if (size == 4 && offset == FDCAN_RXF0C) {
        uint32_t old = fdcan_load(s, offset, 4);

        fdcan_store(s, offset, value, size);
        s->rx_fifo_configured = true;
        if ((old ^ (uint32_t)value) & UINT32_C(0x003f3fff)) {
            s->rx_fifo_fill = 0;
            s->rx_fifo_get = 0;
            s->rx_fifo_put = 0;
        }
        refresh_sizes(s);
        return;
    }
    if (size == 4 && offset == FDCAN_RXBC) {
        fdcan_store(s, offset, value, size);
        s->rx_buffer_configured = true;
        return;
    }
    if (size == 4 && offset == FDCAN_RXF1C) {
        uint32_t old = fdcan_load(s, offset, 4);

        fdcan_store(s, offset, value, size);
        s->rx_fifo_configured = true;
        if ((old ^ (uint32_t)value) & UINT32_C(0x003f3fff)) {
            s->rx_fifo1_fill = 0;
            s->rx_fifo1_get = 0;
            s->rx_fifo1_put = 0;
        }
        refresh_sizes(s);
        return;
    }
    if (size == 4 && offset == FDCAN_TXBC) {
        fdcan_store(s, offset, value, size);
        s->tx_fifo_configured = true;
        s->tx_fifo_put = 0;
        s->tx_fifo_get = 0;
        s->tx_pending_mask = 0;
        refresh_sizes(s);
        return;
    }
    if (size == 4 && offset == FDCAN_CCCR) {
        uint32_t old = fdcan_load(s, offset, 4);
        uint32_t next = (uint32_t)value;

        if (next & FDCAN_CCCR_INIT) {
            next |= FDCAN_CCCR_CCE;
        } else if (s->bus_off && (old & FDCAN_CCCR_INIT)) {
            /* Simplified recovery contract: an explicit INIT -> normal-mode
             * transition represents controller reinitialisation.  The full
             * 129 x 11 recessive-bit recovery sequence is not modelled. */
            s->bus_off = false;
            fdcan_store(s, FDCAN_PSR,
                        fdcan_load(s, FDCAN_PSR, 4) & ~FDCAN_PSR_BO, 4);
            fdcan_store(s, FDCAN_ECR,
                        fdcan_load(s, FDCAN_ECR, 4) & ~0xffu, 4);
        }
        fdcan_store(s, offset, next, 4);
        return;
    }
    fdcan_store(s, offset, value, size);
    if (size == 4 && offset == FDCAN_TXBAR) {
        uint32_t pending = value;
        uint64_t timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

        if (fdcan_in_init(s)) {
            return;
        }
        for (unsigned i = 0; i < 32; ++i) {
            if ((pending & (1u << i)) && i < s->tx_fifo_size &&
                !(s->tx_pending_mask & (1u << i))) {
                s->tx_pending_mask |= 1u << i;
                s->tx_fifo_put = (i + 1) % s->tx_fifo_size;
                transmit_element(s, i, timestamp_ns);
            }
        }
    }
    if (size == 4 && (offset == FDCAN_IE || offset == FDCAN_ILS ||
                      offset == FDCAN_ILE)) {
        fdcan_update_irq(s);
    }
}

static const MemoryRegionOps dm_mc02_fdcan_ops = {
    .read = dm_mc02_fdcan_read,
    .write = dm_mc02_fdcan_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

bool dm_mc02_fdcan_init(DmMc02Fdcan *state, Object *owner, const char *name,
                        uint32_t region_size, uint8_t *msg_ram,
                        size_t msg_ram_size, Chardev *chardev, Error **errp)
{
    memset(state, 0, sizeof(*state));
    dm_can_bus_adapter_init(&state->can_bus);
    state->transceiver_powered = true;
    state->tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, fdcan_tx_retry, state);
    state->msg_ram = msg_ram;
    state->msg_ram_size = msg_ram_size;
    fdcan_store(state, FDCAN_XIDAM, FDCAN_EF_ID_MASK, 4);
    memory_region_init_io(&state->iomem, owner, &dm_mc02_fdcan_ops, state,
                          name, region_size);
    refresh_sizes(state);
    if (!chardev) {
        return true;
    }
    if (!qemu_chr_fe_init(&state->chr, chardev, errp)) {
        return false;
    }
    state->chr_enabled = true;
    qemu_chr_fe_set_handlers(&state->chr, can_can_receive, can_receive,
                             can_event, NULL, state, NULL, true);
    return true;
}

void dm_mc02_fdcan_cleanup(DmMc02Fdcan *state)
{
    if (!state) {
        return;
    }
    dm_can_bus_adapter_disconnect(&state->can_bus);
    timer_free(state->tx_timer);
    state->tx_timer = NULL;
    state->tx_next_ns = 0;
    if (!state->chr_enabled) {
        return;
    }
    qemu_chr_fe_set_handlers(&state->chr, NULL, NULL, NULL, NULL, NULL, NULL,
                             false);
    qemu_chr_fe_deinit(&state->chr, false);
    state->chr_enabled = false;
}

void dm_mc02_fdcan_set_irq(DmMc02Fdcan *state, unsigned line, qemu_irq irq)
{
    if (line >= 2) {
        return;
    }
    state->irq[line] = irq;
    state->irq_level_valid[line] = false;
    fdcan_update_irq(state);
}

void dm_mc02_fdcan_set_powered(DmMc02Fdcan *state, bool powered)
{
    if (state) {
        state->transceiver_powered = powered;
        if (!powered) {
            uint32_t pending = state->tx_pending_mask;

            /* A transceiver power cut cannot complete a bus transaction, but
             * the controller must not leave guest-visible TX requests stuck
             * forever.  Complete each request as a coarse no-ACK error. */
            for (unsigned i = 0; i < 32; ++i) {
                if (pending & (UINT32_C(1) << i)) {
                    DmMc02CanFrame frame = { .tx_index = i };

                    fdcan_tx_complete(state, &frame, false);
                }
            }
            state->rx_wire_len = 0;
            state->rx_fifo_fill = 0;
            state->rx_fifo1_fill = 0;
            state->tx_queue_head = 0;
            state->tx_queue_count = 0;
            memset(state->tx_queue_offset, 0, sizeof(state->tx_queue_offset));
            if (state->tx_timer) {
                timer_del(state->tx_timer);
            }
            state->tx_next_ns = 0;
            fdcan_update_irq(state);
        }
    }
}

void dm_mc02_fdcan_set_host_ack(DmMc02Fdcan *state, bool enabled)
{
    if (state) {
        state->host_ack = enabled;
    }
}

void dm_mc02_fdcan_reset(DmMc02Fdcan *s)
{
    if (!s) {
        return;
    }
    memset(s->regs, 0, sizeof(s->regs));
    /* M_CAN is disconnected from the bus until software completes
     * initialization and clears CCCR.INIT. */
    fdcan_store(s, FDCAN_CCCR, FDCAN_CCCR_INIT, 4);
    /* XIDAM resets to an all-ones extended-ID mask on M_CAN. */
    fdcan_store(s, FDCAN_XIDAM, FDCAN_EF_ID_MASK, 4);
    memset(s->rx_wire, 0, sizeof(s->rx_wire));
    s->rx_wire_len = 0;
    s->rx_fifo_fill = 0;
    s->rx_fifo_get = 0;
    s->rx_fifo_put = 0;
    s->rx_fifo1_fill = 0;
    s->rx_fifo1_get = 0;
    s->rx_fifo1_put = 0;
    s->tx_fifo_put = 0;
    s->rx_fifo_configured = false;
    s->rx_buffer_configured = false;
    s->tx_fifo_configured = false;
    s->tx_pending_mask = 0;
    s->tx_fifo_get = 0;
    s->bus_off = false;
    s->rx_dropped = 0;
    s->tx_dropped = 0;
    s->tx_no_ack = 0;
    s->tx_queue_head = 0;
    s->tx_queue_count = 0;
    memset(s->tx_queue_offset, 0, sizeof(s->tx_queue_offset));
    s->tx_short_writes = 0;
    s->tx_next_ns = 0;
    memset(s->irq_level, 0, sizeof(s->irq_level));
    memset(s->irq_level_valid, 0, sizeof(s->irq_level_valid));
    if (s->tx_timer) {
        timer_del(s->tx_timer);
    }
    refresh_sizes(s);
    fdcan_update_irq(s);
}

static uint8_t fdcan_effective_fifo_size(uint32_t config)
{
    uint8_t size = (config >> 16) & 0x3f;

    return size ? size : 3;
}

static bool fdcan_fifo_state_valid(uint8_t fill, uint8_t get, uint8_t put,
                                   uint8_t size)
{
    return fill <= size && get < size && put < size;
}

bool dm_mc02_fdcan_state_valid(const DmMc02Fdcan *s)
{
    uint8_t rx_fifo_size;
    uint8_t rx_fifo1_size;
    uint8_t tx_fifo_size;
    uint32_t valid_mask;

    if (!s || s->rx_wire_len > sizeof(s->rx_wire) ||
        s->tx_queue_head >= DM_MC02_FDCAN_TX_QUEUE_SIZE ||
        s->tx_queue_count > DM_MC02_FDCAN_TX_QUEUE_SIZE ||
        s->tx_next_ns > INT64_MAX) {
        return false;
    }

    if (s->bus_off != !!(fdcan_load(s, FDCAN_PSR, 4) & FDCAN_PSR_BO)) {
        return false;
    }

    rx_fifo_size = fdcan_effective_fifo_size(fdcan_load(s, FDCAN_RXF0C, 4));
    rx_fifo1_size = fdcan_effective_fifo_size(fdcan_load(s, FDCAN_RXF1C, 4));
    tx_fifo_size = fdcan_effective_fifo_size(fdcan_load(s, FDCAN_TXBC, 4));
    if (!fdcan_fifo_state_valid(s->rx_fifo_fill, s->rx_fifo_get,
                                s->rx_fifo_put, rx_fifo_size) ||
        !fdcan_fifo_state_valid(s->rx_fifo1_fill, s->rx_fifo1_get,
                                s->rx_fifo1_put, rx_fifo1_size) ||
        !fdcan_fifo_state_valid(0, s->tx_fifo_get, s->tx_fifo_put,
                                tx_fifo_size)) {
        return false;
    }

    valid_mask = tx_fifo_size >= 32 ? UINT32_MAX :
                 ((UINT32_C(1) << tx_fifo_size) - 1);
    if (s->tx_pending_mask & ~valid_mask) {
        return false;
    }
    if (!s->tx_queue_count && s->tx_next_ns) {
        return false;
    }
    for (unsigned i = 0; i < DM_MC02_FDCAN_TX_QUEUE_SIZE; ++i) {
        bool active = s->tx_queue_count &&
            ((i >= s->tx_queue_head &&
              i - s->tx_queue_head < s->tx_queue_count) ||
             (i < s->tx_queue_head &&
              i + DM_MC02_FDCAN_TX_QUEUE_SIZE - s->tx_queue_head <
              s->tx_queue_count));

        if (s->tx_queue_offset[i] > DM_MC02_CAN_WIRE_SIZE ||
            (active && s->tx_queue_offset[i] == DM_MC02_CAN_WIRE_SIZE)) {
            return false;
        }
    }
    return true;
}

static bool fdcan_message_ram_span_valid(const DmMc02Fdcan *s,
                                         uint64_t base, uint64_t count,
                                         uint64_t element_size)
{
    if (!element_size) {
        return false;
    }
    if (!count) {
        return true;
    }
    if (base > s->msg_ram_size) {
        return false;
    }
    return count <= (s->msg_ram_size - base) / element_size;
}

bool dm_mc02_fdcan_message_ram_state_valid(const DmMc02Fdcan *s)
{
    uint32_t sidfc;
    uint32_t xidfc;
    uint32_t rxf0c;
    uint32_t rxf1c;
    uint32_t rxbc;
    uint32_t txbc;
    uint32_t rxesc;
    uint32_t txesc;
    unsigned sid_count;
    unsigned xid_count;
    unsigned rx_fifo0_size;
    unsigned rx_fifo1_size;
    unsigned tx_fifo_size;
    unsigned rx_fifo0_element_size;
    unsigned rx_fifo1_element_size;
    unsigned rx_buffer_element_size;
    unsigned tx_element_size;

    if (!s || !s->msg_ram || !s->msg_ram_size ||
        !dm_mc02_fdcan_state_valid(s)) {
        return false;
    }

    sidfc = fdcan_load(s, FDCAN_SIDFC, 4);
    xidfc = fdcan_load(s, FDCAN_XIDFC, 4);
    rxf0c = fdcan_load(s, FDCAN_RXF0C, 4);
    rxf1c = fdcan_load(s, FDCAN_RXF1C, 4);
    rxbc = fdcan_load(s, FDCAN_RXBC, 4);
    txbc = fdcan_load(s, FDCAN_TXBC, 4);
    rxesc = fdcan_load(s, FDCAN_RXESC, 4);
    txesc = fdcan_load(s, FDCAN_TXESC, 4);

    /* H723 M_CAN limits are 128 standard and 64 extended filter elements.
     * Keep these limits explicit even though the register fields are wider. */
    sid_count = (sidfc & FDCAN_SIDFC_LSS_MASK) >> FDCAN_SIDFC_LSS_SHIFT;
    xid_count = (xidfc & FDCAN_XIDFC_LSE_MASK) >> FDCAN_XIDFC_LSE_SHIFT;
    if (sid_count > 128 || xid_count > 64) {
        return false;
    }

    if (!fdcan_message_ram_span_valid(
            s, (sidfc & FDCAN_SIDFC_FLSSA_MASK) * 4u, sid_count, 4) ||
        !fdcan_message_ram_span_valid(
            s, (xidfc & FDCAN_XIDFC_FLSSA_MASK) * 4u, xid_count, 8)) {
        return false;
    }

    rx_fifo0_size = fdcan_effective_fifo_size(rxf0c);
    rx_fifo1_size = fdcan_effective_fifo_size(rxf1c);
    tx_fifo_size = fdcan_effective_fifo_size(txbc);
    rx_fifo0_element_size = element_bytes(rxesc & 7u);
    rx_fifo1_element_size = element_bytes((rxesc >> 4) & 7u);
    rx_buffer_element_size = element_bytes((rxesc >> 8) & 7u);
    tx_element_size = element_bytes(txesc);

    if ((s->rx_fifo_configured &&
         !fdcan_message_ram_span_valid(
             s, (rxf0c & FDCAN_SIDFC_FLSSA_MASK) * 4u,
             rx_fifo0_size, rx_fifo0_element_size)) ||
        (s->rx_fifo_configured &&
         !fdcan_message_ram_span_valid(
             s, (rxf1c & FDCAN_SIDFC_FLSSA_MASK) * 4u,
             rx_fifo1_size, rx_fifo1_element_size)) ||
        (s->rx_buffer_configured &&
         !fdcan_message_ram_span_valid(
             s, (rxbc & 0xfffcu) * 4u, 64, rx_buffer_element_size)) ||
        (s->tx_fifo_configured &&
         !fdcan_message_ram_span_valid(
             s, (txbc & 0xfffcu) * 4u, tx_fifo_size, tx_element_size))) {
        return false;
    }
    return true;
}

void dm_mc02_fdcan_sync_runtime(DmMc02Fdcan *s)
{
    uint64_t deadline;
    uint64_t now;

    if (!s) {
        return;
    }

    deadline = s->tx_next_ns;
    if (s->tx_timer) {
        timer_del(s->tx_timer);
    }
    refresh_sizes(s);
    memset(s->irq_level_valid, 0, sizeof(s->irq_level_valid));
    fdcan_update_irq(s);

    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (deadline && s->tx_queue_count && s->tx_timer && s->chr_enabled &&
        s->transceiver_powered && qemu_chr_fe_backend_open(&s->chr)) {
        s->tx_next_ns = deadline < now ? now : deadline;
        timer_mod(s->tx_timer, s->tx_next_ns);
    }
}

void dm_mc02_fdcan_set_canbus(DmMc02Fdcan *state, CanBusState *canbus)
{
    if (!state) {
        return;
    }
    dm_can_bus_adapter_disconnect(&state->can_bus);
    if (canbus && !dm_can_bus_adapter_connect(&state->can_bus, canbus, state,
                                               fdcan_can_receive,
                                               fdcan_can_receive_frame)) {
        error_report("unable to connect FDCAN endpoint to QEMU CAN bus");
    }
}
