/*
 * Minimal STM32H7 USART model.
 *
 * This first slice provides the status semantics needed by STM32 HAL during
 * initialization and polling transmit.  An optional QEMU chardev provides a
 * bounded RX wire queue, virtual-time RX delivery, and non-blocking host TX;
 * without one, the register model remains self-contained.
 */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_uart.h"
#include "qemu/timer.h"

#define USART_CR1  0x00
#define USART_CR2  0x04
#define USART_CR3  0x08
#define USART_BRR  0x0c
#define USART_RTOR 0x14
#define USART_RQR  0x18
#define USART_ISR  0x1c
#define USART_ICR  0x20
#define USART_RDR  0x24
#define USART_TDR  0x28
#define USART_PRESC 0x2c

#define USART_CR1_RE   (1u << 2)
#define USART_CR1_TE   (1u << 3)
#define USART_CR1_OVER8 (1u << 15)
#define USART_CR1_IDLEIE (1u << 4)
#define USART_CR3_DMAR (1u << 6)
#define USART_CR3_DMAT (1u << 7)
#define USART_CR3_DEM  (1u << 14)
#define USART_CR3_DEP  (1u << 15)
#define USART_ISR_TC   (1u << 6)
#define USART_ISR_TXE  (1u << 7)
#define USART_ISR_RXNE (1u << 5)
#define USART_ISR_IDLE (1u << 4)
#define USART_ISR_ORE  (1u << 3)
#define USART_ISR_REACK (1u << 22)
#define USART_ISR_TEACK (1u << 21)
#define USART_ICR_IDLECF (1u << 4)
#define USART_ICR_ORECF  (1u << 3)
#define USART_CR3_EIE    (1u << 0)
#define DM_MC02_UART_DMA_BATCH_MAX 32

static uint32_t uart_load(const DmMc02Uart *s, hwaddr offset,
                          unsigned size);
static void uart_store(DmMc02Uart *s, hwaddr offset, uint64_t value,
                       unsigned size);
static void uart_dma_tx_tick(void *opaque);
static bool uart_tx_allowed(const DmMc02Uart *s);
static void uart_tx_flush(void *opaque);
static void uart_tx_timer_tick(void *opaque);
static void uart_dma_tx_kick(DmMc02Uart *s);
static void uart_update_tx_timing(DmMc02Uart *s);
static void uart_update_rx_timing(DmMc02Uart *s);
static void uart_rx_timer_tick(void *opaque);
static void uart_idle_timer_tick(void *opaque);
static void uart_rx_drain_immediate(DmMc02Uart *s);
static void uart_rx_kick(DmMc02Uart *s);
static bool uart_dma_rx_request(DmMc02Uart *s);
static void uart_kernel_clock_changed(void *opaque, ClockEvent event);
static DmUartTiming uart_current_timing(const DmMc02Uart *s);
static bool uart_dma_endpoint_read(void *opaque, uint8_t *data,
                                   unsigned size, uint64_t timestamp_ns);
static DmMc02DmaEndpointResult uart_dma_endpoint_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns);
static void uart_dma_endpoint_read_commit(void *opaque);
static void uart_dma_endpoint_read_abort(void *opaque);
static DmMc02DmaEndpointResult uart_dma_endpoint_write_ex(
    void *opaque, const uint8_t *data, unsigned size, uint64_t timestamp_ns);

static uint64_t uart_deadline_after(uint64_t delay_ns)
{
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (now >= INT64_MAX || delay_ns >= (uint64_t)INT64_MAX - now) {
        return INT64_MAX;
    }
    return now + delay_ns;
}

static void uart_timer_del(QEMUTimer *timer, uint64_t *deadline_ns)
{
    if (timer) {
        timer_del(timer);
    }
    if (deadline_ns) {
        *deadline_ns = 0;
    }
}

static void uart_timer_mod(QEMUTimer *timer, uint64_t *deadline_ns,
                           uint64_t deadline)
{
    if (!timer) {
        if (deadline_ns) {
            *deadline_ns = 0;
        }
        return;
    }
    if (deadline > INT64_MAX) {
        deadline = INT64_MAX;
    }
    if (deadline_ns) {
        *deadline_ns = deadline;
    }
    timer_mod_ns(timer, (int64_t)deadline);
}

static void uart_timer_mod_after(QEMUTimer *timer, uint64_t *deadline_ns,
                                 uint64_t delay_ns)
{
    uart_timer_mod(timer, deadline_ns, uart_deadline_after(delay_ns));
}

static void uart_tx_schedule(DmMc02Uart *s, uint64_t delay_ns)
{
    if (s->tx_timer && s->tx_fifo_len != 0) {
        uart_timer_mod_after(s->tx_timer, &s->tx_next_ns, delay_ns);
    } else if (s->tx_fifo_len == 0) {
        uart_timer_del(s->tx_timer, &s->tx_next_ns);
    }
}

static void uart_kernel_clock_changed(void *opaque, ClockEvent event)
{
    DmMc02Uart *s = opaque;

    if (event == ClockUpdate && s->kernel_clock) {
        s->kernel_clock_hz = clock_get_hz(s->kernel_clock);
        uart_update_tx_timing(s);
    }
}

static void uart_update_tx_timing(DmMc02Uart *s)
{
    uart_timer_del(s->tx_timer, &s->tx_next_ns);
    s->tx_timing = uart_current_timing(s);
    uart_update_rx_timing(s);
    if (s->tx_fifo_len != 0) {
        uart_tx_flush(s);
    }
}

