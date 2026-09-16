#include "dm_mc02_bmi088_fifo.h"

#include <stdint.h>
#include <stdio.h>

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "%s\n", message);
        return 1;
    }
    return 0;
}

int main(void)
{
    const uint16_t stop_limit = dm_mc02_bmi088_fifo_limit(false, true);
    const uint16_t stream_limit = dm_mc02_bmi088_fifo_limit(false, false);
    uint16_t length = stop_limit;
    unsigned drops = 0;

    if (expect(stop_limit == 800 && stream_limit == 792,
               "BMI088 gyro FIFO limits changed")) {
        return 1;
    }
    if (expect(dm_mc02_bmi088_fifo_needs_room(length, stream_limit, 8),
               "full stop-at-full FIFO was accepted after stream switch")) {
        return 1;
    }

    /* This is the exact transition handled by dm_mc02_bmi_fifo_push():
     * discard complete gyro frames until the new mode can accept one. */
    while (dm_mc02_bmi088_fifo_needs_room(length, stream_limit, 8)) {
        if (length < 8) {
            return 1;
        }
        length -= 8;
        drops++;
    }
    length += 8;
    if (expect(drops == 2 && length == stream_limit,
               "stop-at-full to stream transition did not preserve bound")) {
        return 1;
    }
    if (expect(!dm_mc02_bmi088_fifo_needs_room(length, stream_limit, 0),
               "stream FIFO length exceeded its effective capacity")) {
        return 1;
    }
    puts("RESULT: BMI088 FIFO mode-switch capacity smoke passed");
    return 0;
}
