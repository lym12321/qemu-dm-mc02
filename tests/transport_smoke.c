#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE 1
#endif

#include "dm_mc02_protocol.h"
#include "dm_mc02_transport.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <signal.h>
#include <unistd.h>

static int check(int ok, const char *what)
{
    if (!ok) fprintf(stderr, "FAIL: %s\n", what);
    return ok ? 0 : 1;
}

int main(void)
{
    char path[108];
    char nonblocking_path[108];
    DmMc02TransportListener *listener = NULL;
    DmMc02TransportListener *nonblocking_listener = NULL;
    DmMc02Transport *server = NULL;
    DmMc02TransportResult r;
    pid_t child;
    DmMc02Frame frame = {0}, received = {0};
    DmMc02ImuSample imu = {{1.0f, 2.0f, 3.0f}, {4.0f, 5.0f, 6.0f}};

    snprintf(path, sizeof(path), "/tmp/dm-mc02-transport-%ld.sock", (long)getpid());
    snprintf(nonblocking_path, sizeof(nonblocking_path),
             "/tmp/dm-mc02-transport-nb-%ld.sock", (long)getpid());
    r = dm_mc02_transport_unix_listener_create(
        nonblocking_path, DM_MC02_TRANSPORT_NONBLOCKING,
        &nonblocking_listener);
    if (check(r == DM_MC02_TRANSPORT_OK, "create non-blocking listener")) return 1;
    r = dm_mc02_transport_accept(nonblocking_listener, &server);
    if (check(r == DM_MC02_TRANSPORT_WOULD_BLOCK,
              "non-blocking accept without a connection")) return 1;
    dm_mc02_transport_listener_close(nonblocking_listener);
    nonblocking_listener = NULL;

    r = dm_mc02_transport_unix_listener_create(path, DM_MC02_TRANSPORT_BLOCKING, &listener);
    if (check(r == DM_MC02_TRANSPORT_OK, "create Unix listener")) return 1;
    child = fork();
    if (check(child >= 0, "fork")) return 1;
    if (child == 0) {
        DmMc02Transport *client = NULL;
        frame.header.version = DM_MC02_PROTOCOL_VERSION;
        frame.header.sequence = 1; frame.header.virtual_time_ns = 100;
        dm_mc02_set_imu_payload(&frame, &imu);
        r = dm_mc02_transport_unix_connect(path, DM_MC02_TRANSPORT_BLOCKING, &client);
        if (r == DM_MC02_TRANSPORT_OK) r = dm_mc02_transport_send_frame(client, &frame);
        dm_mc02_transport_close(client);
        _exit(r == DM_MC02_TRANSPORT_OK ? 0 : 1);
    }
    r = dm_mc02_transport_accept(listener, &server);
    if (check(r == DM_MC02_TRANSPORT_OK, "accept Unix connection")) return 1;
    r = dm_mc02_transport_recv_frame(server, &received);
    if (check(r == DM_MC02_TRANSPORT_OK, "receive IMU frame") ||
        check(received.header.type == DM_MC02_FRAME_IMU_SAMPLE, "IMU frame type") ||
        check(dm_mc02_get_imu_payload(&received, &imu) && imu.accel[2] == 6.0f,
              "IMU payload")) return 1;
    r = dm_mc02_transport_recv_frame(server, &received);
    if (check(r == DM_MC02_TRANSPORT_EOF, "report peer EOF")) return 1;
    if (check(waitpid(child, NULL, 0) == child, "wait for sender")) return 1;
    dm_mc02_transport_close(server); server = NULL;

    /* A non-blocking send must not silently replace a frame that is still
     * partially buffered. Use a deliberately tiny kernel send buffer and a
     * peer that never reads until the parent has verified the API boundary. */
    {
        int send_buffer = 1024;
        DmMc02TransportListener *nb_listener = NULL;
        DmMc02Transport *nb_sender = NULL;
        DmMc02Frame pending = {0}, different = {0};
        int accepted = 0;

        r = dm_mc02_transport_unix_listener_create(
            nonblocking_path, DM_MC02_TRANSPORT_NONBLOCKING, &nb_listener);
        if (check(r == DM_MC02_TRANSPORT_OK,
                  "create pending-send listener")) return 1;
        child = fork();
        if (check(child >= 0, "fork pending-send peer")) return 1;
        if (child == 0) {
            int fd;
            struct sockaddr_un sa;

            fd = socket(AF_UNIX, SOCK_STREAM, 0);
            memset(&sa, 0, sizeof(sa));
            sa.sun_family = AF_UNIX;
            strcpy(sa.sun_path, nonblocking_path);
            if (fd >= 0 &&
                connect(fd, (struct sockaddr *)&sa, sizeof(sa)) == 0) {
                pause();
            }
            if (fd >= 0) close(fd);
            _exit(0);
        }
        for (unsigned attempt = 0; attempt < 1000; ++attempt) {
            r = dm_mc02_transport_accept(nb_listener, &nb_sender);
            if (r == DM_MC02_TRANSPORT_OK) {
                accepted = 1;
                break;
            }
            if (r != DM_MC02_TRANSPORT_WOULD_BLOCK) {
                break;
            }
            usleep(1000);
        }
        if (check(accepted, "accept pending-send peer")) return 1;
        (void)setsockopt(dm_mc02_transport_fd(nb_sender), SOL_SOCKET,
                          SO_SNDBUF, &send_buffer, sizeof(send_buffer));
        pending.header.version = DM_MC02_PROTOCOL_VERSION;
        pending.header.sequence = 1;
        pending.header.virtual_time_ns = 1;
        dm_mc02_set_imu_payload(&pending, &imu);
        different = pending;
        different.header.sequence = 2;
        for (unsigned attempt = 0; attempt < 10000; ++attempt) {
            r = dm_mc02_transport_send_frame(nb_sender, &pending);
            if (r == DM_MC02_TRANSPORT_WOULD_BLOCK) {
                break;
            }
            if (check(r == DM_MC02_TRANSPORT_OK,
                      "fill non-blocking pending send")) return 1;
        }
        if (check(r == DM_MC02_TRANSPORT_WOULD_BLOCK,
                  "produce pending non-blocking send") ||
            check(dm_mc02_transport_send_frame(nb_sender, &different) ==
                  DM_MC02_TRANSPORT_INVALID,
                  "reject different frame during pending send")) return 1;
        dm_mc02_transport_close(nb_sender);
        dm_mc02_transport_listener_close(nb_listener);
        kill(child, SIGTERM);
        waitpid(child, NULL, 0);
    }

    /* A fresh connection proves that an outer length is checked before allocation/read. */
    child = fork();
    if (check(child >= 0, "fork bad-length sender")) return 1;
    if (child == 0) {
        int fd; struct sockaddr_un sa;
        uint8_t bad[4] = {0xff, 0xff, 0xff, 0x7f};
        fd = socket(AF_UNIX, SOCK_STREAM, 0); memset(&sa, 0, sizeof(sa));
        sa.sun_family = AF_UNIX; strcpy(sa.sun_path, path);
        if (fd >= 0 && connect(fd, (struct sockaddr *)&sa, sizeof(sa)) == 0)
            (void)send(fd, bad, sizeof(bad), 0);
        if (fd >= 0) close(fd);
        _exit(0);
    }
    r = dm_mc02_transport_accept(listener, &server);
    if (check(r == DM_MC02_TRANSPORT_OK, "accept bad-length connection")) return 1;
    r = dm_mc02_transport_recv_frame(server, &received);
    if (check(r == DM_MC02_TRANSPORT_PROTOCOL, "reject oversized frame length")) return 1;
    dm_mc02_transport_close(server); dm_mc02_transport_listener_close(listener);
    waitpid(child, NULL, 0);
    puts("RESULT: transport smoke passed");
    return 0;
}