static DmUartTiming uart_current_timing(const DmMc02Uart *s)
{
    uint32_t cr1 = uart_load(s, USART_CR1, 4);
    uint32_t presc = uart_load(s, USART_PRESC, 4);
    uint32_t brr = uart_load(s, USART_BRR, 4);

    return dm_uart_timing_calculate(
        s->kernel_clock_hz, brr, presc & 0xfu,
        (cr1 & USART_CR1_OVER8) != 0);
}

static void uart_tx_flush(void *opaque)
{
    DmMc02Uart *s = opaque;

    if (s->tx_fifo_len == 0) {
        uart_timer_del(s->tx_timer, &s->tx_next_ns);
        uart_store(s, USART_ISR,
                   uart_load(s, USART_ISR, 4) | USART_ISR_TC, 4);
        uart_dma_tx_kick(s);
        return;
    }
    if (!uart_tx_allowed(s) || !qemu_chr_fe_backend_open(&s->chr)) {
        return;
    }
    if (s->tx_timing.valid) {
        if (s->tx_timer && !timer_pending(s->tx_timer)) {
            uart_tx_schedule(s, s->tx_timing.frame_duration_ns);
        }
        return;
    }
    while (s->tx_fifo_len != 0) {
        unsigned contiguous = MIN((unsigned)s->tx_fifo_len,
                                  DM_MC02_UART_TX_FIFO_SIZE -
                                  s->tx_fifo_head);
        int written = qemu_chr_fe_write(&s->chr,
                                        s->tx_fifo + s->tx_fifo_head,
                                        contiguous);

        if (written <= 0) {
            /* Backpressure must not block the emulated CPU or spin the
             * event loop.  Retry later in virtual time. */
            uart_tx_schedule(s, 1000);
            return;
        }
        if ((unsigned)written < contiguous) {
            s->tx_short_writes++;
        }
        s->tx_fifo_head = (s->tx_fifo_head + written) %
                          DM_MC02_UART_TX_FIFO_SIZE;
        s->tx_fifo_len -= written;
    }
    uart_timer_del(s->tx_timer, &s->tx_next_ns);
    /* TC means the emulated UART's host-facing transmit queue is empty.  A
     * queued byte is not complete merely because it was accepted by TDR. */
    uart_store(s, USART_ISR,
               uart_load(s, USART_ISR, 4) | USART_ISR_TC, 4);
    uart_dma_tx_kick(s);
}

static void uart_tx_timer_tick(void *opaque)
{
    DmMc02Uart *s = opaque;
    int written;

    s->tx_next_ns = 0;

    if (s->tx_fifo_len == 0) {
        uart_store(s, USART_ISR,
                   uart_load(s, USART_ISR, 4) | USART_ISR_TC, 4);
        uart_dma_tx_kick(s);
        return;
    }
    if (!s->tx_timing.valid) {
        uart_tx_flush(s);
        return;
    }
    if (!uart_tx_allowed(s) || !qemu_chr_fe_backend_open(&s->chr)) {
        return;
    }

    written = qemu_chr_fe_write(&s->chr, s->tx_fifo + s->tx_fifo_head, 1);
    if (written <= 0) {
        /* Preserve the frame boundary while allowing a non-blocking backend
         * to recover from temporary backpressure. */
        uart_tx_schedule(s, 1000);
        return;
    }

    s->tx_fifo_head = (s->tx_fifo_head + 1) % DM_MC02_UART_TX_FIFO_SIZE;
    s->tx_fifo_len--;
    if (s->tx_fifo_len != 0) {
        uart_tx_schedule(s, s->tx_timing.frame_duration_ns);
    } else {
        uart_store(s, USART_ISR,
                   uart_load(s, USART_ISR, 4) | USART_ISR_TC, 4);
    }
    uart_dma_tx_kick(s);
}

static void uart_tx_enqueue(DmMc02Uart *s, uint8_t byte)
{
    unsigned slot;

    if (s->tx_fifo_len == DM_MC02_UART_TX_FIFO_SIZE) {
        /* Preserve ordering by dropping the newest byte and expose the loss
         * through the counter for a future diagnostics surface. */
        s->tx_dropped++;
        return;
    }
    slot = (s->tx_fifo_head + s->tx_fifo_len) %
           DM_MC02_UART_TX_FIFO_SIZE;
    s->tx_fifo[slot] = byte;
    s->tx_fifo_len++;
    uart_tx_flush(s);
}

static void uart_update_irq(DmMc02Uart *s)
{
    uint32_t cr1 = uart_load(s, USART_CR1, 4);
    uint32_t cr3 = uart_load(s, USART_CR3, 4);
    uint32_t isr = uart_load(s, USART_ISR, 4);
    bool asserted = ((cr1 & USART_CR1_IDLEIE) &&
                     (isr & USART_ISR_IDLE)) ||
                    ((cr3 & USART_CR3_EIE) && (isr & USART_ISR_ORE));

    if (s->irq && (!s->irq_level_valid || s->irq_level != asserted)) {
        s->irq_level = asserted;
        s->irq_level_valid = true;
        qemu_set_irq(s->irq, asserted);
    }
}

