/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_ws2812.h"

#define RESET_NS UINT64_C(50000)

void dm_ws2812_reset(DmWs2812 *s, uint64_t timestamp_ns)
{
    *s = (DmWs2812) { .last_ns = timestamp_ns, .edge_ns = timestamp_ns };
}

void dm_ws2812_resync(DmWs2812 *s, uint64_t timestamp_ns)
{
    s->last_ns = timestamp_ns;
    s->edge_ns = timestamp_ns;
    s->bits = 0;
    s->count = 0;
    s->known = false;
    s->high = false;
    s->bad = false;
    s->synchronized = false;
}

static void malformed(DmWs2812 *s)
{
    if (!s->bad) {
        s->errors++;
        s->bad = true;
    }
}

uint64_t dm_ws2812_deadline(const DmWs2812 *s)
{
    return s->known && !s->high &&
           (!s->synchronized || s->count || s->bad) &&
           s->edge_ns <= INT64_MAX - RESET_NS ? s->edge_ns + RESET_NS : 0;
}

void dm_ws2812_advance(DmWs2812 *s, uint64_t timestamp_ns)
{
    uint64_t deadline = dm_ws2812_deadline(s);

    if (timestamp_ns < s->last_ns) {
        malformed(s);
        return;
    }
    s->last_ns = timestamp_ns;
    if (!deadline || timestamp_ns < deadline) {
        return;
    }
    if (!s->bad && s->count == 24) {
        /* DIN carries G7..G0, R7..R0, B7..B0. */
        s->rgb = ((s->bits >> 8) & 0xff) << 16 |
                 ((s->bits >> 16) & 0xff) << 8 | (s->bits & 0xff);
        s->frame_ns = deadline;
        s->frames++;
    } else if (s->count && !s->bad) {
        malformed(s);
    }
    s->bits = 0;
    s->count = 0;
    s->bad = false;
    s->synchronized = true;
}

void dm_ws2812_level(DmWs2812 *s, uint64_t timestamp_ns,
                     bool known, bool high)
{
    if (timestamp_ns < s->last_ns) {
        malformed(s);
        return;
    }
    dm_ws2812_advance(s, timestamp_ns);
    if (s->known == known && (!known || s->high == high)) {
        return;
    }
    if (!known || !s->known) {
        if (s->count || s->high) {
            malformed(s);
        }
        s->synchronized = false;
    } else if (!high && s->synchronized && !s->bad && s->count < 24) {
        uint64_t duration = timestamp_ns - s->edge_ns;
        bool zero = duration >= 250 && duration <= 550;
        bool one = duration >= 650 && duration <= 950;

        if (!zero && !one) {
            malformed(s);
        } else {
            s->bits = (s->bits << 1) | one;
            s->count++;
        }
    }
    s->known = known;
    s->high = high;
    s->edge_ns = timestamp_ns;
}
