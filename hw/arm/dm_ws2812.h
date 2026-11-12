/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_ARM_DM_WS2812_H
#define HW_ARM_DM_WS2812_H

#include <stdbool.h>
#include <stdint.h>

/* Worldsemi WS2812B classic data sheet: GRB, MSB first, reset >= 50 us.
 * Pure DIN consumer. The caller owns the virtual clock and reset timer. */
typedef struct DmWs2812 {
    uint64_t last_ns;
    uint64_t edge_ns;
    uint64_t frame_ns;
    uint64_t frames;
    uint64_t errors;
    uint32_t rgb;
    uint32_t bits;
    unsigned count;
    bool driven;
    bool high;
    bool bad;
    bool last_bit;
    bool synchronized;
    bool complete_low;
} DmWs2812;

void dm_ws2812_reset(DmWs2812 *s, uint64_t timestamp_ns);
void dm_ws2812_level(DmWs2812 *s, uint64_t timestamp_ns,
                     bool driven, bool high);
void dm_ws2812_advance(DmWs2812 *s, uint64_t timestamp_ns);
/* Zero when no latch/reset deadline is pending. */
uint64_t dm_ws2812_deadline(const DmWs2812 *s);

#endif