static bool uart_tx_allowed(const DmMc02Uart *s)
{
    uint32_t cr3;

    if (!(uart_load(s, USART_CR1, 4) & USART_CR1_TE) ||
        (s->rs485_enabled && !s->transceiver_powered)) {
        return false;
    }
    if (!s->rs485_enabled) {
        return true;
    }

    cr3 = uart_load(s, USART_CR3, 4);
    switch (s->de_mode) {
    case DM_MC02_UART_DE_AUTO_USART:
        /* The USART asserts its AF DE output around the transmitted byte.
         * There is no bit-level timing in this model, so a TDR write is the
         * complete abstract transmission interval. DEP reverses the active
         * level selected by DEM, as on the STM32 USART. */
        {
            bool de_asserted = (cr3 & USART_CR3_DEM) != 0;

            if (cr3 & USART_CR3_DEP) {
                de_asserted = !de_asserted;
            }
            return de_asserted == s->de_active_high;
        }
    case DM_MC02_UART_DE_MANUAL_GPIO:
        /* The pin is a normal GPIO in this mode; DEM/DEP do not drive it. */
        return s->de_level == s->de_active_high;
    case DM_MC02_UART_DE_DISCONNECTED:
    default:
        return false;
    }
}

static bool uart_dma_tx_pending(const DmMc02Uart *s)
{
    uint32_t cr3;

    if (!s->dma_tx || !s->dmamux_tx) {
        return false;
    }
    cr3 = uart_load(s, USART_CR3, 4);
    return (cr3 & USART_CR3_DMAT) != 0;
}

static void uart_dma_tx_kick(DmMc02Uart *s)
{
    if (!uart_dma_tx_pending(s)) {
        uart_timer_del(s->dma_tx_timer, &s->dma_tx_next_ns);
        return;
    }
    if (!s->dma_tx_timer) {
        return;
    }
    uart_timer_mod_after(s->dma_tx_timer, &s->dma_tx_next_ns, 1);
}

static void uart_update_rxne(DmMc02Uart *s)
{
    uint32_t isr = uart_load(s, USART_ISR, 4);

    if (s->rx_fifo_len != 0) {
        isr |= USART_ISR_RXNE;
    } else {
        isr &= ~USART_ISR_RXNE;
    }
    uart_store(s, USART_ISR, isr, 4);
}

static bool uart_rx_allowed(const DmMc02Uart *s)
{
    return (uart_load(s, USART_CR1, 4) & USART_CR1_RE) &&
           (!s->rs485_enabled || s->transceiver_powered);
}

static bool uart_rx_wire_pop(DmMc02Uart *s, uint8_t *byte)
{
    if (!s || !byte || s->rx_wire_len == 0) {
        return false;
    }
    *byte = s->rx_wire_fifo[s->rx_wire_head];
    s->rx_wire_head = (s->rx_wire_head + 1) %
                      DM_MC02_UART_RX_WIRE_FIFO_SIZE;
    s->rx_wire_len--;
    return true;
}

static bool uart_rx_wire_push(DmMc02Uart *s, uint8_t byte)
{
    unsigned slot;

    if (s->rx_wire_len == DM_MC02_UART_RX_WIRE_FIFO_SIZE) {
        return false;
    }
    slot = (s->rx_wire_head + s->rx_wire_len) %
           DM_MC02_UART_RX_WIRE_FIFO_SIZE;
    s->rx_wire_fifo[slot] = byte;
    s->rx_wire_len++;
    return true;
}

static bool uart_rx_fifo_push(DmMc02Uart *s, uint8_t byte)
{
    unsigned slot;

    if (s->rx_fifo_len == DM_MC02_UART_RX_FIFO_SIZE) {
        return false;
    }
    slot = (s->rx_fifo_head + s->rx_fifo_len) % DM_MC02_UART_RX_FIFO_SIZE;
    s->rx_fifo[slot] = byte;
    s->rx_fifo_len++;
    uart_dma_rx_request(s);
    return true;
}

static void uart_idle_timer_cancel(DmMc02Uart *s)
{
    uart_timer_del(s->idle_timer, &s->idle_next_ns);
}

static void uart_idle_timer_arm(DmMc02Uart *s)
{
    if (s->idle_timer && s->rx_timing.valid && uart_rx_allowed(s) &&
        s->rx_wire_len == 0) {
        uart_timer_mod_after(s->idle_timer, &s->idle_next_ns,
                             s->rx_timing.frame_duration_ns);
    } else {
        uart_idle_timer_cancel(s);
    }
}

static void uart_rx_drain_immediate(DmMc02Uart *s)
{
    uint8_t byte;

    if (!uart_rx_allowed(s)) {
        return;
    }
    while (s->rx_wire_len != 0 &&
           s->rx_fifo_len != DM_MC02_UART_RX_FIFO_SIZE) {
        if (!uart_rx_wire_pop(s, &byte)) {
            break;
        }
        if (!uart_rx_fifo_push(s, byte)) {
            break;
        }
    }
    uart_update_rxne(s);
}

static void uart_rx_kick(DmMc02Uart *s)
{
    if (!s->rx_wire_len || !uart_rx_allowed(s)) {
        uart_timer_del(s->rx_timer, &s->rx_next_ns);
        return;
    }
    if (!s->rx_timing.valid) {
        uart_timer_del(s->rx_timer, &s->rx_next_ns);
        uart_rx_drain_immediate(s);
        return;
    }
    if (s->rx_timer && !timer_pending(s->rx_timer)) {
        uart_timer_mod_after(s->rx_timer, &s->rx_next_ns,
                             s->rx_timing.frame_duration_ns);
    }
}

static void uart_update_rx_timing(DmMc02Uart *s)
{
    bool idle_pending = s->idle_timer && timer_pending(s->idle_timer);

    uart_timer_del(s->rx_timer, &s->rx_next_ns);
    uart_idle_timer_cancel(s);
    s->rx_timing = uart_current_timing(s);
    uart_rx_kick(s);
    if (!s->rx_timing.valid) {
        uart_rx_drain_immediate(s);
    } else if (idle_pending) {
        uart_idle_timer_arm(s);
    }
}

