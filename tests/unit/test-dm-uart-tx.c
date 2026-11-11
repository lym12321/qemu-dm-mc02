/*
 * USART timing: STM32H7 8N1 sends ten bits per frame; 2 Mbaud => 5000 ns.
 * Exercise real UART/timer/CharBackend code with late host timer service.
 */
#include "qemu/osdep.h"
#include "chardev/char.h"
#include "hw/arm/dm_mc02_uart.h"
#include "qemu/main-loop.h"
#include "qemu/module.h"
#include "sysemu/cpu-timers.h"

#define TYPE_CAPTURE "dm-uart-tx-capture"
#define FRAME_NS 5000
#define CR1 0x00
#define BRR 0x0c
#define ISR 0x1c
#define TDR 0x28
#define TC (1u << 6)

static int64_t now_ns;

int64_t cpu_get_clock(void)
{
    return now_ns;
}

/* DMA is deliberately unattached; the native qtest covers the real DMA path. */
bool dm_mc02_dma_request(DmMc02Dma *s, const DmMc02Dmamux *mux,
                         uint32_t id, hwaddr addr)
{
    g_assert_not_reached();
}

bool dm_mc02_dma_request_batch(DmMc02Dma *s, const DmMc02Dmamux *mux,
                               uint32_t id, hwaddr addr, unsigned count)
{
    g_assert_not_reached();
}

bool dm_mc02_dma_request_endpoint(DmMc02Dma *s, const DmMc02Dmamux *mux,
                                  uint32_t id, hwaddr addr,
                                  const DmMc02DmaEndpoint *ep, uint64_t stamp)
{
    g_assert_not_reached();
}

bool dm_mc02_dma_request_endpoint_batch(DmMc02Dma *s,
                                        const DmMc02Dmamux *mux,
                                        uint32_t id, hwaddr addr,
                                        const DmMc02DmaEndpoint *ep,
                                        uint64_t stamp, unsigned count)
{
    g_assert_not_reached();
}

typedef struct Capture {
    Chardev parent;
    uint8_t bytes[8192];
    unsigned length, calls;
    int results[4];
    unsigned result_count;
} Capture;

typedef struct Fixture {
    DmMc02Uart uart;
    Capture *capture;
    Clock *clock;
    qemu_irq irq;
    int irq_level;
} Fixture;

static int capture_write(Chardev *chr, const uint8_t *bytes, int length)
{
    Capture *c = (Capture *)chr;
    int result = c->calls < c->result_count ? c->results[c->calls] : length;

    c->calls++;
    g_assert_cmpint(result, <=, length);
    if (result < 0) {
        errno = EAGAIN;
    } else {
        g_assert_cmpuint(c->length + result, <=, sizeof(c->bytes));
        memcpy(c->bytes + c->length, bytes, result);
        c->length += result;
    }
    return result;
}

static void capture_class_init(ObjectClass *klass, void *data)
{
    CHARDEV_CLASS(klass)->chr_write = capture_write;
}

static const TypeInfo capture_type = {
    .name = TYPE_CAPTURE,
    .parent = TYPE_CHARDEV,
    .instance_size = sizeof(Capture),
    .class_init = capture_class_init,
};

static void set_irq(void *opaque, int n, int level)
{
    Fixture *f = opaque;
    f->irq_level = level;
}

static void write_reg(Fixture *f, hwaddr offset, uint32_t value)
{
    f->uart.iomem.ops->write(f->uart.iomem.opaque, offset, value, 4);
}

static void assert_complete(Fixture *f, bool complete)
{
    uint32_t isr = f->uart.iomem.ops->read(f->uart.iomem.opaque, ISR, 4);
    g_assert_cmpint(!!(isr & TC), ==, complete);
    g_assert_cmpint(f->irq_level, ==, complete);
}

static void setup(Fixture *f, const void *data)
{
    now_ns = 1000;
    f->capture = (Capture *)object_new(TYPE_CAPTURE);
    qemu_chr_be_event(CHARDEV(f->capture), CHR_EVENT_OPENED);
    g_assert_true(dm_mc02_uart_init(&f->uart, NULL, "uart-tx",
                                    CHARDEV(f->capture), &error_abort));
    f->clock = clock_new(OBJECT(f->capture), "uart-kernel");
    clock_set_hz(f->clock, 64000000);
    dm_mc02_uart_set_kernel_clock(&f->uart, f->clock);
    f->irq = qemu_allocate_irq(set_irq, f, 0);
    dm_mc02_uart_set_irq(&f->uart, f->irq);
    write_reg(f, BRR, 32);
    write_reg(f, CR1, (1u << 3) | (1u << 6)); /* TE + TCIE (CMSIS). */
}

