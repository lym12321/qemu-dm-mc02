/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_buzzer.h"

static void append(DmBuzzer *s, uint64_t timestamp_ns)
{
    uint64_t sequence = ++s->sequence;
    s->events[(sequence - 1) % DM_BUZZER_EVENTS] = (DmBuzzerEvent) {
        .sequence = sequence, .timestamp_ns = timestamp_ns,
        .frequency_hz = s->frequency_hz, .duty_permille = s->duty_permille,
    };
}

void dm_buzzer_reset(DmBuzzer *s, uint64_t timestamp_ns)
{
    s->generation++;
    s->sequence = 0;
    s->frequency_hz = 0;
    s->duty_permille = 0;
    s->pin = (DmBuzzerPin) { 0 };
    append(s, timestamp_ns);
}

void dm_buzzer_observe(DmBuzzer *s, uint64_t timestamp_ns, bool enabled,
                       uint64_t frequency_hz, uint32_t duty_permille)
{
    uint32_t frequency = enabled && duty_permille > 0 &&
                         duty_permille < 1000 && frequency_hz <= UINT32_MAX ?
                         frequency_hz : 0;
    uint32_t duty = frequency ? duty_permille : 0;

    if (!s->sequence || frequency != s->frequency_hz ||
        duty != s->duty_permille) {
        s->frequency_hz = frequency;
        s->duty_permille = duty;
        append(s, timestamp_ns);
    }
}

const DmBuzzerEvent *dm_buzzer_event(const DmBuzzer *s, uint64_t sequence)
{
    if (!sequence || sequence > s->sequence ||
        s->sequence - sequence >= DM_BUZZER_EVENTS) {
        return NULL;
    }
    return &s->events[(sequence - 1) % DM_BUZZER_EVENTS];
}

void dm_buzzer_pin_disconnect(DmBuzzer *s)
{
    s->pin = (DmBuzzerPin) { 0 };
}

void dm_buzzer_pin_advance(DmBuzzer *s, uint64_t timestamp_ns)
{
    DmBuzzerPin *p = &s->pin;
    if (p->deadline_ns && timestamp_ns >= p->deadline_ns) {
        dm_buzzer_observe(s, p->rise_ns + p->period_ns, false, 0, 0);
        p->deadline_ns = 0;
        p->have_rise = false;
    }
}

void dm_buzzer_pin(DmBuzzer *s, uint64_t timestamp_ns, bool driven, bool high)
{
    DmBuzzerPin *p = &s->pin;
    dm_buzzer_pin_advance(s, timestamp_ns);
    if (!driven) {
        dm_buzzer_pin_disconnect(s);
        dm_buzzer_observe(s, timestamp_ns, false, 0, 0);
        return;
    }
    if (p->driven && p->high == high) { return; }
    if (high) {
        if (p->have_rise && p->high_ns && timestamp_ns > p->rise_ns) {
            uint64_t period = timestamp_ns - p->rise_ns;
            uint64_t frequency = (UINT64_C(1000000000) + period / 2) / period;
            uint32_t duty = MIN(((__uint128_t)p->high_ns * 1000 + period / 2) /
                                period, 1000);
            dm_buzzer_observe(s, p->rise_ns, true, frequency, duty);
            p->period_ns = period;
            p->deadline_ns = period <= (INT64_MAX - timestamp_ns) / 2 ?
                             timestamp_ns + 2 * period : 0;
        }
        p->rise_ns = timestamp_ns;
        p->high_ns = 0;
        p->have_rise = true;
    } else if (p->have_rise && timestamp_ns >= p->rise_ns) {
        p->high_ns = timestamp_ns - p->rise_ns;
    }
    p->driven = true;
    p->high = high;
}