static void uart_rx_timer_tick(void *opaque)
{
    DmMc02Uart *s = opaque;
    uint8_t byte;

    s->rx_next_ns = 0;

    if (!s->rx_timing.valid || !uart_rx_allowed(s)) {
        uart_rx_kick(s);
        return;
    }
    if (!uart_rx_wire_pop(s, &byte)) {
        return;
    }
    if (!uart_rx_fifo_push(s, byte)) {
        /* Keep the wire byte queued when the CPU-visible FIFO is full. */
        s->rx_wire_head = (s->rx_wire_head +
                           DM_MC02_UART_RX_WIRE_FIFO_SIZE - 1) %
                          DM_MC02_UART_RX_WIRE_FIFO_SIZE;
        s->rx_wire_fifo[s->rx_wire_head] = byte;
        s->rx_wire_len++;
        uart_update_rxne(s);
        return;
    }
    uart_update_rxne(s);
    uart_idle_timer_cancel(s);
    if (s->rx_wire_len != 0) {
        uart_rx_kick(s);
    } else {
        uart_idle_timer_arm(s);
    }
}

static void uart_idle_timer_tick(void *opaque)
{
    DmMc02Uart *s = opaque;

    s->idle_next_ns = 0;

    if (!s->rx_timing.valid || !uart_rx_allowed(s) ||
        s->rx_wire_len != 0) {
        return;
    }
    uart_store(s, USART_ISR,
               uart_load(s, USART_ISR, 4) | USART_ISR_IDLE, 4);
    uart_update_irq(s);
}

static bool uart_rx_pop(DmMc02Uart *s, uint8_t *byte)
{
    /* A direct DMA read owns the queue head until its destination write
     * commits.  A re-entrant CPU read must not steal that reserved byte. */
    if (!s || !byte || s->rx_dma_reserved || s->rx_fifo_len == 0) {
        return false;
    }
    *byte = s->rx_fifo[s->rx_fifo_head];
    s->rx_fifo_head = (s->rx_fifo_head + 1) % DM_MC02_UART_RX_FIFO_SIZE;
    s->rx_fifo_len--;
    uart_store(s, USART_RDR, *byte, 1);
    uart_update_rxne(s);
    return true;
}

static void uart_tdr_write(DmMc02Uart *s, uint8_t byte)
{
    /* Keep the CPU-visible data register coherent for both MMIO writes and
     * DMA endpoint writes.  The latter enters here without passing through
     * dm_mc02_uart_write(). */
    uart_store(s, USART_TDR, byte, 1);
    if (uart_tx_allowed(s) && s->chr_enabled &&
        qemu_chr_fe_backend_open(&s->chr)) {
        uart_tx_enqueue(s, byte);
    } else if (uart_tx_allowed(s) && s->chr_enabled) {
        /* A configured but closed backend has no destination.  Do not retain
         * stale data across reconnects; record the loss instead. */
        s->tx_dropped++;
    }
    /* A polled transmitter is immediately ready for the next byte. */
    uint32_t isr = uart_load(s, USART_ISR, 4);

    isr |= USART_ISR_TXE;
    if (s->tx_fifo_len == 0) {
        isr |= USART_ISR_TC;
    } else {
        isr &= ~USART_ISR_TC;
    }
    uart_store(s, USART_ISR, isr, 4);
}

/* Endpoint callbacks own only UART data-register side effects.  DMA remains
 * responsible for request routing, stream selection, address movement and
 * status/IRQ bookkeeping. */
static bool uart_dma_endpoint_read(void *opaque, uint8_t *data, unsigned size,
                                   uint64_t timestamp_ns)
{
    DmMc02Uart *s = opaque;

    (void)timestamp_ns;
    return size == 1 && uart_rx_pop(s, data);
}

static DmMc02DmaEndpointResult uart_dma_endpoint_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    DmMc02Uart *s = opaque;

    (void)timestamp_ns;
    if (!s || !data || size != 1 || s->rx_dma_reserved ||
        s->rx_fifo_len == 0) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }

    s->rx_dma_reserved = true;
    s->rx_dma_reserved_head = s->rx_fifo_head;
    s->rx_dma_reserved_byte = s->rx_fifo[s->rx_fifo_head];
    data[0] = s->rx_dma_reserved_byte;
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void uart_dma_endpoint_read_commit(void *opaque)
{
    DmMc02Uart *s = opaque;

    if (!s || !s->rx_dma_reserved) {
        return;
    }

    /* The callbacks are synchronous, so this mismatch is only a defensive
     * guard against a future re-entrant consumer.  Do not consume a byte
     * whose queue identity changed while it was reserved. */
    if (s->rx_fifo_len != 0 &&
        s->rx_fifo_head == s->rx_dma_reserved_head &&
        s->rx_fifo[s->rx_fifo_head] == s->rx_dma_reserved_byte) {
        s->rx_fifo_head = (s->rx_fifo_head + 1) % DM_MC02_UART_RX_FIFO_SIZE;
        s->rx_fifo_len--;
        uart_store(s, USART_RDR, s->rx_dma_reserved_byte, 1);
        uart_update_rxne(s);
        /* A full CPU FIFO may have stopped the wire timer.  Once the DMA
         * destination commits this byte, resume both virtual delivery and
         * any chardev which was flow-controlled by uart_can_receive(). */
        /* The invalid-baud compatibility path is already inside
         * uart_rx_drain_immediate().  Calling it again from the DMA commit
         * callback would recursively re-enter DMA arbitration for the rest
         * of the wire queue.  With a valid baud, the timer may have stopped
         * on a full CPU FIFO and must be restarted here. */
        if (s->rx_timing.valid) {
            uart_rx_kick(s);
        }
        if (s->chr_enabled) {
            qemu_chr_fe_accept_input(&s->chr);
        }
    }
    s->rx_dma_reserved = false;
}

