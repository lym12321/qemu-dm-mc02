/* Actual guest PIO and chardev consumer. Hardware values: RM0468 Rev3,
 * Table 531, sections 62.11/62.14; no board descriptor responder. */
#include "qemu/osdep.h"
#include "libqtest.h"
#include "qemu/bswap.h"
#include <sys/socket.h>
#include <sys/un.h>
#include <poll.h>

#define U 0x40040000
#define ENABLE (1u << 31)
#define CNAK (1u << 26)
static QTestState *qts;
static int peer;
static void wr(unsigned off, uint32_t value) { qtest_writel(qts, U + off, value); }
static uint32_t rd(unsigned off) { return qtest_readl(qts, U + off); }
static void read_exact(void *buffer, size_t length)
{
    uint8_t *p = buffer;
    while (length) {
        struct pollfd fd = { .fd = peer, .events = POLLIN };
        g_assert_cmpint(poll(&fd, 1, 3000), ==, 1);
        ssize_t n = read(peer, p, length);
        g_assert_cmpint(n, >, 0);
        p += n; length -= n;
    }
}
static void request(unsigned kind, unsigned id, unsigned ep, bool in,
                    unsigned length, const uint8_t *setup)
{
    uint8_t b[36] = {0};
    stl_be_p(b, 32); stl_be_p(b + 4, kind); stl_be_p(b + 8, id);
    stl_be_p(b + 12, ep); stl_be_p(b + 16, in); stl_be_p(b + 20, length);
    if (setup) { memcpy(b + 28, setup, 8); }
    g_assert_cmpint(write(peer, b, sizeof(b)), ==, sizeof(b));
}
static void response(unsigned kind, unsigned id, int status,
                     const uint8_t *expected, unsigned length)
{
    uint8_t b[128];
    read_exact(b, 20);
    g_assert_cmpuint(ldl_be_p(b), ==, 16 + length);
    g_assert_cmpuint(ldl_be_p(b + 4), ==, kind);
    g_assert_cmpuint(ldl_be_p(b + 8), ==, id);
    g_assert_cmpint((int32_t)ldl_be_p(b + 12), ==, status);
    if (kind == 1) { g_assert_cmpuint(ldl_be_p(b + 16), ==, length); }
    read_exact(b, length);
    if (length) { g_assert_cmpmem(b, length, expected, length); }
}
static void wait_rx(void)
{
    gint64 deadline = g_get_monotonic_time() + 3000000;
    while (!(rd(0x14) & 16)) {
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
    }
}
static void test_guest_bridge(void)
{
    uint8_t setup[] = {0xc0, 0x77, 0, 0, 0, 0, 4, 0};
    response(4, 1, 0, NULL, 0);
    /* H723 profile: 9 IN/OUT endpoints, dedicated TX, 1024 words total. */
    g_assert_cmpuint((rd(0x48) >> 10) & 15, ==, 8);
    g_assert_cmpuint(rd(0x4c) >> 16, ==, 1024);
    g_assert_true(rd(0x50) & (1u << 25));
    wr(0x50, 0); g_assert_true(rd(0x50) & (1u << 25));
    wr(0x900 + 9*32, 0xffffffff); g_assert_cmpuint(rd(0x900 + 9*32), ==, 0);
    wr(8, 1); wr(0x804, 0); response(4, 1, 1, NULL, 0);
    request(3, 90, 0, false, 0, NULL); response(3, 90, 0, NULL, 0);
    g_assert_cmphex(rd(0x14), ==, (1u << 12) | (1u << 13));
    wr(0x14, 0xffffffff);
    wr(0x24, 256); wr(0x28, (16u << 16) | 256);
    for (unsigned i = 0; i < 15; i++) { wr(0x104 + i*4, 0); }
    wr(0x18, (1u << 19) | 16); wr(0x814, 9); wr(0x81c, 1u << 16);
    request(1, 91, 0, true, 4, setup);
    wr(0xb10, (1u << 29) | (1u << 19) | 8);
    wait_rx();
    g_assert_true(qtest_readl(qts, 0xe000e208) & (1u << 13)); /* IRQ77 */
    g_assert_cmphex(rd(0x20), ==, (6u << 17) | 128);
    g_assert_cmphex(rd(0x1000), ==, 0x000077c0);
    g_assert_cmphex(rd(0x1000), ==, 0x00040000);
    g_assert_cmphex(rd(0x20), ==, 4u << 17);
    g_assert_cmphex(rd(0xb08), ==, 8);
    wr(0xb08, 8);
    g_assert_false(rd(0x14) & (1u << 19));
    qtest_writel(qts, 0xe000e288, 1u << 13);
    g_assert_false(qtest_readl(qts, 0xe000e208) & (1u << 13));
    wr(0x910, (1u << 19) | 4); wr(0x900, ENABLE | CNAK);
    wr(0x1000, 0x12345678);
    wr(0xb10, 1u << 19); wr(0xb00, ENABLE | CNAK);
    response(1, 91, 0, (const uint8_t *)"\x78\x56\x34\x12", 4);
    wait_rx(); g_assert_cmphex(rd(0x20), ==, 2u << 17);
    g_assert_cmphex(rd(0x20), ==, 3u << 17);
    g_assert_cmphex(rd(0xb08), ==, 1);

    /* FIFO2 serves EP1 through TXFNUM; caller IDs must not reorder requests. */
    wr(0x108, (16u << 16) | 272);
    wr(0x920, 4 | (1u << 15) | (2u << 18) | (2u << 22));
    request(1, 99, 1, true, 4, NULL);
    request(1, 5, 1, true, 4, NULL);
    wr(0x930, (1u << 19) | 4); wr(0x920, rd(0x920) | ENABLE | CNAK);
    wr(0x3000, 0x64636261); response(1, 99, 0, (const uint8_t *)"abcd", 4);
    wr(0x930, (1u << 19) | 4); wr(0x920, rd(0x920) | ENABLE | CNAK);
    wr(0x3000, 0x68676665); response(1, 5, 0, (const uint8_t *)"efgh", 4);
    request(1, 7, 1, true, 4, NULL); request(2, 7, 0, false, 0, NULL);
    response(2, 7, -104, NULL, 0);
    request(2, 7, 0, false, 0, NULL); response(2, 7, -2, NULL, 0);
    /* No private test packet window survives in the production MMIO map. */
    g_assert_cmphex(rd(0x1f018), ==, 0);
}
int main(int argc, char **argv)
{
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    g_autofree char *dir = g_dir_make_tmp("dm-usb-qtest-XXXXXX", NULL);
    g_autofree char *path = g_build_filename(dir, "usb", NULL);
    g_test_init(&argc, &argv, NULL);
    GString *args = g_string_new("-machine dm-mc02");
    for (unsigned i = 0; i < 10; i++) { g_string_append(args, " -serial null"); }
    g_string_append_printf(args, " -chardev socket,id=usb,path=%s,server=on,wait=off -serial chardev:usb", path);
    qts = qtest_init(args->str); g_string_free(args, true);
    peer = socket(AF_UNIX, SOCK_STREAM, 0); g_assert_cmpint(peer, >=, 0);
    g_strlcpy(address.sun_path, path, sizeof(address.sun_path));
    g_assert_cmpint(connect(peer, (void *)&address, sizeof(address)), ==, 0);
    g_test_add_func("/dm-mc02/usb/guest-bridge-irq", test_guest_bridge);
    int result = g_test_run();
    close(peer); qtest_quit(qts); unlink(path); rmdir(dir);
    return result;
}
