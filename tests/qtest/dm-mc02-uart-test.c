/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"
#include "qemu/sockets.h"

#include "hw/arm/dm_mc02_uart.h"

#define USART1_BASE       0x40011000u
#define USART1_BRR        (USART1_BASE + 0x0cu)
#define USART1_CR1        (USART1_BASE + 0x00u)
#define USART1_CR3        (USART1_BASE + 0x08u)
#define USART1_ISR        (USART1_BASE + 0x1cu)
#define USART1_ICR        (USART1_BASE + 0x20u)
#define USART1_RDR        (USART1_BASE + 0x24u)
#define USART1_TDR        (USART1_BASE + 0x28u)
#define USART_CR1_RE      (1u << 2)
#define USART_CR1_TE      (1u << 3)
#define USART_CR3_EIE     (1u << 0)
#define USART_CR3_DMAT    (1u << 7)
#define USART_ISR_ORE     (1u << 3)
#define USART_ICR_ORECF   (1u << 3)

#define PPB_BASE          0xe000e000ull
#define NVIC_ISER1        (PPB_BASE + 0x104)
#define NVIC_ISPR1        (PPB_BASE + 0x204)
#define NVIC_ICPR1        (PPB_BASE + 0x284)
#define USART1_IRQ        37u

#define DMA1_BASE         0x40020000u
#define DMA1_LISR         (DMA1_BASE + 0x00u)
#define DMA1_S1CR         (DMA1_BASE + 0x28u)
#define DMA1_S1NDTR       (DMA1_BASE + 0x2cu)
#define DMA1_S1PAR        (DMA1_BASE + 0x30u)
#define DMA1_S1M0AR       (DMA1_BASE + 0x34u)
#define DMA1_S1FCR        (DMA1_BASE + 0x3cu)
#define DMA_CR_EN         (1u << 0)
#define DMA_CR_DIR_M2P    (1u << 6)
#define DMA_CR_MINC       (1u << 10)
#define DMA_FLAG_TEIF_S1  (1u << (6 + 3))

#define DMAMUX1_C1CR      0x40020804u
#define DMAMUX_USART1_TX 42u
#define RCC_D2CCIP2R     0x58024454u
#define DMA_SOURCE        0x20000100u
#define DMA_BAUD_DIVIDER 0xffffu
#define UART_RX_BAUD_DIVIDER 64u
#define UART_RX_FRAME_NS 10000u
#define RCC_BASE          0x58024400u

static void test_kernel_clock_sources(void)
{
    static const struct {
        unsigned source, hsi_div, apb1, apb2;
        uint64_t usart16, usart234578;
    } cases[] = {
        { 0, 1, 5, 4, 16000000, 8000000 },
        { 1, 1, 5, 4, 96000000, 96000000 },
        { 2, 1, 5, 4, 60000000, 60000000 },
        { 3, 1, 5, 4, 32000000, 32000000 },
        { 4, 1, 5, 4, 4000000, 4000000 },
        { 5, 1, 5, 4, 32768, 32768 },
        { 6, 1, 5, 4, 0, 0 },
        { 0, 1, 4, 5, 8000000, 16000000 },
        { 3, 2, 5, 4, 16000000, 16000000 },
    };
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* ST LL_RCC_GetUSARTClockFreq: PCLK2/PCLK1, PLL2Q, PLL3Q,
     * HSI, CSI, LSE. Ported from the redundant UART-clock shell fixture. */
    for (size_t i = 0; i < ARRAY_SIZE(cases); ++i) {
        QDict *response;
        const char *properties[] = {
            "usart16-kernel-clock-hz", "usart234578-kernel-clock-hz",
        };
        uint64_t expected[] = { cases[i].usart16, cases[i].usart234578 };

        qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
        qtest_writel(qts, RCC_BASE + 0x28,
                     2u | (2u << 4) | (2u << 12) | (2u << 20));
        qtest_writel(qts, RCC_BASE + 0x30, 39u | (3u << 16));
        qtest_writel(qts, RCC_BASE + 0x38, 15u | (1u << 16));
        qtest_writel(qts, RCC_BASE + 0x40, 19u | (3u << 16));
        qtest_writel(qts, RCC_BASE + 0x2c,
                     (1u << 17) | (1u << 20) | (1u << 23));
        qtest_writel(qts, RCC_BASE + 0x1c,
                     (cases[i].apb1 << 4) | (cases[i].apb2 << 8));
        qtest_writel(qts, RCC_BASE, 1u | (1u << 7) | (1u << 16) |
                     (1u << 24) | (1u << 26) | (1u << 28) |
                     (cases[i].hsi_div << 3));
        qtest_writel(qts, RCC_BASE + 0x70, 1u);
        qtest_writel(qts, RCC_D2CCIP2R,
                     (cases[i].source << 3) | cases[i].source);
        for (size_t j = 0; j < ARRAY_SIZE(properties); ++j) {
            response = qtest_qmp(qts,
                "{ 'execute': 'qom-get', 'arguments': { 'path': '/machine', "
                "'property': %s } }", properties[j]);
            g_assert_cmpuint(qdict_get_int(response, "return"), ==, expected[j]);
            qobject_unref(response);
        }
    }
    qtest_quit(qts);
}

