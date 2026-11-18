/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_ARM_DM_WS2812_H
#define HW_ARM_DM_WS2812_H

#include <stdbool.h>
#include <stdint.h>

/* DIN pulse decoder: GRB, MSB first, 250..550 ns high for zero and
 * 650..950 ns high for one. Low intervals below the 50 us reset threshold
 * do not determine the bit value. The caller resolves the DIN node level,
 * owns the clock and reset timer, and marks an unknown input with known=false. */
typedef struct DmWs2812 {
    uint64_t last_ns;
    uint64_t edge_ns;
    uint64_t frame_ns;
    uint64_t frames;
    uint64_t errors;
    uint32_t rgb;
    uint32_t bits;
    unsigned count;
    bool known;
    bool high;
    bool bad;
    bool synchronized;
} DmWs2812;

void dm_ws2812_reset(DmWs2812 *s, uint64_t timestamp_ns);
/* Drop a partial power-up frame while retaining accumulated telemetry. */
void dm_ws2812_resync(DmWs2812 *s, uint64_t timestamp_ns);
void dm_ws2812_level(DmWs2812 *s, uint64_t timestamp_ns,
                     bool known, bool high);
void dm_ws2812_advance(DmWs2812 *s, uint64_t timestamp_ns);
/* Zero when no latch/reset deadline is pending. */
uint64_t dm_ws2812_deadline(const DmWs2812 *s);

#endif
