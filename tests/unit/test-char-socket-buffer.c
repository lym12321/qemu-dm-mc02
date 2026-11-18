#include "qemu/osdep.h"
#include "qemu/config-file.h"
#include "qemu/coroutine.h"
#include "qemu/main-loop.h"
#include "qemu/module.h"
#include "qemu/option.h"
#include "qemu/sockets.h"
#include "qapi/error.h"
#include "sysemu/sysemu.h"
#include "chardev/char-fe.h"
#include "chardev/char-socket.h"
#include "io/channel-buffer.h"

#define TYPE_TEST_BUFFER_CHANNEL "test-socket-buffer-channel"
OBJECT_DECLARE_SIMPLE_TYPE(TestBufferChannel, TEST_BUFFER_CHANNEL)

struct TestBufferChannel {
    QIOChannelBuffer parent;
    const ssize_t *results;
    size_t nresults;
    size_t calls;
    bool blocked;
};

static QIOChannelClass *test_buffer_channel_parent;

/* Script short writes and backpressure; the real buffer stores accepted data. */
static ssize_t test_buffer_writev(QIOChannel *ioc,
                                  const struct iovec *iov, size_t niov,
                                  int *fds, size_t nfds, int flags,
                                  Error **errp)
{
    TestBufferChannel *t = TEST_BUFFER_CHANNEL(ioc);
    struct iovec shortened;
    ssize_t result;

    g_assert_cmpuint(niov, ==, 1);
    g_assert_cmpuint(nfds, ==, 0);
    if (t->blocked) {
        t->calls++;
        return QIO_CHANNEL_ERR_BLOCK;
    }
    if (t->results) {
        g_assert_cmpuint(t->calls, <, t->nresults);
        result = t->results[t->calls++];
    } else {
        t->calls++;
        result = iov[0].iov_len;
    }
    if (result < 0) {
        if (result != QIO_CHANNEL_ERR_BLOCK) {
            error_setg_errno(errp, EPIPE, "Scripted write failure");
        }
        return result;
    }
    g_assert_cmpint(result, >, 0);
    g_assert_cmpuint(result, <=, iov[0].iov_len);
    shortened = iov[0];
    shortened.iov_len = result;
    return test_buffer_channel_parent->io_writev(ioc, &shortened, 1,
                                                 fds, nfds, flags, errp);
}

static void test_buffer_channel_class_init(ObjectClass *oc, void *data)
{
    test_buffer_channel_parent = QIO_CHANNEL_CLASS(object_class_get_parent(oc));
    QIO_CHANNEL_CLASS(oc)->io_writev = test_buffer_writev;
}

static const TypeInfo test_buffer_channel_type = {
    .name = TYPE_TEST_BUFFER_CHANNEL,
    .parent = TYPE_QIO_CHANNEL_BUFFER,
    .instance_size = sizeof(TestBufferChannel),
    .class_init = test_buffer_channel_class_init,
};

typedef struct BufferFixture {
    GMainContext *context;
    Chardev *chr;
    CharBackend be;
    QIOChannelSocket *peer;
    TestBufferChannel *output;
} BufferFixture;

static void buffer_fixture_connect(BufferFixture *f)
{
    SocketChardev *s = SOCKET_CHARDEV(f->chr);

    f->peer = qio_channel_socket_new();
    qio_channel_socket_connect_sync(f->peer, s->addr, &error_abort);
    qemu_chr_wait_connected(f->chr, &error_abort);
    g_assert_cmpint(s->state, ==, TCP_CHARDEV_STATE_CONNECTED);

    /* Keep the real connection lifecycle; replace only its output dependency. */
    f->output = TEST_BUFFER_CHANNEL(object_new(TYPE_TEST_BUFFER_CHANNEL));
    object_unref(OBJECT(s->ioc));
    s->ioc = QIO_CHANNEL(object_ref(f->output));
}