static size_t read_ringbuf(QTestState *qts, uint8_t *buffer, size_t capacity)
{
    QDict *response = qtest_qmp(qts,
        "{ 'execute': 'ringbuf-read', 'arguments': "
        "{ 'device': 'uart1', 'size': %u, 'format': 'base64' } }",
        (unsigned)capacity);
    const char *encoded = qdict_get_str(response, "return");
    gsize decoded_size = 0;
    g_autofree guchar *decoded = g_base64_decode(encoded, &decoded_size);

    g_assert_cmpuint(decoded_size, <=, capacity);
    memcpy(buffer, decoded, decoded_size);
    qobject_unref(response);
    return decoded_size;
}

static void test_uart_dma_tx_backpressure(void)
{
    const unsigned count = DM_MC02_UART_TX_FIFO_SIZE + 2;
    const uint64_t frame_ns =
        (UINT64_C(10) * UINT64_C(1000000000) * DMA_BAUD_DIVIDER +
         UINT64_C(64000000) - 1) / UINT64_C(64000000);
    g_autofree uint8_t *transmit = g_malloc(count);
    g_autofree uint8_t *received = g_malloc(count);
    QTestState *qts;

    for (unsigned i = 0; i < count; ++i) {
        transmit[i] = (uint8_t)(i * 37u + 11u);
    }

    qts = qtest_initf("-machine dm-mc02,uart-dma-endpoint=on -nodefaults "
                      "-chardev ringbuf,id=uart1,size=8192 "
                      "-serial none -serial chardev:uart1");
    /* Select HSI for USART1/6/10 so the UART has a valid frame period. */
    qtest_writel(qts, RCC_D2CCIP2R, 3u << 3);

    qtest_memwrite(qts, DMA_SOURCE, transmit, count);
    qtest_writel(qts, USART1_BRR, DMA_BAUD_DIVIDER);
    qtest_writel(qts, USART1_CR1, USART_CR1_TE);
    qtest_writel(qts, DMAMUX1_C1CR, DMAMUX_USART1_TX);
    qtest_writel(qts, DMA1_S1NDTR, count);
    qtest_writel(qts, DMA1_S1PAR, USART1_TDR);
    qtest_writel(qts, DMA1_S1M0AR, DMA_SOURCE);
    qtest_writel(qts, DMA1_S1FCR, 0);
    qtest_writel(qts, DMA1_S1CR, DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC);
    qtest_writel(qts, USART1_CR3, USART_CR3_DMAT);

    /* The DMA batch fills the UART TX FIFO and then receives RETRY.  There
     * must be no transfer error, and the final two source bytes must remain
     * pending while no UART frame has drained yet. */
    qtest_clock_step(qts, 5 * 1000 * 1000);
    g_assert_cmphex(qtest_readl(qts, DMA1_S1NDTR), ==, 2);
    g_assert_cmphex(qtest_readl(qts, DMA1_S1PAR), ==, USART1_TDR);
    g_assert_cmphex(qtest_readl(qts, DMA1_S1M0AR), ==, DMA_SOURCE);
    g_assert_cmphex(qtest_readl(qts, DMA1_LISR) & DMA_FLAG_TEIF_S1, ==, 0);
    g_assert_cmphex(qtest_readl(qts, DMA1_S1CR) & DMA_CR_EN, ==, DMA_CR_EN);

    /* A UART frame creates space and must kick the stalled DMA stream.  Run
     * enough controlled virtual time for every queued frame to reach the
     * host chardev, then compare the complete wire sequence byte-for-byte. */
    qtest_clock_step(qts, (int64_t)frame_ns * (count + 1));
    g_assert_cmphex(qtest_readl(qts, DMA1_S1NDTR), ==, 0);
    size_t received_count = 0;

    for (unsigned attempt = 0; attempt < 8; ++attempt) {
        received_count += read_ringbuf(qts, received + received_count,
                                       count - received_count);
        if (received_count == count) {
            break;
        }
        qtest_clock_step(qts, frame_ns);
    }
    g_assert_cmpmem(received, count, transmit, count);

    qtest_quit(qts);
}

