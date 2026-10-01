/* Component-only VMState contract for the board-independent USART data path. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_uart.h"
#include "migration/vmstate.h"

static bool uart_fifo_state_valid(uint16_t head, uint16_t length,
                                  unsigned capacity)
{
    return head < capacity && length <= capacity;
}

static int dm_mc02_uart_post_load(void *opaque, int version_id)
{
    DmMc02Uart *state = opaque;

    if (version_id != 1 ||
        !uart_fifo_state_valid(state->rx_fifo_head, state->rx_fifo_len,
                               DM_MC02_UART_RX_FIFO_SIZE) ||
        !uart_fifo_state_valid(state->rx_wire_head, state->rx_wire_len,
                               DM_MC02_UART_RX_WIRE_FIFO_SIZE) ||
        !uart_fifo_state_valid(state->tx_fifo_head, state->tx_fifo_len,
                               DM_MC02_UART_TX_FIFO_SIZE) ||
        (state->rx_next_ns && !state->rx_wire_len) ||
        (state->idle_next_ns && state->rx_wire_len) ||
        (state->tx_next_ns && !state->tx_fifo_len) ||
        state->rx_next_ns > INT64_MAX ||
        state->idle_next_ns > INT64_MAX ||
        state->tx_next_ns > INT64_MAX ||
        state->dma_tx_next_ns > INT64_MAX) {
        return -EINVAL;
    }

    /* Chardev, clock, DMA/DMAMUX, GPIO/RS485 and IRQ handles are destination
     * runtime wiring.  Rebuild only timers and baud timing after validation. */
    dm_mc02_uart_sync_runtime(state);
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_uart = {
    .name = "dm-mc02-uart",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_uart_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Uart, DM_MC02_UART_REGION_SIZE),
        VMSTATE_UINT8_ARRAY(rx_fifo, DmMc02Uart, DM_MC02_UART_RX_FIFO_SIZE),
        VMSTATE_UINT16(rx_fifo_head, DmMc02Uart),
        VMSTATE_UINT16(rx_fifo_len, DmMc02Uart),
        VMSTATE_UINT8_ARRAY(rx_wire_fifo, DmMc02Uart,
                            DM_MC02_UART_RX_WIRE_FIFO_SIZE),
        VMSTATE_UINT16(rx_wire_head, DmMc02Uart),
        VMSTATE_UINT16(rx_wire_len, DmMc02Uart),
        VMSTATE_UINT64(rx_dropped, DmMc02Uart),
        VMSTATE_UINT64(rx_next_ns, DmMc02Uart),
        VMSTATE_UINT8_ARRAY(tx_fifo, DmMc02Uart, DM_MC02_UART_TX_FIFO_SIZE),
        VMSTATE_UINT16(tx_fifo_head, DmMc02Uart),
        VMSTATE_UINT16(tx_fifo_len, DmMc02Uart),
        VMSTATE_UINT64(tx_dropped, DmMc02Uart),
        VMSTATE_UINT64(tx_short_writes, DmMc02Uart),
        VMSTATE_UINT64(tx_next_ns, DmMc02Uart),
        VMSTATE_UINT64(idle_next_ns, DmMc02Uart),
        VMSTATE_UINT64(dma_tx_next_ns, DmMc02Uart),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_uart_vmstate(void)
{
    return &vmstate_dm_mc02_uart;
}