static void teardown(Fixture *f, const void *data)
{
    dm_mc02_uart_cleanup(&f->uart);
    qemu_free_irq(f->irq);
    object_unref(OBJECT(f->capture));
    g_assert_false(qemu_clock_has_timers(QEMU_CLOCK_VIRTUAL));
}

static void advance(int64_t offset)
{
    now_ns = 1000 + offset;
    qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL);
}

static void put_bytes(Fixture *f, const uint8_t *bytes, unsigned length)
{
    for (unsigned i = 0; i < length; i++) {
        write_reg(f, TDR, bytes[i]);
    }
}

static void test_late_service(Fixture *f, const void *data)
{
    const uint8_t bytes[] = {0, 1, 2, 3, 4, 5};

    put_bytes(f, bytes, sizeof(bytes));
    advance(4 * FRAME_NS + 2345);
    g_assert_cmpmem(f->capture->bytes, f->capture->length, bytes, 4);
    g_assert_cmpuint(f->capture->calls, ==, 1);
    assert_complete(f, false);
    advance(5 * FRAME_NS - 1);
    g_assert_cmpuint(f->capture->length, ==, 4);
    advance(5 * FRAME_NS);
    g_assert_cmpuint(f->capture->length, ==, 5);
    advance(6 * FRAME_NS);
    g_assert_cmpmem(f->capture->bytes, f->capture->length,
                    bytes, sizeof(bytes));
    assert_complete(f, true);
}

static void test_new_ingress(Fixture *f, const void *data)
{
    const uint8_t bytes[] = {0xa5, 0x00, 0x5a};

    put_bytes(f, bytes, 2);
    /* New MMIO arrives before the overdue timer callback gets CPU time. */
    now_ns += 10 * FRAME_NS;
    put_bytes(f, bytes + 2, 1);
    advance(11 * FRAME_NS - 1);
    g_assert_cmpmem(f->capture->bytes, f->capture->length, bytes, 2);
    assert_complete(f, false);
    advance(11 * FRAME_NS);
    g_assert_cmpmem(f->capture->bytes, f->capture->length,
                    bytes, sizeof(bytes));
    assert_complete(f, true);
}

static void test_short_write_wrap(Fixture *f, const void *data)
{
    uint8_t bytes[DM_MC02_UART_TX_FIFO_SIZE + 4];
    const unsigned prefix = DM_MC02_UART_TX_FIFO_SIZE - 2;
    int64_t start;

    for (unsigned i = 0; i < sizeof(bytes); i++) {
        bytes[i] = i * 37 + 11;
    }
    put_bytes(f, bytes, prefix);
    advance(prefix * FRAME_NS);
    g_assert_cmpmem(f->capture->bytes, f->capture->length, bytes, prefix);
    put_bytes(f, bytes + prefix, sizeof(bytes) - prefix);
    /* Cross the ring end, then see zero/EAGAIN/short writes in the suffix. */
    f->capture->calls = 0;
    f->capture->results[0] = 2;
    f->capture->results[1] = 0;
    f->capture->results[2] = -1;
    f->capture->results[3] = 1;
    f->capture->result_count = 4;
    start = (prefix + 6) * FRAME_NS;
    advance(start);
    advance(start + 1000);
    advance(start + 2000 + 3 * FRAME_NS);
    assert_complete(f, false);
    advance(start + 3000 + 7 * FRAME_NS);
    g_assert_cmpmem(f->capture->bytes, f->capture->length,
                    bytes, sizeof(bytes));
    assert_complete(f, true);
}

int main(int argc, char **argv)
{
    module_call_init(MODULE_INIT_QOM);
    type_register_static(&capture_type);
    g_assert_cmpint(qemu_init_main_loop(&error_abort), ==, 0);
    qemu_clock_enable(QEMU_CLOCK_VIRTUAL, true);
    g_test_init(&argc, &argv, NULL);
    g_test_add("/dm-uart-tx/late-service", Fixture, NULL,
               setup, test_late_service, teardown);
    g_test_add("/dm-uart-tx/new-ingress", Fixture, NULL,
               setup, test_new_ingress, teardown);
    g_test_add("/dm-uart-tx/short-write-wrap", Fixture, NULL,
               setup, test_short_write_wrap, teardown);
    return g_test_run();
}
