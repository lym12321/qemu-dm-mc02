/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef SYSEMU_VIRTUAL_EXECUTION_H
#define SYSEMU_VIRTUAL_EXECUTION_H

/*
 * Remaining ns from the retained bound for supplied QEMU_CLOCK_VIRTUAL time:
 * -1 if inactive, zero at/past target. Borrowed by native budget and idle warp.
 */
int64_t virtual_execution_deadline_ns(int64_t now_ns);

#endif
