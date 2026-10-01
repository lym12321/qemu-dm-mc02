/* STM32H7 DWC2 host-port register subset, independent of board wiring. */
#include "qemu/osdep.h"
#include "hw/usb/dm_stm32h7_otg_host.h"

#define DM_STM32H7_OTG_HPRT0_CHANGE_MASK \
    (DM_STM32H7_OTG_HPRT0_OVRCURRCHG | \
     DM_STM32H7_OTG_HPRT0_ENACHG | \
     DM_STM32H7_OTG_HPRT0_CONNDET)

#define DM_STM32H7_OTG_HOST_CHANNEL_MASK \
    ((1u << DM_STM32H7_OTG_HOST_CHANNELS) - 1u)

static void dm_stm32h7_otg_host_halt_channel(DmStm32H7OtgHost *host,
                                              unsigned channel,
                                              uint32_t interrupt);

static bool dm_stm32h7_otg_host_port_enabled(const DmStm32H7OtgHost *host)
{
    return (host->hprt0 & (DM_STM32H7_OTG_HPRT0_PWR |
                           DM_STM32H7_OTG_HPRT0_CONNSTS |
                           DM_STM32H7_OTG_HPRT0_ENA)) ==
           (DM_STM32H7_OTG_HPRT0_PWR |
            DM_STM32H7_OTG_HPRT0_CONNSTS |
            DM_STM32H7_OTG_HPRT0_ENA) &&
           !(host->hprt0 & DM_STM32H7_OTG_HPRT0_RST);
}

static uint64_t dm_stm32h7_otg_host_sof_interval(
    const DmStm32H7OtgHost *host)
{
    return ((host->hprt0 & DM_STM32H7_OTG_HPRT0_SPD_MASK) >>
            DM_STM32H7_OTG_HPRT0_SPD_SHIFT) ==
           DM_STM32H7_OTG_PORT_HIGH_SPEED ?
           DM_STM32H7_OTG_SOF_HIGH_SPEED_NS :
           DM_STM32H7_OTG_SOF_FULL_SPEED_NS;
}

static void dm_stm32h7_otg_host_start_sof(DmStm32H7OtgHost *host,
                                           uint64_t timestamp_ns)
{
    host->sof_running = true;
    host->next_sof_ns = timestamp_ns + dm_stm32h7_otg_host_sof_interval(host);
}

static void dm_stm32h7_otg_host_update_channel_irq(DmStm32H7OtgHost *host,
                                                    unsigned channel)
{
    if (host->channel[channel].hcint & host->channel[channel].hcintmsk) {
        host->haint |= 1u << channel;
    } else {
        host->haint &= ~(1u << channel);
    }
}

static void dm_stm32h7_otg_host_update_irq(DmStm32H7OtgHost *host)
{
    bool level;

    if (host->hprt0 & DM_STM32H7_OTG_HPRT0_CHANGE_MASK) {
        host->gintsts |= DM_STM32H7_OTG_GINTSTS_PRTINT;
    } else {
        host->gintsts &= ~DM_STM32H7_OTG_GINTSTS_PRTINT;
    }
    if (host->haint & host->haintmsk) {
        host->gintsts |= DM_STM32H7_OTG_GINTSTS_HCINT;
    } else {
        host->gintsts &= ~DM_STM32H7_OTG_GINTSTS_HCINT;
    }

    level = (host->gahbcfg & DM_STM32H7_OTG_GAHBCFG_GINT) &&
            (host->gintmsk & host->gintsts);
    if (level != host->irq_level) {
        host->irq_level = level;
        if (host->irq) {
            host->irq(host->irq_opaque, level);
        }
    }
}

