/* STM32H7 DWC2 host-port register subset, independent of board wiring. */
#ifndef HW_USB_DM_STM32H7_OTG_HOST_H
#define HW_USB_DM_STM32H7_OTG_HOST_H

#include "hw/usb/dm_usb_host_port.h"
#include "hw/usb/dm_usb_host_pio_fifo.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_STM32H7_OTG_GAHBCFG   0x008
#define DM_STM32H7_OTG_GINTSTS   0x014
#define DM_STM32H7_OTG_GINTMSK   0x018
#define DM_STM32H7_OTG_HCFG      0x400
#define DM_STM32H7_OTG_HFIR      0x404
#define DM_STM32H7_OTG_HFNUM     0x408
#define DM_STM32H7_OTG_HAINT     0x414
#define DM_STM32H7_OTG_HAINTMSK  0x418
#define DM_STM32H7_OTG_HPRT0     0x440
#define DM_STM32H7_OTG_HCCHAR(channel) \
    (0x500u + 0x20u * (channel))
#define DM_STM32H7_OTG_HCINT(channel) \
    (0x508u + 0x20u * (channel))
#define DM_STM32H7_OTG_HCINTMSK(channel) \
    (0x50cu + 0x20u * (channel))
#define DM_STM32H7_OTG_HCTSIZ(channel) \
    (0x510u + 0x20u * (channel))
#define DM_STM32H7_OTG_HCDMA(channel) \
    (0x514u + 0x20u * (channel))
#define DM_STM32H7_OTG_HCFIFO(channel) \
    (0x1000u + 0x1000u * (channel))

#define DM_STM32H7_OTG_HOST_CHANNELS 12

#define DM_STM32H7_OTG_GAHBCFG_GINT      (1u << 0)
#define DM_STM32H7_OTG_GAHBCFG_DMAEN     (1u << 5)
#define DM_STM32H7_OTG_GINTSTS_SOF       (1u << 3)
#define DM_STM32H7_OTG_GINTSTS_PRTINT    (1u << 24)
#define DM_STM32H7_OTG_GINTSTS_HCINT     (1u << 25)

#define DM_STM32H7_OTG_HCFG_RESVALID     (2u << 8)
#define DM_STM32H7_OTG_HFIR_RESET         60000u
#define DM_STM32H7_OTG_HFNUM_FRNUM_MASK   0xffffu
#define DM_STM32H7_OTG_SOF_FULL_SPEED_NS  1000000ull
#define DM_STM32H7_OTG_SOF_HIGH_SPEED_NS  125000ull

#define DM_STM32H7_OTG_HPRT0_SPD_SHIFT    17
#define DM_STM32H7_OTG_HPRT0_SPD_MASK     (3u << 17)
#define DM_STM32H7_OTG_HPRT0_PWR          (1u << 12)
#define DM_STM32H7_OTG_HPRT0_RST          (1u << 8)
#define DM_STM32H7_OTG_HPRT0_OVRCURRCHG   (1u << 5)
#define DM_STM32H7_OTG_HPRT0_OVRCURRACT   (1u << 4)
#define DM_STM32H7_OTG_HPRT0_ENACHG       (1u << 3)
#define DM_STM32H7_OTG_HPRT0_ENA          (1u << 2)
#define DM_STM32H7_OTG_HPRT0_CONNDET      (1u << 1)
#define DM_STM32H7_OTG_HPRT0_CONNSTS      (1u << 0)

#define DM_STM32H7_OTG_HCCHAR_CHENA       (1u << 31)
#define DM_STM32H7_OTG_HCCHAR_CHDIS       (1u << 30)
#define DM_STM32H7_OTG_HCCHAR_ODDFRM      (1u << 29)
#define DM_STM32H7_OTG_HCCHAR_DEVADDR_SHIFT 22
#define DM_STM32H7_OTG_HCCHAR_DEVADDR_MASK (0x7fu << 22)
#define DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT 18
#define DM_STM32H7_OTG_HCCHAR_EPTYPE_MASK (3u << 18)
#define DM_STM32H7_OTG_HCCHAR_LSPDDEV     (1u << 17)
#define DM_STM32H7_OTG_HCCHAR_EPDIR       (1u << 15)
#define DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT 11
#define DM_STM32H7_OTG_HCCHAR_EPNUM_MASK  (0xfu << 11)
#define DM_STM32H7_OTG_HCCHAR_MPS_MASK    0x7ffu

#define DM_STM32H7_OTG_HCINT_XFRC         (1u << 0)
#define DM_STM32H7_OTG_HCINT_CHHLTD       (1u << 1)
#define DM_STM32H7_OTG_HCINT_STALL        (1u << 3)
#define DM_STM32H7_OTG_HCINT_NAK          (1u << 4)
#define DM_STM32H7_OTG_HCINT_XACTERR      (1u << 7)
#define DM_STM32H7_OTG_HCINT_VALID_MASK   0x3fffu

#define DM_STM32H7_OTG_HCTSIZ_DOPNG       (1u << 31)
#define DM_STM32H7_OTG_HCTSIZ_PID_SHIFT   29
#define DM_STM32H7_OTG_HCTSIZ_PID_MASK    (3u << 29)
#define DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT 19
#define DM_STM32H7_OTG_HCTSIZ_PKTCNT_MASK (0x3ffu << 19)
#define DM_STM32H7_OTG_HCTSIZ_XFERSIZE_MASK 0x7ffffu

typedef enum DmStm32H7OtgPortSpeed {
    DM_STM32H7_OTG_PORT_HIGH_SPEED = 0,
    DM_STM32H7_OTG_PORT_FULL_SPEED = 1,
    DM_STM32H7_OTG_PORT_LOW_SPEED = 2,
} DmStm32H7OtgPortSpeed;

