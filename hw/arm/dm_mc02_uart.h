/* Minimal STM32H7 USART register model for the DM-MC02 machine. */
#ifndef HW_ARM_DM_MC02_UART_H
#define HW_ARM_DM_MC02_UART_H

#include "chardev/char-fe.h"
#include "exec/memory.h"
#include "hw/clock.h"
#include "hw/char/dm_uart_timing.h"
#include "hw/arm/dm_mc02_dma.h"
#include "hw/irq.h"
#include "migration/vmstate.h"
#include "qapi/error.h"
#include "qemu/timer.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_UART_REGION_SIZE 0x400
#define DM_MC02_UART_RX_FIFO_SIZE 256
#define DM_MC02_UART_RX_WIRE_FIFO_SIZE 256
#define DM_MC02_UART_TX_FIFO_SIZE 4096

/* H723 USART kernel mux groups.  The group is an SoC property, while the
 * board profile decides which instance is present and routes it. */
typedef enum DmMc02UartKernelClockGroup {
    DM_MC02_UART_KERNEL_CLOCK_USART16,
    DM_MC02_UART_KERNEL_CLOCK_USART234578,
    DM_MC02_UART_KERNEL_CLOCK_GROUP_COUNT,
} DmMc02UartKernelClockGroup;

typedef enum DmMc02UartDeMode {
    /* The pin is not connected to a usable DE source. */
    DM_MC02_UART_DE_DISCONNECTED,
    /* USART drives the DE alternate-function pin while transmitting. */
    DM_MC02_UART_DE_AUTO_USART,
    /* Software drives the mapped DE pin as a normal GPIO using ODR. */
    DM_MC02_UART_DE_MANUAL_GPIO,
} DmMc02UartDeMode;

typedef struct DmMc02Uart {
    MemoryRegion iomem;
    Clock *kernel_clock;
    uint64_t kernel_clock_hz;
    uint8_t regs[DM_MC02_UART_REGION_SIZE];
    CharBackend chr;
    bool chr_enabled;
    /* Board-level RS485 driver-enable state. */
    bool rs485_enabled;
    DmMc02UartDeMode de_mode;
    bool de_level;
    bool de_active_high;
    bool transceiver_powered;
    uint8_t rx_fifo[DM_MC02_UART_RX_FIFO_SIZE];
    uint16_t rx_fifo_head;
    uint16_t rx_fifo_len;
    /* Synchronous direct-P2M DMA reservation.  The queue head remains
     * untouched until the destination memory write commits. */
    bool rx_dma_reserved;
    uint16_t rx_dma_reserved_head;
    uint8_t rx_dma_reserved_byte;
    /* Chardev bytes first enter the wire queue.  A valid baud configuration
     * moves one byte per virtual frame into the CPU-visible RX FIFO. */
    uint8_t rx_wire_fifo[DM_MC02_UART_RX_WIRE_FIFO_SIZE];
    uint16_t rx_wire_head;
    uint16_t rx_wire_len;
    uint64_t rx_dropped;
    DmUartTiming rx_timing;
    /* Absolute virtual deadline for the next wire-byte delivery. */
    uint64_t rx_next_ns;
    QEMUTimer *rx_timer;
    /* Absolute virtual deadline for the post-frame IDLE indication. */
    uint64_t idle_next_ns;
    QEMUTimer *idle_timer;
    /* Host-facing TX is buffered so chardev short writes cannot silently
     * lose bytes accepted by the guest UART. */
    uint8_t tx_fifo[DM_MC02_UART_TX_FIFO_SIZE];
    uint16_t tx_fifo_head;
    uint16_t tx_fifo_len;
    uint64_t tx_dropped;
    uint64_t tx_short_writes;
    DmUartTiming tx_timing;
    /* Absolute virtual deadline for the next host-facing TX byte. */
    uint64_t tx_next_ns;
    QEMUTimer *tx_timer;
    qemu_irq irq;
    /* One peripheral request per received byte.  The DMA engine performs the
     * RDR read, which consumes the FIFO entry. */
    DmMc02Dma *dma_rx;
    const DmMc02Dmamux *dmamux_rx;
    uint32_t dma_rx_request_id;
    hwaddr dma_rx_peripheral_addr;
    DmMc02Dma *dma_tx;
    const DmMc02Dmamux *dmamux_tx;
    uint32_t dma_tx_request_id;
    hwaddr dma_tx_peripheral_addr;
    DmMc02DmaEndpoint dma_rx_endpoint;
    DmMc02DmaEndpoint dma_tx_endpoint;
    bool dma_endpoint_enabled;
    /* Absolute virtual deadline for deferred TX-DMA request retry. */
    uint64_t dma_tx_next_ns;
    QEMUTimer *dma_tx_timer;
    bool dma_tx_started;
    bool irq_level;
    bool irq_level_valid;
} DmMc02Uart;