static void dm_stm32h7_otg_host_cancel_active_channels(
    DmStm32H7OtgHost *host)
{
    unsigned channel;

    for (channel = 0; channel < DM_STM32H7_OTG_HOST_CHANNELS; ++channel) {
        DmStm32H7OtgHostChannel *state = &host->channel[channel];

        if (!state->active && !state->waiting_completion) {
            continue;
        }
        if (state->waiting_completion && host->channel_cancel) {
            host->channel_cancel(host->channel_cancel_opaque, host, channel,
                                 state->completion_token);
        }
        /* Port loss cancels work; it is not a channel completion. */
        state->active = false;
        state->waiting_completion = false;
        state->completion_token = 0;
        state->hcchar &= ~DM_STM32H7_OTG_HCCHAR_CHENA;
        dm_stm32h7_otg_host_update_channel_irq(host, channel);
    }
}

void dm_stm32h7_otg_host_reset(DmStm32H7OtgHost *host)
{
    unsigned channel;
    uint64_t next_channel_completion_token;

    if (!host) {
        return;
    }

    dm_stm32h7_otg_host_cancel_active_channels(host);
    next_channel_completion_token = host->next_channel_completion_token;
    host->gahbcfg = 0;
    host->gintsts = 0;
    host->gintmsk = 0;
    host->hcfg = DM_STM32H7_OTG_HCFG_RESVALID;
    host->hfir = DM_STM32H7_OTG_HFIR_RESET;
    host->hfnum = 0;
    host->haint = 0;
    host->haintmsk = 0;
    host->hprt0 = DM_STM32H7_OTG_HPRT0_PWR;
    host->next_sof_ns = 0;
    host->next_channel_completion_token = next_channel_completion_token;
    host->sof_running = false;
    memset(host->channel, 0, sizeof(host->channel));
    for (channel = 0; channel < DM_STM32H7_OTG_HOST_CHANNELS; ++channel) {
        dm_usb_host_pio_fifo_reset(&host->out_fifo[channel]);
        dm_usb_host_pio_fifo_reset(&host->in_fifo[channel]);
    }
    if (host->irq_level) {
        host->irq_level = false;
        if (host->irq) {
            host->irq(host->irq_opaque, false);
        }
    }
}

void dm_stm32h7_otg_host_fifo_write(DmStm32H7OtgHost *host,
                                    unsigned channel, uint32_t value,
                                    unsigned size)
{
    uint8_t data[4];
    unsigned index;

    if (channel >= DM_STM32H7_OTG_HOST_CHANNELS || size > sizeof(data)) {
        return;
    }
    for (index = 0; index < size; ++index) {
        data[index] = value >> (8 * index);
    }
    dm_usb_host_pio_fifo_write(&host->out_fifo[channel], data, size);
}

uint32_t dm_stm32h7_otg_host_fifo_read(DmStm32H7OtgHost *host,
                                       unsigned channel, unsigned size)
{
    uint8_t data[4] = { 0 };
    uint32_t value = 0;
    unsigned index;

    if (channel >= DM_STM32H7_OTG_HOST_CHANNELS || size > sizeof(data)) {
        return 0;
    }
    dm_usb_host_pio_fifo_read(&host->in_fifo[channel], data, size);
    for (index = 0; index < size; ++index) {
        value |= (uint32_t)data[index] << (8 * index);
    }
    return value;
}

bool dm_stm32h7_otg_host_read_out_fifo(void *opaque, unsigned channel,
                                        uint8_t *data, uint32_t length)
{
    DmStm32H7OtgHost *host = opaque;

    return channel < DM_STM32H7_OTG_HOST_CHANNELS &&
           dm_usb_host_pio_fifo_read_exact(&host->out_fifo[channel], data,
                                           length);
}

bool dm_stm32h7_otg_host_write_in_fifo(void *opaque, unsigned channel,
                                       const uint8_t *data, uint32_t length)
{
    DmStm32H7OtgHost *host = opaque;

    return channel < DM_STM32H7_OTG_HOST_CHANNELS &&
           dm_usb_host_pio_fifo_write(&host->in_fifo[channel], data, length) ==
           length;
}

static bool dm_stm32h7_otg_host_hcfifo_channel(uint32_t offset,
                                                unsigned *channel)
{
    if (offset < DM_STM32H7_OTG_HCFIFO(0) || offset % 0x1000u) {
        return false;
    }
    *channel = offset / 0x1000u - 1u;
    return *channel < DM_STM32H7_OTG_HOST_CHANNELS;
}