static void uart_dma_endpoint_read_abort(void *opaque)
{
    DmMc02Uart *s = opaque;

    if (!s) {
        return;
    }
    /* The source queue was never advanced by prepare, so abort only releases
     * the reservation. */
    s->rx_dma_reserved = false;
}

static DmMc02DmaEndpointResult uart_dma_endpoint_write_ex(
    void *opaque, const uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    DmMc02Uart *s = opaque;

    (void)timestamp_ns;
    if (!s || !data || size != 1) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    if (s->tx_fifo_len == DM_MC02_UART_TX_FIFO_SIZE) {
        /* The DMA beat has not reached TDR yet.  Keep the source address,
         * NDTR and the DMA FIFO intact until TX service creates space. */
        return DM_MC02_DMA_ENDPOINT_RETRY;
    }
    uart_tdr_write(s, data[0]);
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static bool uart_dma_rx_request(DmMc02Uart *s)
{
    uint32_t cr3;

    if (!s->dma_rx || !s->dmamux_rx) {
        return false;
    }
    cr3 = uart_load(s, USART_CR3, 4);
    if (!(cr3 & USART_CR3_DMAR)) {
        return false;
    }
    if (s->dma_endpoint_enabled) {
        return dm_mc02_dma_request_endpoint(
            s->dma_rx, s->dmamux_rx, s->dma_rx_request_id,
            s->dma_rx_peripheral_addr, &s->dma_rx_endpoint,
            qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
    return dm_mc02_dma_request(s->dma_rx, s->dmamux_rx,
                               s->dma_rx_request_id,
                               s->dma_rx_peripheral_addr);
}

static void uart_dma_rx_drain(DmMc02Uart *s)
{
    if (!s->dma_rx || !s->dmamux_rx ||
        !(uart_load(s, USART_CR3, 4) & USART_CR3_DMAR)) {
        return;
    }
    while (s->rx_fifo_len != 0) {
        uint16_t before = s->rx_fifo_len;

        if (!uart_dma_rx_request(s) || s->rx_fifo_len == before) {
            break;
        }
    }
}

static void uart_dma_tx_tick(void *opaque)
{
    DmMc02Uart *s = opaque;
    bool moved = false;

    s->dma_tx_next_ns = 0;

    /* A one-item timer cadence made long DMA transfers pay one virtual-clock
     * heap operation per byte.  Keep each peripheral write observable, but
     * amortize request lookup and IRQ propagation over a small bounded batch.
     * The DMA layer still performs one address-space transaction per byte. */
    if (uart_dma_tx_pending(s)) {
        if (s->dma_endpoint_enabled) {
            moved = dm_mc02_dma_request_endpoint_batch(
                s->dma_tx, s->dmamux_tx, s->dma_tx_request_id,
                s->dma_tx_peripheral_addr, &s->dma_tx_endpoint,
                qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
                DM_MC02_UART_DMA_BATCH_MAX);
        } else {
            moved = dm_mc02_dma_request_batch(
                s->dma_tx, s->dmamux_tx, s->dma_tx_request_id,
                s->dma_tx_peripheral_addr, DM_MC02_UART_DMA_BATCH_MAX);
        }
    }
    if (moved) {
        s->dma_tx_started = true;
    }
    if (moved && uart_dma_tx_pending(s)) {
        /* Keep a timer boundary between bounded batches. */
        uart_timer_mod_after(s->dma_tx_timer, &s->dma_tx_next_ns, 1);
    } else if (!moved && uart_dma_tx_pending(s) && !s->dma_tx_started &&
               s->tx_fifo_len != DM_MC02_UART_TX_FIFO_SIZE) {
        /* The stream may be enabled after DMAT.  Retry without a tight host
         * loop while waiting for DMA EN/DMAMUX configuration. */
        uart_timer_mod_after(s->dma_tx_timer, &s->dma_tx_next_ns, 1000);
    }
}

static int uart_can_receive(void *opaque)
{
    DmMc02Uart *s = opaque;
    unsigned used;
    uint32_t isr;

    if (!uart_rx_allowed(s)) {
        return 0;
    }
    used = s->rx_fifo_len + s->rx_wire_len;
    if (used != DM_MC02_UART_RX_FIFO_SIZE +
                DM_MC02_UART_RX_WIRE_FIFO_SIZE) {
        return DM_MC02_UART_RX_FIFO_SIZE +
               DM_MC02_UART_RX_WIRE_FIFO_SIZE - used;
    }

    /* The chardev asks before it removes bytes from its own transport.  Admit
     * precisely one byte at the first full condition so uart_receive() can
     * make the loss visible as ISR.ORE.  Once ORE is latched, stop admitting
     * bytes until software acknowledges the error and a consumer frees room;
     * this avoids an unbounded drop loop when a host keeps its socket ready. */
    isr = uart_load(s, USART_ISR, 4);
    return (isr & USART_ISR_ORE) ? 0 : 1;
}

static void uart_receive(void *opaque, const uint8_t *buf, int size)
{
    DmMc02Uart *s = opaque;
    bool accepted = false;
    bool overrun = false;

    for (int i = 0; i < size; ++i) {
        if (!uart_rx_wire_push(s, buf[i])) {
            s->rx_dropped++;
            /* The wire queue is the bounded receiver-side staging area.  A
             * byte which cannot enter it is the first observable data loss;
             * report the USART overrun instead of silently exposing only a
             * diagnostic counter.  RX FIFO backpressure is different: the
             * virtual wire timer retains that byte and must not assert ORE. */
            overrun = true;
            continue;
        }
        accepted = true;
    }
    if (accepted) {
        uart_idle_timer_cancel(s);
        if (s->rx_timing.valid) {
            uart_rx_kick(s);
        } else {
            /* Keep the pre-timing compatibility behavior: bytes become
             * visible immediately when the baud configuration is invalid. */
            uart_rx_drain_immediate(s);
        }
    }
    if (accepted && !s->rx_timing.valid) {
        uint32_t isr = uart_load(s, USART_ISR, 4);

        uart_store(s, USART_ISR, isr | USART_ISR_IDLE, 4);
    }
    if (overrun) {
        uint32_t isr = uart_load(s, USART_ISR, 4);

        uart_store(s, USART_ISR, isr | USART_ISR_ORE, 4);
    }
    if (accepted || overrun) {
        uart_update_irq(s);
    }
    qemu_chr_fe_accept_input(&s->chr);
}

static void uart_event(void *opaque, QEMUChrEvent event)
{
    DmMc02Uart *s = opaque;

    if (event == CHR_EVENT_OPENED) {
        uart_tx_flush(s);
        uart_rx_kick(s);
        uart_dma_tx_kick(s);
    } else if (event == CHR_EVENT_CLOSED) {
        s->rx_fifo_head = 0;
        s->rx_fifo_len = 0;
        s->rx_wire_head = 0;
        s->rx_wire_len = 0;
        uart_timer_del(s->rx_timer, &s->rx_next_ns);
        uart_idle_timer_cancel(s);
        uart_timer_del(s->tx_timer, &s->tx_next_ns);
        uart_timer_del(s->dma_tx_timer, &s->dma_tx_next_ns);
        uart_update_rxne(s);
        uart_store(s, USART_ISR,
                   uart_load(s, USART_ISR, 4) & ~USART_ISR_IDLE, 4);
        uart_update_irq(s);
    }
}

static uint32_t uart_load(const DmMc02Uart *s, hwaddr offset,
                          unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)s->regs[offset + i] << (i * 8);
    }
    return value;
}

static void uart_store(DmMc02Uart *s, hwaddr offset, uint64_t value,
                       unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        s->regs[offset + i] = value >> (i * 8);
    }
}

