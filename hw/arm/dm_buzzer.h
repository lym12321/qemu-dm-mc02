/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Bounded, timestamped tone observations. No host audio or board policy. */
#ifndef HW_ARM_DM_BUZZER_H
#define HW_ARM_DM_BUZZER_H

#include <stdbool.h>
#include <stdint.h>

#define DM_BUZZER_EVENTS 256

typedef struct DmBuzzerEvent {
    uint64_t sequence;
    uint64_t timestamp_ns;
    uint32_t frequency_hz;
    uint32_t duty_permille;
} DmBuzzerEvent;

typedef struct DmBuzzerPin {
    uint64_t rise_ns;
    uint64_t high_ns;
    uint64_t period_ns;
    uint64_t deadline_ns;
    bool driven;
    bool high;
    bool have_rise;
} DmBuzzerPin;

typedef struct DmBuzzer {
    DmBuzzerEvent events[DM_BUZZER_EVENTS];
    uint64_t sequence;
    uint64_t generation;
    uint32_t frequency_hz;
    uint32_t duty_permille;
    DmBuzzerPin pin;
} DmBuzzer;

void dm_buzzer_reset(DmBuzzer *s, uint64_t timestamp_ns);
void dm_buzzer_observe(DmBuzzer *s, uint64_t timestamp_ns, bool enabled,
                       uint64_t frequency_hz, uint32_t duty_permille);
/* Entries are retained until overwritten, never consumed by a reader. */
const DmBuzzerEvent *dm_buzzer_event(const DmBuzzer *s, uint64_t sequence);
/* GPIO tone inference: one complete rise/fall/rise establishes pitch/duty.
 * Two missing periods terminate it at the end of the last observed cycle. */
void dm_buzzer_pin(DmBuzzer *s, uint64_t timestamp_ns, bool driven, bool high);
void dm_buzzer_pin_advance(DmBuzzer *s, uint64_t timestamp_ns);
void dm_buzzer_pin_disconnect(DmBuzzer *s);

#endif