static void buffer_fixture_init(BufferFixture *f, bool enabled)
{
    QemuOpts *opts;
    g_autofree char *optstr = g_strdup_printf(
        "socket,id=buffer-test,host=127.0.0.1,port=0,server=on,wait=off,"
        "write-buffer=%s", enabled ? "on" : "off");

    f->context = g_main_context_new();
    opts = qemu_opts_parse_noisily(qemu_find_opts("chardev"), optstr, true);
    g_assert_nonnull(opts);
    f->chr = qemu_chr_new_from_opts(opts, f->context, &error_abort);
    qemu_opts_del(opts);
    g_assert_nonnull(f->chr);
    qemu_chr_fe_init(&f->be, f->chr, &error_abort);
    buffer_fixture_connect(f);
}

static void buffer_fixture_clear(BufferFixture *f)
{
    qemu_chr_fe_deinit(&f->be, true);
    object_unref(OBJECT(f->output));
    object_unref(OBJECT(f->peer));
    g_main_context_unref(f->context);
}

static void buffer_run_until_calls(BufferFixture *f, size_t calls)
{
    int64_t deadline = g_get_monotonic_time() + G_USEC_PER_SEC;

    while (f->output->calls < calls) {
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        g_main_context_iteration(f->context, false);
        g_usleep(100);
    }
}

static const uint8_t bytes[] = { 0x00, 0x13, 0xa5, 0x7f,
                                0x80, 0x5a, 0xfe, 0xff };

static void buffer_batch_test(void)
{
    BufferFixture f = { 0 };
    QIOChannelBuffer *output;
    size_t i;

    buffer_fixture_init(&f, true);
    output = QIO_CHANNEL_BUFFER(f.output);
    for (i = 0; i < sizeof(bytes); i++) {
        g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes + i, 1), ==, 1);
    }
    g_assert_cmpuint(f.output->calls, ==, 0);
    g_assert_cmpuint(output->usage, ==, 0);

    /* Host output progresses without advancing any guest clock. */
    buffer_run_until_calls(&f, 1);
    g_assert_cmpuint(f.output->calls, ==, 1);
    g_assert_cmpmem(output->data, output->usage, bytes, sizeof(bytes));
    buffer_fixture_clear(&f);
}

static void buffer_disabled_test(void)
{
    BufferFixture f = { 0 };

    buffer_fixture_init(&f, false);
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, 1), ==, 1);
    g_assert_cmpuint(f.output->calls, ==, 1);
    g_assert_cmpuint(QIO_CHANNEL_BUFFER(f.output)->usage, ==, 1);
    buffer_fixture_clear(&f);
}

static void buffer_backpressure_test(void)
{
    enum { CAPACITY = 16 * 1024 };
    static const ssize_t results[] = {
        QIO_CHANNEL_ERR_BLOCK, 3, QIO_CHANNEL_ERR_BLOCK, CAPACITY - 3, 3,
    };
    BufferFixture f = { 0 };
    QIOChannelBuffer *output;
    g_autofree uint8_t *data = g_malloc(CAPACITY + 1);
    size_t i;

    for (i = 0; i < CAPACITY + 1; i++) {
        data[i] = i * 13;
    }
    buffer_fixture_init(&f, true);
    output = QIO_CHANNEL_BUFFER(f.output);
    f.output->results = results;
    f.output->nresults = G_N_ELEMENTS(results);

    g_assert_cmpint(qemu_chr_fe_write(&f.be, data, CAPACITY + 1), ==, CAPACITY);
    g_assert_cmpuint(f.output->calls, ==, 0);
    errno = 0;
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, 1), ==, -1);
    g_assert_cmpint(errno, ==, EAGAIN);
    g_assert_cmpuint(output->usage, ==, 0);

    buffer_run_until_calls(&f, 3);
    g_assert_cmpuint(output->usage, ==, 3);
    /* Only the free prefix fits; the caller retains the unaccepted suffix. */
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, 4), ==, 3);
    buffer_run_until_calls(&f, G_N_ELEMENTS(results));
    g_assert_cmpuint(output->usage, ==, CAPACITY + 3);
    g_assert_cmpmem(output->data, CAPACITY, data, CAPACITY);
    g_assert_cmpmem(output->data + CAPACITY, 3, bytes, 3);
    buffer_fixture_clear(&f);
}