typedef void DmStm32H7OtgHostIrq(void *opaque, bool level);

typedef enum DmStm32H7OtgHostChannelPid {
    DM_STM32H7_OTG_HOST_PID_DATA0 = 0,
    DM_STM32H7_OTG_HOST_PID_DATA2 = 1,
    DM_STM32H7_OTG_HOST_PID_DATA1 = 2,
    DM_STM32H7_OTG_HOST_PID_SETUP = 3,
} DmStm32H7OtgHostChannelPid;

typedef enum DmStm32H7OtgHostChannelCompletion {
    DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED,
    DM_STM32H7_OTG_HOST_CHANNEL_NAK,
    DM_STM32H7_OTG_HOST_CHANNEL_STALL,
    DM_STM32H7_OTG_HOST_CHANNEL_TRANSACTION_ERROR,
} DmStm32H7OtgHostChannelCompletion;

typedef struct DmStm32H7OtgHost DmStm32H7OtgHost;

typedef struct DmStm32H7OtgHostChannelRequest {
    uint8_t channel;
    uint8_t device_address;
    uint8_t endpoint;
    uint8_t endpoint_type;
    bool direction_in;
    bool low_speed;
    uint16_t max_packet_size;
    uint32_t transfer_size;
    uint16_t packet_count;
    DmStm32H7OtgHostChannelPid pid;
    uint64_t timestamp_ns;
    uint64_t completion_token;
} DmStm32H7OtgHostChannelRequest;

typedef void DmStm32H7OtgHostChannelStart(
    void *opaque, const DmStm32H7OtgHostChannelRequest *request);
typedef void DmStm32H7OtgHostChannelCancel(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token);

typedef struct DmStm32H7OtgHostChannel {
    uint32_t hcchar;
    uint32_t hcint;
    uint32_t hcintmsk;
    uint32_t hctsiz;
    uint32_t hcdma;
    /* HCTSIZ.DPID seeds this automatic per-packet toggle state. */
    DmStm32H7OtgHostChannelPid next_pid;
    uint32_t issued_length;
    uint64_t completion_token;
    bool active;
    bool waiting_completion;
} DmStm32H7OtgHostChannel;

struct DmStm32H7OtgHost {
    DmUsbHostPort port;
    DmStm32H7OtgHostIrq *irq;
    void *irq_opaque;
    uint32_t gahbcfg;
    uint32_t gintsts;
    uint32_t gintmsk;
    uint32_t hcfg;
    uint32_t hfir;
    uint32_t hfnum;
    uint32_t haint;
    uint32_t haintmsk;
    uint32_t hprt0;
    DmStm32H7OtgHostChannel channel[DM_STM32H7_OTG_HOST_CHANNELS];
    DmUsbHostPioFifo out_fifo[DM_STM32H7_OTG_HOST_CHANNELS];
    DmUsbHostPioFifo in_fifo[DM_STM32H7_OTG_HOST_CHANNELS];
    DmStm32H7OtgHostChannelStart *channel_start;
    void *channel_opaque;
    DmStm32H7OtgHostChannelCancel *channel_cancel;
    void *channel_cancel_opaque;
    uint64_t next_sof_ns;
    uint64_t next_channel_completion_token;
    bool sof_running;
    bool irq_level;
};

void dm_stm32h7_otg_host_init(DmStm32H7OtgHost *host,
                               DmUsbHostPortReset *port_reset,
                               void *port_opaque,
                               DmStm32H7OtgHostIrq *irq,
                               void *irq_opaque);
void dm_stm32h7_otg_host_reset(DmStm32H7OtgHost *host);

void dm_stm32h7_otg_host_set_port_connected(
    DmStm32H7OtgHost *host, bool connected, DmStm32H7OtgPortSpeed speed);
void dm_stm32h7_otg_host_set_channel_start(
    DmStm32H7OtgHost *host, DmStm32H7OtgHostChannelStart *channel_start,
    void *channel_opaque);
void dm_stm32h7_otg_host_set_channel_cancel(
    DmStm32H7OtgHost *host, DmStm32H7OtgHostChannelCancel *channel_cancel,
    void *channel_cancel_opaque);
void dm_stm32h7_otg_host_advance_time(DmStm32H7OtgHost *host,
                                      uint64_t timestamp_ns);
bool dm_stm32h7_otg_host_service_channel(DmStm32H7OtgHost *host,
                                         unsigned channel,
                                         uint64_t timestamp_ns);
void dm_stm32h7_otg_host_complete_channel(
    DmStm32H7OtgHost *host, unsigned channel,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length);
void dm_stm32h7_otg_host_complete_channel_with_token(
    DmStm32H7OtgHost *host, unsigned channel, uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length);
void dm_stm32h7_otg_host_fifo_write(DmStm32H7OtgHost *host,
                                    unsigned channel, uint32_t value,
                                    unsigned size);
uint32_t dm_stm32h7_otg_host_fifo_read(DmStm32H7OtgHost *host,
                                       unsigned channel, unsigned size);
bool dm_stm32h7_otg_host_read_out_fifo(void *opaque, unsigned channel,
                                        uint8_t *data, uint32_t length);
bool dm_stm32h7_otg_host_write_in_fifo(void *opaque, unsigned channel,
                                       const uint8_t *data, uint32_t length);
uint32_t dm_stm32h7_otg_host_read(const DmStm32H7OtgHost *host,
                                   uint32_t offset);
void dm_stm32h7_otg_host_write(DmStm32H7OtgHost *host, uint32_t offset,
                                uint32_t value, uint64_t timestamp_ns);

#endif /* HW_USB_DM_STM32H7_OTG_HOST_H */