static bool dm_stm32h7_otg_host_channel_matches_sof_frame(
    const DmStm32H7OtgHostChannel *state, uint32_t frame)
{
    uint32_t endpoint_type = (state->hcchar &
                              DM_STM32H7_OTG_HCCHAR_EPTYPE_MASK) >>
                             DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT;

    if (endpoint_type != 1 && endpoint_type != 3) {
        return true;
    }
    return !!(state->hcchar & DM_STM32H7_OTG_HCCHAR_ODDFRM) ==
           !!(frame & 1);
}

void dm_stm32h7_otg_host_advance_time(DmStm32H7OtgHost *host,
                                      uint64_t timestamp_ns)
{
    if (!host || !host->sof_running || timestamp_ns < host->next_sof_ns) {
        return;
    }

    do {
        unsigned channel;

        host->hfnum = (host->hfnum + 1) & DM_STM32H7_OTG_HFNUM_FRNUM_MASK;
        host->gintsts |= DM_STM32H7_OTG_GINTSTS_SOF;
        for (channel = 0; channel < DM_STM32H7_OTG_HOST_CHANNELS; ++channel) {
            if (!dm_stm32h7_otg_host_channel_matches_sof_frame(
                    &host->channel[channel], host->hfnum)) {
                continue;
            }
            dm_stm32h7_otg_host_service_channel(host, channel,
                                                 host->next_sof_ns);
        }
        host->next_sof_ns += dm_stm32h7_otg_host_sof_interval(host);
    } while (host->sof_running && timestamp_ns >= host->next_sof_ns);
    dm_stm32h7_otg_host_update_irq(host);
}

void dm_stm32h7_otg_host_set_channel_start(
    DmStm32H7OtgHost *host, DmStm32H7OtgHostChannelStart *channel_start,
    void *channel_opaque)
{
    host->channel_start = channel_start;
    host->channel_opaque = channel_opaque;
}

void dm_stm32h7_otg_host_set_channel_cancel(
    DmStm32H7OtgHost *host, DmStm32H7OtgHostChannelCancel *channel_cancel,
    void *channel_cancel_opaque)
{
    host->channel_cancel = channel_cancel;
    host->channel_cancel_opaque = channel_cancel_opaque;
}

bool dm_stm32h7_otg_host_service_channel(DmStm32H7OtgHost *host,
                                         unsigned channel,
                                         uint64_t timestamp_ns)
{
    DmStm32H7OtgHostChannel *state;
    DmStm32H7OtgHostChannelRequest request;
    uint32_t remaining;

    if (channel >= DM_STM32H7_OTG_HOST_CHANNELS) {
        return false;
    }
    state = &host->channel[channel];
    if (!state->active || state->waiting_completion || !host->channel_start ||
        !dm_stm32h7_otg_host_port_enabled(host) ||
        !(state->hctsiz & DM_STM32H7_OTG_HCTSIZ_PKTCNT_MASK) ||
        !(state->hcchar & DM_STM32H7_OTG_HCCHAR_MPS_MASK)) {
        return false;
    }

    remaining = state->hctsiz & DM_STM32H7_OTG_HCTSIZ_XFERSIZE_MASK;
    state->issued_length = remaining <
                           (state->hcchar & DM_STM32H7_OTG_HCCHAR_MPS_MASK) ?
                           remaining :
                           state->hcchar & DM_STM32H7_OTG_HCCHAR_MPS_MASK;
    if (++host->next_channel_completion_token == 0) {
        ++host->next_channel_completion_token;
    }
    state->completion_token = host->next_channel_completion_token;
    state->waiting_completion = true;
    request = (DmStm32H7OtgHostChannelRequest) {
        .channel = channel,
        .device_address = (state->hcchar &
                           DM_STM32H7_OTG_HCCHAR_DEVADDR_MASK) >>
                          DM_STM32H7_OTG_HCCHAR_DEVADDR_SHIFT,
        .endpoint = (state->hcchar & DM_STM32H7_OTG_HCCHAR_EPNUM_MASK) >>
                    DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT,
        .endpoint_type = (state->hcchar &
                          DM_STM32H7_OTG_HCCHAR_EPTYPE_MASK) >>
                         DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT,
        .direction_in = state->hcchar & DM_STM32H7_OTG_HCCHAR_EPDIR,
        .low_speed = state->hcchar & DM_STM32H7_OTG_HCCHAR_LSPDDEV,
        .max_packet_size = state->hcchar & DM_STM32H7_OTG_HCCHAR_MPS_MASK,
        .transfer_size = state->issued_length,
        .packet_count = (state->hctsiz & DM_STM32H7_OTG_HCTSIZ_PKTCNT_MASK) >>
                        DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT,
        .pid = state->next_pid,
        .timestamp_ns = timestamp_ns,
        .completion_token = state->completion_token,
    };
    host->channel_start(host->channel_opaque, &request);
    return true;
}