static void buffer_disconnect_test(void)
{
    BufferFixture f = { 0 };

    buffer_fixture_init(&f, true);
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, sizeof(bytes)), ==,
                    sizeof(bytes));
    qemu_chr_fe_disconnect(&f.be);
    g_assert_cmpuint(f.output->calls, ==, 0);
    object_unref(OBJECT(f.output));
    object_unref(OBJECT(f.peer));

    buffer_fixture_connect(&f);
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes + 5, 3), ==, 3);
    buffer_run_until_calls(&f, 1);
    g_assert_cmpmem(QIO_CHANNEL_BUFFER(f.output)->data,
                    QIO_CHANNEL_BUFFER(f.output)->usage, bytes + 5, 3);

    /* Removing a chardev must also cancel a pending host flush. */
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, 1), ==, 1);
    qemu_chr_fe_deinit(&f.be, true);
    g_usleep(2000);
    g_main_context_iteration(f.context, false);
    g_assert_cmpuint(f.output->calls, ==, 1);
    object_unref(OBJECT(f.output));
    object_unref(OBJECT(f.peer));
    g_main_context_unref(f.context);
}

static void buffer_context_test(void)
{
    BufferFixture f = { 0 };
    GMainContext *old_context;

    buffer_fixture_init(&f, true);
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, sizeof(bytes)), ==,
                    sizeof(bytes));
    old_context = f.context;
    f.context = g_main_context_new();
    qemu_chr_be_update_read_handlers(f.chr, f.context);
    buffer_run_until_calls(&f, 1);
    g_assert_cmpmem(QIO_CHANNEL_BUFFER(f.output)->data,
                    QIO_CHANNEL_BUFFER(f.output)->usage, bytes, sizeof(bytes));
    buffer_fixture_clear(&f);
    g_main_context_unref(old_context);
}

static void buffer_fatal_test(void)
{
    static const ssize_t results[] = { -1 };
    BufferFixture f = { 0 };

    buffer_fixture_init(&f, true);
    f.output->results = results;
    f.output->nresults = G_N_ELEMENTS(results);
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, sizeof(bytes)), ==,
                    sizeof(bytes));
    buffer_run_until_calls(&f, 1);
    g_assert_cmpint(SOCKET_CHARDEV(f.chr)->state, ==,
                    TCP_CHARDEV_STATE_DISCONNECTED);
    g_assert_cmpuint(QIO_CHANNEL_BUFFER(f.output)->usage, ==, 0);
    errno = 0;
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, 1), ==, -1);
    g_assert_cmpint(errno, ==, EIO);
    buffer_fixture_clear(&f);
}

static gpointer buffer_echo_peer(gpointer opaque)
{
    QIOChannel *peer = opaque;
    uint8_t request[sizeof(bytes)];

    qio_channel_read_all(peer, (char *)request, sizeof(request), &error_abort);
    g_assert_cmpmem(request, sizeof(request), bytes, sizeof(bytes));
    qio_channel_write_all(peer, (char *)request, sizeof(request), &error_abort);
    return NULL;
}

static void buffer_sync_read_test(void)
{
    BufferFixture f = { 0 };
    SocketChardev *s;
    GThread *peer_thread;
    uint8_t response[sizeof(bytes)];

    buffer_fixture_init(&f, true);
    s = SOCKET_CHARDEV(f.chr);
    object_unref(OBJECT(s->ioc));
    s->ioc = QIO_CHANNEL(object_ref(s->sioc));
    peer_thread = g_thread_new("buffer-echo", buffer_echo_peer, f.peer);

    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, sizeof(bytes)), ==,
                    sizeof(bytes));
    /* Synchronous request/reply must not depend on a blocked main loop. */
    g_assert_cmpint(qemu_chr_fe_read_all(&f.be, response, sizeof(response)), ==,
                    sizeof(response));
    g_assert_cmpmem(response, sizeof(response), bytes, sizeof(bytes));
    g_thread_join(peer_thread);
    buffer_fixture_clear(&f);
}

typedef struct BufferCoroutine {
    BufferFixture *fixture;
    uint8_t *data;
    int length;
    bool done;
} BufferCoroutine;

static void coroutine_fn buffer_write_all(void *opaque)
{
    BufferCoroutine *writer = opaque;

    g_assert_cmpint(qemu_chr_fe_write_all(&writer->fixture->be, writer->data,
                                         writer->length), ==, writer->length);
    writer->done = true;
}

