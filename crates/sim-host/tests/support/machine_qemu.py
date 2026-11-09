#!/usr/bin/python3
"""Black-box QMP peer for machine boundary failure and ordering tests only."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import struct

args = sys.argv[1:]
firmware = Path(args[args.index('-kernel') + 1])
mode = firmware.read_text()
requests = firmware.with_suffix('.requests')
paths = []
for index, value in enumerate(args):
    if value == '-qmp':
        paths.append(('qmp', args[index + 1].split('unix:', 1)[1].split(',')[0]))
    elif value == '-chardev':
        fields = dict(field.split('=', 1) for field in args[index + 1].split(',') if '=' in field)
        paths.append((fields['id'], fields['path']))
listeners = {}
for name, path in paths:
    peer = socket.socket(socket.AF_UNIX)
    peer.bind(path)
    peer.listen(1)
    listeners[name] = peer
streams = {}
rx_completed = 0
def accept(name, listener):
    stream, _ = listener.accept()
    global rx_completed
    streams[name] = stream
    if name == 'cosim':
        sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
        from smoke.support import step_codec as wire
        def receive(size):
            data = b''
            while len(data) < size:
                chunk = stream.recv(size-len(data))
                if not chunk:
                    raise EOFError
                data += chunk
            return data
        try:
            while True:
                prefix = receive(4)
                body = receive(struct.unpack('<I', prefix)[0])
                frame = wire.decode_step_frame(prefix + body)
                header = frame.header
                if header.kind == wire.RESET:
                    payload = wire.encode_reset_ack_payload(wire.ResetAckPayload(0, 11, 900, 0))
                    kind = wire.RESET_ACK
                else:
                    wire.validate_step_input_payload(frame.payload)
                    with firmware.with_suffix('.steps').open('a') as output:
                        output.write(json.dumps({'step_id':header.step_id,'time':header.t_sim_ns,'dt':header.dt_ns,'payload':frame.payload.hex()})+'\n')
                    payload = wire.encode_step_ack_payload(wire.StepAckPayload(0, 0, 0))
                    kind = wire.STEP_ACK
                stream.sendall(wire.build_step_frame(kind,header.step_id,header.t_sim_ns,header.dt_ns,payload=payload,session_id=header.flags))
                if header.kind == wire.STEP and any(section.type == wire.STEP_SECTION_IMU_SAMPLE for section in wire.decode_step_payload(frame.payload)):
                    done = wire.StepDonePayload(2, 1, 2, 0, 0) if mode == 'imu-missing' else wire.StepDonePayload(0, 3, 0, 0, 0)
                    stream.sendall(wire.build_step_frame(wire.STEP_DONE,header.step_id,header.t_sim_ns,header.dt_ns,payload=wire.encode_step_done_payload(done),session_id=header.flags))
        except EOFError:
            return
    if name.startswith('can'):
        tail = b''
        while True:
            data = stream.recv(4096)
            if not data:
                return
            tail += data
            while len(tail) >= 84:
                tail = tail[84:]
                rx_completed += 2 if mode == 'can-rx-overrun' else 1
for name, listener in listeners.items():
    if name != 'qmp':
        threading.Thread(target=accept, args=(name, listener), daemon=True).start()
peer, _ = listeners['qmp'].accept()
def send(value):
    peer.sendall(json.dumps(value).encode() + b'\r\n')
send({'QMP': {'version': {'qemu': {'major': 8, 'minor': 2, 'micro': 2}, 'package': 'fixture'}, 'capabilities': []}})
origin, target = 900, None
key = False
def execution():
    if target is None:
        return {'state': 'idle', 'current-ns': origin}
    state = 'reached'
    if mode == 'barrier' and len(requests.read_text().splitlines()) < 2:
        state = 'running'
    return {'state': 'cancelled' if mode == 'interrupted' else state,
            'current-ns': target - 1 if mode == 'wrong-time' else target,
            'target-ns': target}
def snapshot():
    return {'schema': 1, 'board': 'fixture', 'timestamp_ns': origin if target is None else target,
            'vin_mv': 24000, 'gpio': {bank: {'moder': 0, 'sampled': (0 if key else 32768) if bank == 'A' else 0,
            'odr': 0, 'output_mask': 0} for bank in 'ABCDEFGH'},
            'led': {'pin': 'B0', 'rgb': 0, 'brightness': 0, 'source': 'gpio-latch'},
            'key': {'pin': 'A15', 'pressed': key, 'level': 0 if key else 1}}
for line in peer.makefile('rb'):
    command = json.loads(line)
    name, arguments = command['execute'], command.get('arguments', {})
    if name == 'query-commands':
        result = [{'name': item} for item in ['x-run-to-virtual-time', 'x-query-virtual-execution']]
    elif name == 'x-query-virtual-execution': result = execution()
    elif name == 'query-status': result = {'running': False, 'status': 'prelaunch' if target is None else 'paused'}
    elif name == 'qom-list': result = [{'name': item, 'type': 'json'} for item in ['board-state', 'can-execution-state']]
    elif name == 'qom-get' and arguments['property'] == 'can-execution-state':
        stamp = origin if target is None else target
        result = {'schema': 1, 'execution': execution(), 'status': {'running': False, 'status': 'prelaunch' if target is None else 'paused'},
                  'fdcan-transports': {'dm-mc02.fdcan1': {'path': '/chardevs/can1', 'connected': True}},
                  'fdcan-tx-state': {'schema': 1, 'timestamp_ns': stamp, 'controllers': {'dm-mc02.fdcan1': {
                    'pending_frames': 0, 'pending_bytes': 0, 'tx_dropped': int(mode == 'can-tx-drop' and target is not None), 'tx_short_writes': 0}}},
                  'fdcan-rx-state': {'schema': 1, 'timestamp_ns': stamp, 'controllers': {'dm-mc02.fdcan1': {
                    'completed_frames': rx_completed, 'partial_bytes': 0, 'rx_dropped': rx_completed}}}}
    elif name == 'qom-get': result = snapshot()
    elif name == 'x-run-to-virtual-time':
        target = arguments['target-ns']
        with requests.open('a') as stream: stream.write(f'{os.getpid()} {target}\n')
        if 'uart1' in streams: streams['uart1'].sendall(b'actual-peer\x00UART')
        if mode == 'can-good':
            streams['can1'].sendall(struct.pack('<IIB3xQ64s', 0x123, 0, 1, target - 10, b'\xa5'))
        result = {}
    elif name == 'qom-set':
        key = arguments['value']
        result = {}
    else: result = {}
    send({'return': result, 'id': command['id']})
