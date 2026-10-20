"""Shared QMP command deadlines; the caller retains the total host deadline."""
import time

QMP_COMMAND_TIMEOUT_SECONDS = 2.0


class StartupTimeoutError(RuntimeError):
    """The process did not reach the requested sampling-ready boundary."""


def _deadline_remaining(deadline, phase):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StartupTimeoutError(f'{phase} exceeded startup timeout')
    return remaining


def _set_startup_qmp_timeout(qmp, deadline, phase):
    qmp.settimeout(min(QMP_COMMAND_TIMEOUT_SECONDS,
                       _deadline_remaining(deadline, phase)))


def deadline_qmp_call(qmp, deadline, phase, callback):
    _set_startup_qmp_timeout(qmp, deadline, phase)
    try:
        result = callback()
    except TimeoutError as exc:
        raise StartupTimeoutError(f'{phase} QMP command timed out') from exc
    _deadline_remaining(deadline, phase)
    return result