static uint32_t uart_status(const DmMc02Uart *s)
{
    uint32_t cr1 = uart_load(s, USART_CR1, 4);
    uint32_t isr = uart_load(s, USART_ISR, 4);

    isr |= USART_ISR_TXE;
    if (s->tx_fifo_len == 0) {
        isr |= USART_ISR_TC;
    } else {
        isr &= ~USART_ISR_TC;
    }
    if (cr1 & USART_CR1_RE) {
        isr |= USART_ISR_REACK;
    }
    if (cr1 & USART_CR1_TE) {
        isr |= USART_ISR_TEACK;
    }
    return isr;
}

static uint64_t dm_mc02_uart_read(void *opaque, hwaddr offset,
                                  unsigned size)
{
    DmMc02Uart *s = opaque;

    if (size > 4 || offset >= DM_MC02_UART_REGION_SIZE ||
        size > DM_MC02_UART_REGION_SIZE - offset) {
        return 0;
    }
    if (offset == USART_ISR && size == 4) {
        return uart_status(s);
    }
    if (offset == USART_RDR) {
        uint32_t value = 0;

        (void)uart_rx_pop(s, (uint8_t *)&value);
        uart_rx_kick(s);
        if (s->chr_enabled) {
            /* A consumer has made capacity available.  Let a paused chardev
             * re-evaluate uart_can_receive(), without manufacturing input. */
            qemu_chr_fe_accept_input(&s->chr);
        }
        return value;
    }
    return uart_load(s, offset, size);
}

static void dm_mc02_uart_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Uart *s = opaque;
    uint32_t old_cr3;

    if (size > 4 || offset >= DM_MC02_UART_REGION_SIZE ||
        size > DM_MC02_UART_REGION_SIZE - offset) {
        return;
    }
    if (offset == USART_ICR) {
        uint32_t isr = uart_load(s, USART_ISR, 4);

        uint32_t clear = (uint32_t)value;

        if (clear & USART_ICR_IDLECF) {
            isr &= ~USART_ISR_IDLE;
        }
        if (clear & USART_ICR_ORECF) {
            isr &= ~USART_ISR_ORE;
        }
        uart_store(s, USART_ISR, isr, 4);
        uart_update_irq(s);
        return;
    }
    old_cr3 = offset == USART_CR3 ? uart_load(s, USART_CR3, 4) : 0;
    uart_store(s, offset, value, size);
    if (offset == USART_CR1 || offset == USART_BRR ||
        offset == USART_PRESC) {
        uart_update_tx_timing(s);
    }
    if (offset == USART_CR1 || offset == USART_CR3) {
        uart_update_irq(s);
    }
    if (offset == USART_CR1 && (value & USART_CR1_RE) && s->chr_enabled) {
        qemu_chr_fe_accept_input(&s->chr);
    }
    if (offset == USART_CR1 || offset == USART_CR3) {
        uart_dma_rx_drain(s);
        uart_rx_kick(s);
    }
    if (offset == USART_TDR) {
        uart_tdr_write(s, value);
    }
    if (offset == USART_CR3 &&
        (uart_load(s, USART_CR3, 4) & USART_CR3_DMAT)) {
        if (!(old_cr3 & USART_CR3_DMAT)) {
            s->dma_tx_started = false;
        }
        uart_dma_tx_kick(s);
    }
}