static void buffer_coroutine_test(void)
{
    BufferFixture f = { 0 };
    g_autofree uint8_t *data = g_malloc0(16 * 1024 + 1);
    BufferCoroutine writer = { .fixture = &f, .data = data,
                               .length = 16 * 1024 + 1 };
    Coroutine *co;
    int64_t deadline;

    if (!g_test_subprocess()) {
        g_test_trap_subprocess(NULL, 2 * G_USEC_PER_SEC, 0);
        g_test_trap_assert_passed();
        return;
    }
    buffer_fixture_init(&f, true);
    f.output->blocked = true;
    co = qemu_coroutine_create(buffer_write_all, &writer);
    qemu_coroutine_enter(co);
    g_assert_false(writer.done);
    g_assert_cmpuint(f.output->calls, ==, 1);

    /* write_all yielded holding chr_write_lock. Dispatch its writable watch
     * before the AIO timer can resume the writer on this same host thread. */
    g_assert_true(g_main_context_iteration(f.context, false));
    g_assert_false(writer.done);
    g_assert_cmpuint(f.output->calls, ==, 1);
    /* A ready socket must not keep spinning while the lock is unavailable. */
    g_assert_cmpint(g_source_get_ready_time(SOCKET_CHARDEV(f.chr)->write_watch),
                    >, 0);

    f.output->blocked = false;
    deadline = g_get_monotonic_time() + G_USEC_PER_SEC;
    while (!writer.done) {
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        main_loop_wait(false);
    }
    buffer_run_until_calls(&f, 3);
    g_assert_cmpmem(QIO_CHANNEL_BUFFER(f.output)->data,
                    QIO_CHANNEL_BUFFER(f.output)->usage, data, writer.length);
    buffer_fixture_clear(&f);
}

static void buffer_closed_reconfigure(void *opaque, QEMUChrEvent event)
{
    BufferFixture *f = opaque;

    if (event == CHR_EVENT_CLOSED) {
        qemu_chr_fe_set_handlers(&f->be, NULL, NULL, NULL, NULL,
                                 NULL, f->context, true);
    }
}

static void buffer_closed_handler_test(void)
{
    BufferFixture f = { 0 };

    if (!g_test_subprocess()) {
        g_test_trap_subprocess(NULL, 2 * G_USEC_PER_SEC, 0);
        g_test_trap_assert_passed();
        return;
    }
    buffer_fixture_init(&f, true);
    qemu_chr_fe_set_handlers(&f.be, NULL, NULL, buffer_closed_reconfigure,
                             NULL, &f, f.context, true);
    g_assert_cmpint(qemu_chr_fe_write(&f.be, bytes, sizeof(bytes)), ==,
                    sizeof(bytes));
    qemu_chr_fe_disconnect(&f.be);
    g_assert_cmpint(SOCKET_CHARDEV(f.chr)->state, ==,
                    TCP_CHARDEV_STATE_DISCONNECTED);
    g_assert_cmpuint(f.output->calls, ==, 0);
    buffer_fixture_clear(&f);
}

int main(int argc, char **argv)
{
    qemu_init_main_loop(&error_abort);
    socket_init();
    g_test_init(&argc, &argv, NULL);
    module_call_init(MODULE_INIT_QOM);
    type_register_static(&test_buffer_channel_type);
    qemu_add_opts(&qemu_chardev_opts);
    g_test_add_func("/char/socket/write-buffer/batch", buffer_batch_test);
    g_test_add_func("/char/socket/write-buffer/disabled", buffer_disabled_test);
    g_test_add_func("/char/socket/write-buffer/backpressure",
                    buffer_backpressure_test);
    g_test_add_func("/char/socket/write-buffer/disconnect", buffer_disconnect_test);
    g_test_add_func("/char/socket/write-buffer/context", buffer_context_test);
    g_test_add_func("/char/socket/write-buffer/fatal", buffer_fatal_test);
    g_test_add_func("/char/socket/write-buffer/sync-read", buffer_sync_read_test);
    g_test_add_func("/char/socket/write-buffer/coroutine", buffer_coroutine_test);
    g_test_add_func("/char/socket/write-buffer/closed-handler",
                    buffer_closed_handler_test);
    return g_test_run();
}