static void test_uart_rx_wire_overrun(void)
{
    const unsigned capacity = DM_MC02_UART_RX_FIFO_SIZE +
                              DM_MC02_UART_RX_WIRE_FIFO_SIZE;
    const uint32_t irq_mask = UINT32_C(1) << (USART1_IRQ % 32);
    g_autofree uint8_t *fill = g_malloc0(capacity);
    g_autofree char *socket_dir = NULL;
    g_autofree char *socket_path = NULL;
    QTestState *qts;
    int socket_fd;
    uint8_t next = 0xa5;

    socket_dir = g_dir_make_tmp("qtest-dm-mc02-uart-XXXXXX", NULL);
    g_assert_nonnull(socket_dir);
    socket_path = g_build_filename(socket_dir, "uart1.sock", NULL);

    qts = qtest_initf("-machine dm-mc02 -nodefaults "
                      "-chardev socket,id=uart1,path=%s,server=on,wait=off "
                      "-serial none -serial chardev:uart1", socket_path);
    socket_fd = unix_connect(socket_path, NULL);
    g_assert_cmpint(socket_fd, >=, 0);
    /* The connection event is asynchronous relative to qtest initialization. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'query-status' }");

    /* A valid 1 MHz UART clock moves one host byte to the CPU FIFO per 10 us.
     * Enable NVIC external IRQ 37 before the receive stream so the qtest can
     * observe the board routing as well as the USART's local ORE state. */
    qtest_writel(qts, RCC_D2CCIP2R, 3u << 3);
    qtest_writel(qts, USART1_BRR, UART_RX_BAUD_DIVIDER);
    qtest_writel(qts, NVIC_ISER1, irq_mask);
    qtest_writel(qts, USART1_CR1, USART_CR1_RE);

    for (unsigned i = 0; i < capacity; ++i) {
        fill[i] = (uint8_t)i;
    }
    g_assert_cmpint(qemu_send_full(socket_fd, fill, capacity), ==, capacity);
    qtest_qmp_assert_success(qts, "{ 'execute': 'query-status' }");

    /* Fill the CPU-visible queue while retaining a full wire queue.  The
     * following frame observes CPU backpressure and restores its byte to the
     * wire FIFO, so neither queue has lost data yet. */
    qtest_clock_step(qts, (uint64_t)UART_RX_FRAME_NS *
                     (DM_MC02_UART_RX_FIFO_SIZE + 1));

    /* The one-byte admission at a full staging queue must expose the first
     * dropped byte through ORE.  EIE is still off, so the local error must not
     * reach NVIC yet. */
    g_assert_cmpint(qemu_send_full(socket_fd, &next, 1), ==, 1);
    qtest_qmp_assert_success(qts, "{ 'execute': 'query-status' }");
    g_assert_cmphex(qtest_readl(qts, USART1_ISR) & USART_ISR_ORE, ==,
                    USART_ISR_ORE);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR1) & irq_mask, ==, 0);

    /* Enabling EIE with a latched ORE must project the already-present source
     * into the native NVIC wiring. */
    qtest_writel(qts, USART1_CR3, USART_CR3_EIE);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR1) & irq_mask, ==, irq_mask);

    /* ORECF is local W1C: it lowers the UART source but does not clear the
     * architecturally distinct NVIC pending state. */
    qtest_writel(qts, USART1_ICR, USART_ICR_ORECF);
    g_assert_cmphex(qtest_readl(qts, USART1_ISR) & USART_ISR_ORE, ==, 0);
    qtest_writel(qts, NVIC_ICPR1, irq_mask);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR1) & irq_mask, ==, 0);

    /* Draining RDR exposes one byte of space and asks the chardev to resume.
     * A subsequent host byte is accepted, and no new ORE is generated. */
    g_assert_cmphex(qtest_readb(qts, USART1_RDR), ==, fill[0]);
    next = 0x5a;
    g_assert_cmpint(qemu_send_full(socket_fd, &next, 1), ==, 1);
    qtest_qmp_assert_success(qts, "{ 'execute': 'query-status' }");
    g_assert_cmphex(qtest_readl(qts, USART1_ISR) & USART_ISR_ORE, ==, 0);

    close(socket_fd);
    unlink(socket_path);
    qtest_quit(qts);
    g_rmdir(socket_dir);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/uart/kernel-clock-sources",
                    test_kernel_clock_sources);
    g_test_add_func("/dm-mc02/uart/dma-tx-backpressure",
                    test_uart_dma_tx_backpressure);
    g_test_add_func("/dm-mc02/uart/rx-wire-overrun",
                    test_uart_rx_wire_overrun);
    return g_test_run();
}