bool dm_mc02_uart_init(DmMc02Uart *state, Object *owner, const char *name,
                       Chardev *chardev, Error **errp);
void dm_mc02_uart_cleanup(DmMc02Uart *state);
void dm_mc02_uart_reset(DmMc02Uart *state);
void dm_mc02_uart_set_powered(DmMc02Uart *state, bool powered);

/* Rebuild execution-local timers and derived baud timing after component
 * state restore.  Chardev, DMA, GPIO/RS485, clock and IRQ wiring remain
 * caller-owned runtime state. */
void dm_mc02_uart_sync_runtime(DmMc02Uart *state);

/* Component-only state contract; machine-level migration is not registered. */
const VMStateDescription *dm_mc02_uart_vmstate(void);

/* Attach the H723 kernel clock consumed by this UART.  The clock object is
 * owned by the composing SoC/machine and must outlive the UART. */
void dm_mc02_uart_set_kernel_clock(DmMc02Uart *state, Clock *clock);

/* Attach the optional USART interrupt output. */
void dm_mc02_uart_set_irq(DmMc02Uart *state, qemu_irq irq);

/* Configure board-level RS485 TX gating.  When USART_CR3.DEM is set, the
 * USART's CR3.DEP bit selects the active polarity; otherwise the supplied
 * default polarity is used for the mapped DE GPIO. */
void dm_mc02_uart_set_rs485(DmMc02Uart *state, bool enabled,
                            bool de_active_high);

/* Update the DE source selected by the board's GPIO mode/AF configuration. */
void dm_mc02_uart_set_de_mode(DmMc02Uart *state, DmMc02UartDeMode mode);

/* Update the mapped RS485 DE GPIO level for manual GPIO mode. */
void dm_mc02_uart_set_de(DmMc02Uart *state, bool level);

/* Attach the board's fixed RX request mapping. */
void dm_mc02_uart_set_dma_rx(DmMc02Uart *state, DmMc02Dma *dma,
                             const DmMc02Dmamux *dmamux,
                             uint32_t request_id,
                             hwaddr peripheral_addr);

/* Attach the board's fixed TX request mapping.  The caller owns the DMA and
 * DMAMUX objects; they must outlive the UART. */
void dm_mc02_uart_set_dma_tx(DmMc02Uart *state, DmMc02Dma *dma,
                             const DmMc02Dmamux *dmamux,
                             uint32_t request_id,
                             hwaddr peripheral_addr);

/* Select the synchronous, direct, 8-bit endpoint path; false retains the
 * address-space compatibility path. */
void dm_mc02_uart_set_dma_endpoint(DmMc02Uart *state, bool enabled);

/* Notify the UART that its mapped TX stream was enabled.  This is optional
 * when CR3.DMAT is written after DMA EN; it is the explicit board-level
 * trigger when DMA EN is written first. */
void dm_mc02_uart_dma_tx_stream_enabled(DmMc02Uart *state);

/* Notify the UART that its mapped RX stream was enabled, so bytes which
 * arrived while the stream was disabled can be drained from the RX queues. */
void dm_mc02_uart_dma_rx_stream_enabled(DmMc02Uart *state);

#endif
