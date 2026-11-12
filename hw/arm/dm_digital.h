/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_ARM_DM_DIGITAL_H
#define HW_ARM_DM_DIGITAL_H

#include <stdbool.h>
#include <stdint.h>

/* A resolved digital level at a virtual timestamp. driven=false means that
 * this producer supplies no defined level (disabled, disconnected or Hi-Z). */
typedef void DmDigitalOutput(void *opaque, uint64_t timestamp_ns,
                             bool driven, bool level);

#endif
