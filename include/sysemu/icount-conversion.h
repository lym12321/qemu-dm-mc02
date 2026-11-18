/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef SYSEMU_ICOUNT_CONVERSION_H
#define SYSEMU_ICOUNT_CONVERSION_H

#include "qemu/host-utils.h"

#define ICOUNT_MAX_NS_PER_INSN 1024

/* The caller supplies a configured, positive ns_per_insn. */
static inline bool icount_ns_per_insn_to_ns(int64_t count,
                                           uint16_t ns_per_insn,
                                           int64_t *ns)
{
    return !smul64_overflow(count, ns_per_insn, ns);
}

/* Round deadlines up without overflowing at INT64_MAX. */
static inline int64_t icount_ns_per_insn_round(int64_t ns,
                                             uint16_t ns_per_insn)
{
    return ns / ns_per_insn + (ns % ns_per_insn > 0);
}

#endif /* SYSEMU_ICOUNT_CONVERSION_H */
