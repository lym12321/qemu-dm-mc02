"""Host adapter isolation with real socket bytes; these are not QEMU runs."""

import socket
import struct
from types import SimpleNamespace

import pytest

from sim.api import Session
from sim.boards.qemu import validate_can_execution_snapshot
from sim.fabric import CanFrame
from sim.machines.qemu import profile
from sim.transports.can_stream import CanStream


def test_can_batch_reserves_exact_goal_and_observes_once_after_all_writes(monkeypatch):
    host, peer = socket.socketpair()
    host.setblocking(False)
    peer.settimeout(1)
    events, receipts = [], []
    context = SimpleNamespace(t_sim_ns=5, session=Session(1),
                              emit=lambda *args, **kwargs: events.append((args, kwargs)))
    backend = SimpleNamespace(check=lambda: None, pending=None, started=True, t_sim_ns=5,
                              to_native=lambda cut: 100 + cut, deadline_monotonic=123)
    adapter = object.__new__(profile.DmMc02Adapter)
    adapter._ctx, adapter._backend = context, backend
    adapter._cosim = None
    adapter._streams = {'can1': CanStream(host)}
    adapter._can_state = {'fdcan1': {'rx': {'completed_frames': 7}}}
    adapter._can_goals = {}

    def receipt(owner, stream, **options):
        receipts.append(options)
        assert options['completed_frames'] == 9
        expected = (struct.pack('<IIB3xQ64s', 0x201, 0, 1, 105, b'a')
                    + struct.pack('<IIB3xQ64s', 0x202, 0, 1, 105, b'b'))
        assert peer.recv(168) == expected
        return {'completed_frames': 9, 'rx_dropped': 0, 'partial_bytes': 0}

    monkeypatch.setattr(profile, 'wait_paused_can_input', receipt)
    try:
        adapter._receive_can('fdcan1', CanFrame(0x201, data=b'a'))
        adapter._receive_can('fdcan1', CanFrame(0x202, data=b'b'))
        assert not receipts and not events and adapter._can_goals == {'fdcan1': 9}
        adapter.commit_inputs(5)
        assert len(receipts) == len(events) == 1 and adapter._can_goals == {}
        assert adapter._can_state['fdcan1']['rx']['completed_frames'] == 9
        assert events[0][1]['source_time_ns'] == 105
        assert events[0][1]['mapped_time_ns'] == events[0][1]['delivery_time_ns'] == 5
        adapter.commit_inputs(5)
        assert len(receipts) == 1  # No retransmission or repeated receipt.
    finally:
        host.close()
        peer.close()


@pytest.mark.parametrize('changed', ['path', 'connected', 'schema'])
def test_aggregate_execution_refuses_wrong_native_identity(changed):
    state = {'schema': 1, 'execution': {'state': 'running', 'current-ns': 4, 'target-ns': 5},
             'status': {'running': True, 'status': 'running'},
             'fdcan-transports': {'dm-mc02.fdcan1': {'path': '/chardevs/can1', 'connected': True}}}
    if changed == 'schema':
        state['schema'] = True
    else:
        state['fdcan-transports']['dm-mc02.fdcan1'][changed] = (
            '/chardevs/other' if changed == 'path' else False)
    with pytest.raises((ValueError, RuntimeError, ConnectionError)):
        validate_can_execution_snapshot(state, {'dm-mc02.fdcan1': 'can1'})