static const MemoryRegionOps dm_mc02_uart_ops = {
    .read = dm_mc02_uart_read,
    .write = dm_mc02_uart_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

bool dm_mc02_uart_init(DmMc02Uart *state, Object *owner, const char *name,
                       Chardev *chardev, Error **errp)
{
    memset(state, 0, sizeof(*state));
    state->transceiver_powered = true;
    state->dma_endpoint_enabled = true;
    state->dma_rx_endpoint.read = uart_dma_endpoint_read;
    state->dma_rx_endpoint.read_prepare = uart_dma_endpoint_read_prepare;
    state->dma_rx_endpoint.read_commit = uart_dma_endpoint_read_commit;
    state->dma_rx_endpoint.read_abort = uart_dma_endpoint_read_abort;
    state->dma_rx_endpoint.opaque = state;
    state->dma_tx_endpoint.write_ex = uart_dma_endpoint_write_ex;
    state->dma_tx_endpoint.opaque = state;
    state->dma_tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                       uart_dma_tx_tick, state);
    state->tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                   uart_tx_timer_tick, state);
    state->rx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                   uart_rx_timer_tick, state);
    state->idle_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                     uart_idle_timer_tick, state);
    uart_store(state, USART_ISR, USART_ISR_TXE | USART_ISR_TC, 4);
    memory_region_init_io(&state->iomem, owner, &dm_mc02_uart_ops, state,
                          name, DM_MC02_UART_REGION_SIZE);
    if (!chardev) {
        return true;
    }
    if (!qemu_chr_fe_init(&state->chr, chardev, errp)) {
        return false;
    }
    state->chr_enabled = true;
    qemu_chr_fe_set_handlers(&state->chr, uart_can_receive, uart_receive,
                             uart_event, NULL, state, NULL, true);
    return true;
}

void dm_mc02_uart_cleanup(DmMc02Uart *state)
{
    if (!state) {
        return;
    }
    if (state->kernel_clock) {
        clock_clear_callback(state->kernel_clock);
        state->kernel_clock = NULL;
        state->kernel_clock_hz = 0;
    }
    if (state->dma_tx_timer) {
        timer_del(state->dma_tx_timer);
        timer_free(state->dma_tx_timer);
        state->dma_tx_timer = NULL;
    }
    if (state->tx_timer) {
        timer_del(state->tx_timer);
        timer_free(state->tx_timer);
        state->tx_timer = NULL;
    }
    if (state->rx_timer) {
        timer_del(state->rx_timer);
        timer_free(state->rx_timer);
        state->rx_timer = NULL;
    }
    if (state->idle_timer) {
        timer_del(state->idle_timer);
        timer_free(state->idle_timer);
        state->idle_timer = NULL;
    }
    if (state->chr_enabled) {
        qemu_chr_fe_set_handlers(&state->chr, NULL, NULL, NULL, NULL, NULL,
                                 NULL, false);
        qemu_chr_fe_deinit(&state->chr, false);
        state->chr_enabled = false;
    }
}

void dm_mc02_uart_reset(DmMc02Uart *s)
{
    if (!s) {
        return;
    }
    memset(s->regs, 0, sizeof(s->regs));
    s->rx_fifo_head = 0;
    s->rx_fifo_len = 0;
    s->rx_dma_reserved = false;
    s->rx_dma_reserved_head = 0;
    s->rx_dma_reserved_byte = 0;
    s->rx_wire_head = 0;
    s->rx_wire_len = 0;
    s->rx_dropped = 0;
    s->tx_fifo_head = 0;
    s->tx_fifo_len = 0;
    s->tx_dropped = 0;
    s->tx_short_writes = 0;
    s->dma_tx_started = false;
    s->rx_next_ns = 0;
    s->idle_next_ns = 0;
    s->tx_next_ns = 0;
    s->dma_tx_next_ns = 0;
    s->irq_level = false;
    s->irq_level_valid = false;
    s->de_level = false;
    if (s->dma_tx_timer) {
        timer_del(s->dma_tx_timer);
    }
    if (s->tx_timer) {
        timer_del(s->tx_timer);
    }
    if (s->rx_timer) {
        timer_del(s->rx_timer);
    }
    uart_idle_timer_cancel(s);
    s->tx_timing = dm_uart_timing_calculate(
        s->kernel_clock_hz, 0, 0, false);
    s->rx_timing = dm_uart_timing_calculate(
        s->kernel_clock_hz, 0, 0, false);
    uart_store(s, USART_ISR, USART_ISR_TXE | USART_ISR_TC, 4);
    uart_update_irq(s);
}

