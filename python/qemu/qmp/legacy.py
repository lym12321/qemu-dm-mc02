"""
(Legacy) Sync QMP Wrapper

This module provides the `QEMUMonitorProtocol` class, which is a
synchronous wrapper around `QMPClient`.

Its design closely resembles that of the original QEMUMonitorProtocol
class, originally written by Luiz Capitulino. It is provided here for
compatibility with scripts inside the QEMU source tree that expect the
old interface.
"""

#
# Copyright (C) 2009-2022 Red Hat Inc.
#
# Authors:
#  Luiz Capitulino <lcapitulino@redhat.com>
#  John Snow <jsnow@redhat.com>
#
# This work is licensed under the terms of the GNU GPL, version 2.  See
# the COPYING file in the top-level directory.
#

import asyncio
import math
import socket
from types import TracebackType
from typing import (
    Any,
    Awaitable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from .error import QMPError
from .message import Message
from .protocol import Runstate, SocketAddrT
from .qmp_client import QMPClient


#: QMPMessage is an entire QMP message of any kind.
QMPMessage = Dict[str, Any]

#: QMPReturnValue is the 'return' value of a command.
QMPReturnValue = object

#: QMPObject is any object in a QMP message.
QMPObject = Dict[str, object]

# QMPMessage can be outgoing commands or incoming events/returns.
# QMPReturnValue is usually a dict/json object, but due to QAPI's
# 'command-returns-exceptions', it can actually be anything.
#
# {'return': {}} is a QMPMessage,
# {} is the QMPReturnValue.


class QMPBadPortError(QMPError):
    """
    Unable to parse socket address: Port was non-numerical.
    """


class QEMUMonitorProtocol:
    """
    Provide an API to connect to QEMU via QEMU Monitor Protocol (QMP)
    and then allow to handle commands and events.

    :param address:  QEMU address, can be a unix socket path (string), a tuple
                     in the form ( address, port ) for a TCP connection, or an
                     existing `socket.socket` object.
    :param server:   Act as the socket server. (See 'accept')
                     Not applicable when passing a socket directly.
    :param nickname: Optional nickname used for logging.
    """

    def __init__(self,
                 address: Union[SocketAddrT, socket.socket],
                 server: bool = False,
                 nickname: Optional[str] = None):

        if server and isinstance(address, socket.socket):
            raise ValueError(
                "server argument should be False when passing a socket")

        self._qmp = QMPClient(nickname)
        self._aloop = asyncio.get_event_loop()
        self._address = address
        self._timeout: Optional[float] = None

        if server:
            assert not isinstance(self._address, socket.socket)
            self._sync(self._qmp.start_server(self._address))

    _T = TypeVar('_T')

    def _sync(
            self, future: Awaitable[_T], timeout: Optional[float] = None
    ) -> _T:
        return self._aloop.run_until_complete(
            asyncio.wait_for(future, timeout=timeout)
        )

    def _get_greeting(self) -> Optional[QMPMessage]:
        if self._qmp.greeting is not None:
            # pylint: disable=protected-access
            return self._qmp.greeting._asdict()
        return None

    def __enter__(self: _T) -> _T:
        # Implement context manager enter function.
        return self

    def __exit__(self,
                 exc_type: Optional[Type[BaseException]],
                 exc_val: Optional[BaseException],
                 exc_tb: Optional[TracebackType]) -> None:
        # Implement context manager exit function.
        self.close()

    @classmethod
    def parse_address(cls, address: str) -> SocketAddrT:
        """
        Parse a string into a QMP address.

        Figure out if the argument is in the port:host form.
        If it's not, it's probably a file path.
        """
        components = address.split(':')
        if len(components) == 2:
            try:
                port = int(components[1])
            except ValueError:
                msg = f"Bad port: '{components[1]}' in '{address}'."
                raise QMPBadPortError(msg) from None
            return (components[0], port)

        # Treat as filepath.
        return address

    def connect(self, negotiate: bool = True) -> Optional[QMPMessage]:
        """
        Connect to the QMP Monitor and perform capabilities negotiation.

        :return: QMP greeting dict, or None if negotiate is false
        :raise ConnectError: on connection errors
        """
        self._qmp.await_greeting = negotiate
        self._qmp.negotiate = negotiate

        self._sync(
            self._qmp.connect(self._address)
        )
        return self._get_greeting()

    def accept(self, timeout: Optional[float] = 15.0) -> QMPMessage:
        """
        Await connection from QMP Monitor and perform capabilities negotiation.

        :param timeout:
            timeout in seconds (nonnegative float number, or None).
            If None, there is no timeout, and this may block forever.

        :return: QMP greeting dict
        :raise ConnectError: on connection errors
        """
        self._qmp.await_greeting = True
        self._qmp.negotiate = True

        self._sync(self._qmp.accept(), timeout)

        ret = self._get_greeting()
        assert ret is not None
        return ret

    def cmd_obj(self, qmp_cmd: QMPMessage) -> QMPMessage:
        """
        Send a QMP command to the QMP Monitor.

        :param qmp_cmd: QMP command to be sent as a Python dict
        :return: QMP response as a Python dict
        """
        return dict(
            self._sync(
                # pylint: disable=protected-access

                # _raw() isn't a public API, because turning off
                # automatic ID assignment is discouraged. For
                # compatibility with iotests *only*, do it anyway.
                self._qmp._raw(qmp_cmd, assign_id=False),
                self._timeout
            )
        )

    def cmd_raw(self, name: str,
                args: Optional[Dict[str, object]] = None) -> QMPMessage:
        """
        Build a QMP command and send it to the QMP Monitor.

        :param name: command name (string)
        :param args: command arguments (dict)
        """
        qmp_cmd: QMPMessage = {'execute': name}
        if args:
            qmp_cmd['arguments'] = args
        return self.cmd_obj(qmp_cmd)

    def cmd(self, cmd: str, **kwds: object) -> QMPReturnValue:
        """
        Build and send a QMP command to the monitor, report errors if any
        """
        return self._sync(
            self._qmp.execute(cmd, kwds),
            self._timeout
        )

    def cmd_many(
            self, commands: Tuple[Tuple[str, Optional[Dict[str, object]]], ...]
    ) -> Tuple[QMPReturnValue, ...]:
        """Execute 1..8 independent commands, returning values in input order.

        All commands must serialize before any is issued. The existing finite,
        positive RPC timeout bounds the group wait; cancellation cleanup has a
        separate bound of the same duration.
        This is not atomic: errors retain earlier command effects, with no retry
        or rollback. Callers must use independent queries and freeze on failure.
        Event routing, IDs and execution remain owned by QMPClient.
        """
        timeout = self._timeout
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("cmd_many requires a finite positive timeout")
        try:
            timeout = float(timeout)
        except OverflowError as error:
            raise ValueError(
                "cmd_many requires a finite positive timeout"
            ) from error
        if not math.isfinite(timeout):
            raise ValueError("cmd_many requires a finite positive timeout")
        if type(commands) is not tuple or not 1 <= len(commands) <= 8:
            raise ValueError("cmd_many requires a plain tuple of 1..8 commands")
        messages = []
        for command in commands:
            if type(command) is not tuple or len(command) != 2:
                raise TypeError(
                    "cmd_many commands must be plain (name, arguments) tuples"
                )
            name, arguments = command
            if type(name) is not str or not name or '\0' in name:
                raise ValueError(
                    "cmd_many command names must be plain nonempty strings "
                    "without NUL"
                )
            if arguments is not None and type(arguments) is not dict:
                raise TypeError("cmd_many arguments must be a plain dict or None")
            message = self._qmp.make_execute_msg(name, arguments)
            # Canonical serialization/deserialization snapshots nested arguments
            # too: later caller mutation cannot invalidate whole-group admission.
            messages.append(Message(bytes(message)))

        def note(error: BaseException, text: str) -> None:
            # Keep this fork's Python >=3.8 support; Python >=3.11 renders these
            # notes itself, older supported versions retain the same evidence.
            add_note = getattr(error, 'add_note', None)
            if add_note is not None:
                add_note(text)
            else:
                setattr(error, '__notes__',
                        getattr(error, '__notes__', []) + [text])

        owned = []
        groups = []

        async def collect() -> Tuple[QMPReturnValue, ...]:
            for message in messages:
                owned.append(self._aloop.create_task(
                    self._qmp.execute_msg(message)))
            group = asyncio.gather(*owned, return_exceptions=True)
            groups.append(group)
            done, _ = await asyncio.wait((group,), timeout=timeout)
            if not done:
                raise asyncio.TimeoutError("cmd_many group timed out")
            return tuple(group.result())

        main = self._aloop.create_task(collect())
        try:
            values = self._sync(main)
        except BaseException as error:
            # run_until_complete may raise KeyboardInterrupt without cancelling
            # its main future. Explicitly cancel every task owned by this call.
            pending = (main, *groups, *owned)
            for task in pending:
                task.cancel()

            async def settle() -> None:
                _, remaining = await asyncio.wait(pending, timeout=timeout)
                if remaining:
                    raise asyncio.TimeoutError("cmd_many cancellation did not settle")
                for task in pending:
                    if not task.cancelled():
                        task.exception()  # Retrieve any terminal exception.
                for index, task in enumerate(owned):
                    if not task.cancelled() and task.exception() is not None:
                        failure = task.exception()
                        note(error, f"cmd_many command {index} failed: " +
                             f"{type(failure).__name__}: {failure}")

            try:
                self._sync(settle())
            except BaseException as cleanup:
                note(error, "cmd_many cleanup failed: " +
                     f"{type(cleanup).__name__}: {cleanup}")
            raise
        failures = [(index, value) for index, value in enumerate(values)
                    if isinstance(value, BaseException)]
        if failures:
            _, primary = failures[0]
            for index, error in failures[1:]:
                note(primary, f"cmd_many command {index} failed: " +
                     f"{type(error).__name__}: {error}")
            raise primary
        return values

    def pull_event(self,
                   wait: Union[bool, float] = False) -> Optional[QMPMessage]:
        """
        Pulls a single event.

        :param wait:
            If False or 0, do not wait. Return None if no events ready.
            If True, wait forever until the next event.
            Otherwise, wait for the specified number of seconds.

        :raise asyncio.TimeoutError:
            When a timeout is requested and the timeout period elapses.

        :return: The first available QMP event, or None.
        """
        if not wait:
            # wait is False/0: "do not wait, do not except."
            if self._qmp.events.empty():
                return None

        # If wait is 'True', wait forever. If wait is False/0, the events
        # queue must not be empty; but it still needs some real amount
        # of time to complete.
        timeout = None
        if wait and isinstance(wait, float):
            timeout = wait

        return dict(
            self._sync(
                self._qmp.events.get(),
                timeout
            )
        )

    def get_events(self, wait: Union[bool, float] = False) -> List[QMPMessage]:
        """
        Get a list of QMP events and clear all pending events.

        :param wait:
            If False or 0, do not wait. Return None if no events ready.
            If True, wait until we have at least one event.
            Otherwise, wait for up to the specified number of seconds for at
            least one event.

        :raise asyncio.TimeoutError:
            When a timeout is requested and the timeout period elapses.

        :return: A list of QMP events.
        """
        events = [dict(x) for x in self._qmp.events.clear()]
        if events:
            return events

        event = self.pull_event(wait)
        return [event] if event is not None else []

    def clear_events(self) -> None:
        """Clear current list of pending events."""
        self._qmp.events.clear()

    def close(self) -> None:
        """Close the connection."""
        self._sync(
            self._qmp.disconnect()
        )

    def settimeout(self, timeout: Optional[float]) -> None:
        """
        Set the timeout for QMP RPC execution.

        This timeout affects the `cmd`, `cmd_obj`, and `command` methods.
        The `accept`, `pull_event` and `get_event` methods have their
        own configurable timeouts.

        :param timeout:
            timeout in seconds, or None.
            None will wait indefinitely.
        """
        self._timeout = timeout

    def send_fd_scm(self, fd: int) -> None:
        """
        Send a file descriptor to the remote via SCM_RIGHTS.
        """
        self._qmp.send_fd_scm(fd)

    def __del__(self) -> None:
        if self._qmp.runstate == Runstate.IDLE:
            return

        if not self._aloop.is_running():
            self.close()
        else:
            # Garbage collection ran while the event loop was running.
            # Nothing we can do about it now, but if we don't raise our
            # own error, the user will be treated to a lot of traceback
            # they might not understand.
            raise QMPError(
                "QEMUMonitorProtocol.close()"
                " was not called before object was garbage collected"
            )