static void dm_stm32h7_otg_host_halt_channel(DmStm32H7OtgHost *host,
                                              unsigned channel,
                                              uint32_t interrupt)
{
    DmStm32H7OtgHostChannel *state = &host->channel[channel];

    state->active = false;
    state->waiting_completion = false;
    state->hcchar &= ~DM_STM32H7_OTG_HCCHAR_CHENA;
    state->hcint |= interrupt | DM_STM32H7_OTG_HCINT_CHHLTD;
    dm_stm32h7_otg_host_update_channel_irq(host, channel);
}

static DmStm32H7OtgHostChannelPid dm_stm32h7_otg_host_next_pid(
    DmStm32H7OtgHostChannelPid pid)
{
    switch (pid) {
    case DM_STM32H7_OTG_HOST_PID_DATA0:
        return DM_STM32H7_OTG_HOST_PID_DATA1;
    case DM_STM32H7_OTG_HOST_PID_DATA1:
        return DM_STM32H7_OTG_HOST_PID_DATA0;
    default:
        return pid;
    }
}

static void dm_stm32h7_otg_host_complete_channel_internal(
    DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length)
{
    DmStm32H7OtgHostChannel *state;
    uint32_t remaining;
    uint32_t packet_count;
    uint32_t packets_consumed;
    uint32_t max_packet_size;

    if (channel >= DM_STM32H7_OTG_HOST_CHANNELS) {
        return;
    }
    state = &host->channel[channel];
    if (!state->active || !state->waiting_completion ||
        (completion_token && state->completion_token != completion_token)) {
        return;
    }
    state->waiting_completion = false;

    switch (completion) {
    case DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED:
        if (actual_length > state->issued_length) {
            dm_stm32h7_otg_host_halt_channel(
                host, channel, DM_STM32H7_OTG_HCINT_XACTERR);
            break;
        }
        remaining = state->hctsiz & DM_STM32H7_OTG_HCTSIZ_XFERSIZE_MASK;
        packet_count = (state->hctsiz &
                        DM_STM32H7_OTG_HCTSIZ_PKTCNT_MASK) >>
                       DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT;
        max_packet_size = state->hcchar & DM_STM32H7_OTG_HCCHAR_MPS_MASK;
        packets_consumed = actual_length / max_packet_size;
        if (actual_length % max_packet_size) {
            ++packets_consumed;
        }
        if (host->gahbcfg & DM_STM32H7_OTG_GAHBCFG_DMAEN) {
            state->hcdma += actual_length;
        }
        remaining -= actual_length;
        packet_count -= packets_consumed < packet_count ?
                        packets_consumed : packet_count;
        if (packets_consumed) {
            state->next_pid = dm_stm32h7_otg_host_next_pid(state->next_pid);
        }
        state->hctsiz &= ~(DM_STM32H7_OTG_HCTSIZ_XFERSIZE_MASK |
                           DM_STM32H7_OTG_HCTSIZ_PKTCNT_MASK);
        state->hctsiz |= remaining |
                         (packet_count <<
                          DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
        if (actual_length < state->issued_length || !remaining ||
            !packet_count) {
            dm_stm32h7_otg_host_halt_channel(
                host, channel, DM_STM32H7_OTG_HCINT_XFRC);
        }
        break;
    case DM_STM32H7_OTG_HOST_CHANNEL_NAK:
        state->hcint |= DM_STM32H7_OTG_HCINT_NAK;
        if (((state->hcchar & DM_STM32H7_OTG_HCCHAR_EPTYPE_MASK) >>
             DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) != 0 &&
            ((state->hcchar & DM_STM32H7_OTG_HCCHAR_EPTYPE_MASK) >>
             DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) != 2) {
            dm_stm32h7_otg_host_halt_channel(host, channel, 0);
        } else {
            dm_stm32h7_otg_host_update_channel_irq(host, channel);
        }
        break;
    case DM_STM32H7_OTG_HOST_CHANNEL_STALL:
        dm_stm32h7_otg_host_halt_channel(host, channel,
                                          DM_STM32H7_OTG_HCINT_STALL);
        break;
    case DM_STM32H7_OTG_HOST_CHANNEL_TRANSACTION_ERROR:
        dm_stm32h7_otg_host_halt_channel(host, channel,
                                          DM_STM32H7_OTG_HCINT_XACTERR);
        break;
    }
    dm_stm32h7_otg_host_update_irq(host);
}

void dm_stm32h7_otg_host_complete_channel(
    DmStm32H7OtgHost *host, unsigned channel,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length)
{
    dm_stm32h7_otg_host_complete_channel_internal(
        host, channel, 0, completion, actual_length);
}

void dm_stm32h7_otg_host_complete_channel_with_token(
    DmStm32H7OtgHost *host, unsigned channel, uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length)
{
    if (!completion_token) {
        return;
    }
    dm_stm32h7_otg_host_complete_channel_internal(
        host, channel, completion_token, completion, actual_length);
}

void dm_stm32h7_otg_host_init(DmStm32H7OtgHost *host,
                               DmUsbHostPortReset *port_reset,
                               void *port_opaque,
                               DmStm32H7OtgHostIrq *irq,
                               void *irq_opaque)
{
    if (!host) {
        return;
    }

    memset(host, 0, sizeof(*host));
    dm_usb_host_port_init(&host->port, port_reset, port_opaque);
    host->irq = irq;
    host->irq_opaque = irq_opaque;
    dm_stm32h7_otg_host_reset(host);
}

void dm_stm32h7_otg_host_set_port_connected(
    DmStm32H7OtgHost *host, bool connected, DmStm32H7OtgPortSpeed speed)
{
    if (!host || connected == !!(host->hprt0 & DM_STM32H7_OTG_HPRT0_CONNSTS)) {
        return;
    }

    if (connected) {
        host->hprt0 &= ~(DM_STM32H7_OTG_HPRT0_SPD_MASK |
                         DM_STM32H7_OTG_HPRT0_ENA |
                         DM_STM32H7_OTG_HPRT0_RST);
        host->hprt0 |= DM_STM32H7_OTG_HPRT0_CONNSTS |
                       DM_STM32H7_OTG_HPRT0_CONNDET |
                       ((uint32_t)speed << DM_STM32H7_OTG_HPRT0_SPD_SHIFT);
    } else {
        host->hprt0 &= ~(DM_STM32H7_OTG_HPRT0_SPD_MASK |
                         DM_STM32H7_OTG_HPRT0_RST |
                         DM_STM32H7_OTG_HPRT0_ENA |
                         DM_STM32H7_OTG_HPRT0_CONNSTS);
        host->hprt0 |= DM_STM32H7_OTG_HPRT0_CONNDET |
                       DM_STM32H7_OTG_HPRT0_ENACHG;
        dm_stm32h7_otg_host_cancel_active_channels(host);
        host->sof_running = false;
    }
    dm_stm32h7_otg_host_update_irq(host);
}

uint32_t dm_stm32h7_otg_host_read(const DmStm32H7OtgHost *host,
                                   uint32_t offset)
{
    if (!host) {
        return 0;
    }

    switch (offset) {
    case DM_STM32H7_OTG_GAHBCFG:
        return host->gahbcfg;
    case DM_STM32H7_OTG_GINTSTS:
        return host->gintsts;
    case DM_STM32H7_OTG_GINTMSK:
        return host->gintmsk;
    case DM_STM32H7_OTG_HCFG:
        return host->hcfg;
    case DM_STM32H7_OTG_HFIR:
        return host->hfir;
    case DM_STM32H7_OTG_HFNUM:
        return host->hfnum;
    case DM_STM32H7_OTG_HAINT:
        return host->haint;
    case DM_STM32H7_OTG_HAINTMSK:
        return host->haintmsk;
    case DM_STM32H7_OTG_HPRT0:
        return host->hprt0;
    default:
        break;
    }

    if (offset >= DM_STM32H7_OTG_HCCHAR(0) &&
        offset < DM_STM32H7_OTG_HCCHAR(DM_STM32H7_OTG_HOST_CHANNELS)) {
        unsigned channel = (offset - DM_STM32H7_OTG_HCCHAR(0)) / 0x20u;

        switch ((offset - DM_STM32H7_OTG_HCCHAR(0)) & 0x1cu) {
        case 0:
            return host->channel[channel].hcchar;
        case 0x8:
            return host->channel[channel].hcint;
        case 0xc:
            return host->channel[channel].hcintmsk;
        case 0x10:
            return host->channel[channel].hctsiz;
        case 0x14:
            return host->channel[channel].hcdma;
        default:
            break;
        }
    }
    {
        unsigned channel;

        if (dm_stm32h7_otg_host_hcfifo_channel(offset, &channel)) {
            return dm_stm32h7_otg_host_fifo_read((DmStm32H7OtgHost *)host,
                                                 channel, 4);
        }
    }
    return 0;
}

static void dm_stm32h7_otg_host_write_channel(DmStm32H7OtgHost *host,
                                               unsigned channel,
                                               uint32_t register_offset,
                                               uint32_t value,
                                               uint64_t timestamp_ns)
{
    DmStm32H7OtgHostChannel *state = &host->channel[channel];
    uint32_t old = state->hcchar;

    switch (register_offset) {
    case 0:
        if ((value & DM_STM32H7_OTG_HCCHAR_CHDIS) &&
            !(old & DM_STM32H7_OTG_HCCHAR_CHDIS)) {
            if ((old & DM_STM32H7_OTG_HCCHAR_CHENA) &&
                state->waiting_completion && host->channel_cancel) {
                host->channel_cancel(host->channel_cancel_opaque, host,
                                     channel, state->completion_token);
            }
            state->hcchar = value & ~(DM_STM32H7_OTG_HCCHAR_CHENA |
                                       DM_STM32H7_OTG_HCCHAR_CHDIS);
            dm_stm32h7_otg_host_halt_channel(
                host, channel, 0);
        } else if ((value & DM_STM32H7_OTG_HCCHAR_CHENA) &&
                   !(old & DM_STM32H7_OTG_HCCHAR_CHENA)) {
            state->hcchar = value & ~DM_STM32H7_OTG_HCCHAR_CHDIS;
            state->active = true;
            state->waiting_completion = false;
            state->next_pid = (state->hctsiz &
                               DM_STM32H7_OTG_HCTSIZ_PID_MASK) >>
                              DM_STM32H7_OTG_HCTSIZ_PID_SHIFT;
            dm_stm32h7_otg_host_service_channel(host, channel, timestamp_ns);
        } else {
            state->hcchar = value | (old & DM_STM32H7_OTG_HCCHAR_CHENA);
        }
        break;
    case 0x8:
        state->hcint &= ~(value & DM_STM32H7_OTG_HCINT_VALID_MASK);
        dm_stm32h7_otg_host_update_channel_irq(host, channel);
        break;
    case 0xc:
        state->hcintmsk = value & DM_STM32H7_OTG_HCINT_VALID_MASK;
        dm_stm32h7_otg_host_update_channel_irq(host, channel);
        break;
    case 0x10:
        state->hctsiz = value & (DM_STM32H7_OTG_HCTSIZ_DOPNG |
                                 DM_STM32H7_OTG_HCTSIZ_PID_MASK |
                                 DM_STM32H7_OTG_HCTSIZ_PKTCNT_MASK |
                                 DM_STM32H7_OTG_HCTSIZ_XFERSIZE_MASK);
        break;
    case 0x14:
        state->hcdma = value;
        break;
    default:
        return;
    }
    dm_stm32h7_otg_host_update_irq(host);
}

static void dm_stm32h7_otg_host_write_hprt0(DmStm32H7OtgHost *host,
                                             uint32_t value,
                                             uint64_t timestamp_ns)
{
    uint32_t old = host->hprt0;

    if ((old & DM_STM32H7_OTG_HPRT0_PWR) &&
        !(value & DM_STM32H7_OTG_HPRT0_PWR)) {
        dm_stm32h7_otg_host_cancel_active_channels(host);
    }
    host->hprt0 &= ~(value & DM_STM32H7_OTG_HPRT0_CHANGE_MASK);
    host->hprt0 = (host->hprt0 & ~DM_STM32H7_OTG_HPRT0_PWR) |
                  (value & DM_STM32H7_OTG_HPRT0_PWR);

    if (value & DM_STM32H7_OTG_HPRT0_RST) {
        host->hprt0 |= DM_STM32H7_OTG_HPRT0_RST;
        host->sof_running = false;
    } else if (old & DM_STM32H7_OTG_HPRT0_RST) {
        host->hprt0 &= ~DM_STM32H7_OTG_HPRT0_RST;
        if (host->hprt0 & DM_STM32H7_OTG_HPRT0_CONNSTS) {
            host->hprt0 |= DM_STM32H7_OTG_HPRT0_ENA |
                           DM_STM32H7_OTG_HPRT0_ENACHG;
            dm_usb_host_port_reset(&host->port, timestamp_ns);
            dm_stm32h7_otg_host_start_sof(host, timestamp_ns);
        }
    } else if (!dm_stm32h7_otg_host_port_enabled(host)) {
        host->sof_running = false;
    }
    dm_stm32h7_otg_host_update_irq(host);
}

void dm_stm32h7_otg_host_write(DmStm32H7OtgHost *host, uint32_t offset,
                                uint32_t value, uint64_t timestamp_ns)
{
    if (!host) {
        return;
    }

    switch (offset) {
    case DM_STM32H7_OTG_GAHBCFG:
        host->gahbcfg = value;
        break;
    case DM_STM32H7_OTG_GINTSTS:
        host->gintsts &= ~(value & (DM_STM32H7_OTG_GINTSTS_PRTINT |
                                    DM_STM32H7_OTG_GINTSTS_SOF));
        break;
    case DM_STM32H7_OTG_GINTMSK:
        host->gintmsk = value;
        break;
    case DM_STM32H7_OTG_HCFG:
        host->hcfg = value;
        break;
    case DM_STM32H7_OTG_HFIR:
        host->hfir = value;
        break;
    case DM_STM32H7_OTG_HAINTMSK:
        host->haintmsk = value & DM_STM32H7_OTG_HOST_CHANNEL_MASK;
        break;
    case DM_STM32H7_OTG_HPRT0:
        dm_stm32h7_otg_host_write_hprt0(host, value, timestamp_ns);
        return;
    default:
        {
            unsigned channel;

            if (dm_stm32h7_otg_host_hcfifo_channel(offset, &channel)) {
                dm_stm32h7_otg_host_fifo_write(host, channel, value, 4);
                return;
            }
        }
        if (offset >= DM_STM32H7_OTG_HCCHAR(0) &&
            offset < DM_STM32H7_OTG_HCCHAR(DM_STM32H7_OTG_HOST_CHANNELS)) {
            dm_stm32h7_otg_host_write_channel(
                host, (offset - DM_STM32H7_OTG_HCCHAR(0)) / 0x20u,
                (offset - DM_STM32H7_OTG_HCCHAR(0)) & 0x1cu,
                value, timestamp_ns);
        }
        return;
    }
    dm_stm32h7_otg_host_update_irq(host);
}