void dm_mc02_uart_sync_runtime(DmMc02Uart *s)
{
    uint64_t rx_deadline;
    uint64_t idle_deadline;
    uint64_t tx_deadline;
    uint64_t dma_tx_deadline;
    uint64_t now;

    if (!s) {
        return;
    }

    /* Timer objects, chardevs, DMA channels, GPIO/RS485 state and IRQ lines
     * belong to the destination composition.  Preserve only their virtual
     * deadlines while rebuilding the execution-local objects. */
    rx_deadline = s->rx_next_ns;
    idle_deadline = s->idle_next_ns;
    tx_deadline = s->tx_next_ns;
    dma_tx_deadline = s->dma_tx_next_ns;
    uart_timer_del(s->rx_timer, &s->rx_next_ns);
    uart_idle_timer_cancel(s);
    uart_timer_del(s->tx_timer, &s->tx_next_ns);
    uart_timer_del(s->dma_tx_timer, &s->dma_tx_next_ns);

    s->tx_timing = uart_current_timing(s);
    s->rx_timing = s->tx_timing;
    s->irq_level = false;
    s->irq_level_valid = false;
    uart_update_irq(s);

    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (tx_deadline && s->tx_fifo_len && s->tx_timing.valid &&
        s->tx_timer && uart_tx_allowed(s) && s->chr_enabled &&
        qemu_chr_fe_backend_open(&s->chr)) {
        uart_timer_mod(s->tx_timer, &s->tx_next_ns,
                       tx_deadline < now ? now : tx_deadline);
    } else if (!tx_deadline && s->tx_fifo_len) {
        /* This also preserves the invalid-baud compatibility path. */
        uart_tx_flush(s);
    }

    if (rx_deadline && s->rx_wire_len && s->rx_timing.valid &&
        uart_rx_allowed(s) && s->rx_timer) {
        uart_timer_mod(s->rx_timer, &s->rx_next_ns,
                       rx_deadline < now ? now : rx_deadline);
    } else if (!rx_deadline && s->rx_wire_len) {
        uart_rx_kick(s);
    }

    if (idle_deadline && s->rx_timing.valid && uart_rx_allowed(s) &&
        s->rx_wire_len == 0 && s->idle_timer) {
        uart_timer_mod(s->idle_timer, &s->idle_next_ns,
                       idle_deadline < now ? now : idle_deadline);
    }

    if (dma_tx_deadline && uart_dma_tx_pending(s) && s->dma_tx_timer) {
        uart_timer_mod(s->dma_tx_timer, &s->dma_tx_next_ns,
                       dma_tx_deadline < now ? now : dma_tx_deadline);
    } else if (!dma_tx_deadline) {
        uart_dma_tx_kick(s);
    }
}

void dm_mc02_uart_set_powered(DmMc02Uart *s, bool powered)
{
    if (!s) {
        return;
    }
    s->transceiver_powered = powered;
    if (!powered) {
        s->rx_fifo_head = 0;
        s->rx_fifo_len = 0;
        s->rx_wire_head = 0;
        s->rx_wire_len = 0;
        /* Bytes already accepted by the UART but not delivered to the
         * external RS485 transceiver are lost when its rail is removed. */
        s->tx_fifo_head = 0;
        s->tx_fifo_len = 0;
        uart_timer_del(s->tx_timer, &s->tx_next_ns);
        uart_timer_del(s->rx_timer, &s->rx_next_ns);
        uart_idle_timer_cancel(s);
        uart_update_rxne(s);
    }
    if (s->chr_enabled) {
        qemu_chr_fe_accept_input(&s->chr);
    }
    if (powered) {
        uart_tx_flush(s);
        uart_rx_kick(s);
    }
}

void dm_mc02_uart_set_kernel_clock(DmMc02Uart *s, Clock *clock)
{
    if (s->kernel_clock) {
        clock_clear_callback(s->kernel_clock);
    }
    s->kernel_clock = clock;
    s->kernel_clock_hz = clock ? clock_get_hz(clock) : 0;
    uart_update_tx_timing(s);
    if (clock) {
        clock_set_callback(clock, uart_kernel_clock_changed, s,
                           ClockUpdate);
    }
}

void dm_mc02_uart_set_irq(DmMc02Uart *state, qemu_irq irq)
{
    state->irq = irq;
    state->irq_level_valid = false;
    uart_update_irq(state);
}

void dm_mc02_uart_set_rs485(DmMc02Uart *state, bool enabled,
                            bool de_active_high)
{
    state->rs485_enabled = enabled;
    state->de_active_high = de_active_high;
    if (enabled) {
        state->de_mode = DM_MC02_UART_DE_DISCONNECTED;
    }
}

void dm_mc02_uart_set_de_mode(DmMc02Uart *state, DmMc02UartDeMode mode)
{
    state->de_mode = mode;
    uart_tx_flush(state);
}

void dm_mc02_uart_set_de(DmMc02Uart *state, bool level)
{
    state->de_level = level;
    uart_tx_flush(state);
}

void dm_mc02_uart_set_dma_rx(DmMc02Uart *state, DmMc02Dma *dma,
                             const DmMc02Dmamux *dmamux,
                             uint32_t request_id, hwaddr peripheral_addr)
{
    state->dma_rx = dma;
    state->dmamux_rx = dmamux;
    state->dma_rx_request_id = request_id;
    state->dma_rx_peripheral_addr = peripheral_addr;
    uart_dma_rx_drain(state);
    uart_rx_kick(state);
}

void dm_mc02_uart_set_dma_tx(DmMc02Uart *state, DmMc02Dma *dma,
                             const DmMc02Dmamux *dmamux,
                             uint32_t request_id, hwaddr peripheral_addr)
{
    state->dma_tx = dma;
    state->dmamux_tx = dmamux;
    state->dma_tx_request_id = request_id;
    state->dma_tx_peripheral_addr = peripheral_addr;
    state->dma_tx_started = false;
    uart_dma_tx_kick(state);
}

void dm_mc02_uart_set_dma_endpoint(DmMc02Uart *state, bool enabled)
{
    state->dma_endpoint_enabled = enabled;
    uart_dma_rx_drain(state);
    uart_rx_kick(state);
    uart_dma_tx_kick(state);
}

void dm_mc02_uart_dma_tx_stream_enabled(DmMc02Uart *state)
{
    state->dma_tx_started = false;
    uart_dma_tx_kick(state);
}

void dm_mc02_uart_dma_rx_stream_enabled(DmMc02Uart *state)
{
    uart_dma_rx_drain(state);
    uart_rx_kick(state);
}
