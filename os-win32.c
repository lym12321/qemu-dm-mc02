/*
 * os-win32.c
 *
 * Copyright (c) 2003-2008 Fabrice Bellard
 * Copyright (c) 2010 Red Hat, Inc.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */

#include "qemu/osdep.h"
#include <windows.h>
#include <mmsystem.h>
#include "qemu/error-report.h"
#include "sysemu/runstate.h"

static BOOL WINAPI qemu_ctrl_handler(DWORD type)
{
    qemu_system_shutdown_request(SHUTDOWN_CAUSE_HOST_SIGNAL);
    /* Windows 7 kills application when the function returns.
       Sleep here to give QEMU a try for closing.
       Sleep period is 10000ms because Windows kills the program
       after 10 seconds anyway. */
    Sleep(10000);

    return TRUE;
}

static TIMECAPS mm_tc;

static void os_undo_timer_resolution(void)
{
    timeEndPeriod(mm_tc.wPeriodMin);
}

static void os_setup_execution_speed_qos(void)
{
    typedef BOOL (WINAPI *ProcessInformationFunc)(HANDLE,
        PROCESS_INFORMATION_CLASS, LPVOID, DWORD);
    HMODULE kernel32 = GetModuleHandleW(L"kernel32.dll");
    ProcessInformationFunc get_information = (ProcessInformationFunc)
        GetProcAddress(kernel32, "GetProcessInformation");
    ProcessInformationFunc set_information = (ProcessInformationFunc)
        GetProcAddress(kernel32, "SetProcessInformation");
    PROCESS_POWER_THROTTLING_STATE state = {
        .Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION,
    };

    if (!get_information || !set_information) {
        warn_report("process execution-speed QoS is unavailable; "
                    "using Windows default scheduling");
        return;
    }
    if (!get_information(GetCurrentProcess(), ProcessPowerThrottling,
                         &state, sizeof(state))) {
        warn_report("cannot read process execution-speed QoS (error %lu)",
                    GetLastError());
        return;
    }

    /* Keep time-sensitive emulation on HighQoS even without a visible window. */
    state.ControlMask |= PROCESS_POWER_THROTTLING_EXECUTION_SPEED;
    state.StateMask &= ~PROCESS_POWER_THROTTLING_EXECUTION_SPEED;
    if (!set_information(GetCurrentProcess(), ProcessPowerThrottling,
                         &state, sizeof(state))) {
        warn_report("cannot request process execution-speed HighQoS "
                    "(error %lu)", GetLastError());
    }
}

void os_setup_early_signal_handling(void)
{
    SetConsoleCtrlHandler(qemu_ctrl_handler, TRUE);
    os_setup_execution_speed_qos();
    timeGetDevCaps(&mm_tc, sizeof(mm_tc));
    timeBeginPeriod(mm_tc.wPeriodMin);
    atexit(os_undo_timer_resolution);
}

void os_set_line_buffering(void)
{
    setbuf(stdout, NULL);
    setbuf(stderr, NULL);
}
