#!/usr/bin/env python3
"""Independent actual trobot consumer of the native guest USB bridge."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smoke.support.process import running_qemu, identity
from smoke.support.qmp import qmp_session_class

p = argparse.ArgumentParser()
p.add_argument('--qemu', type=Path, required=True)
p.add_argument('--elf', type=Path, required=True)
p.add_argument('--trobot', type=Path, required=True)
p.add_argument('--report', type=Path, required=True)
a = p.parse_args()
sys.path.insert(0, str(a.trobot / 'tools'))
from trobot_host.protocol import encode_frame, decode_frame, decode_debug_info

report = {'status': 'failed', 'inputs': {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in (a.qemu, a.elf)}}
argv = [str(a.qemu.resolve()), '-machine', 'dm-mc02', '-kernel', str(a.elf.resolve()),
        '-nodefaults', '-display', 'none', '-monitor', 'none', '-S',
        '-qmp', 'unix:{qmp},server=on,wait=off']
for _ in range(10): argv += ['-serial', 'null']
argv += ['-chardev', 'socket,id=usb,path={usb},server=on,wait=off', '-serial', 'chardev:usb']
try:
    with running_qemu(argv, sockets=['qmp', 'usb']) as native:
        report['identity'] = identity(native.process.pid)
        with socket.socket(socket.AF_UNIX) as usb:
            usb.settimeout(8)
            usb.connect(str(native.sockets['usb']))
            qmp = qmp_session_class()(str(native.sockets['qmp']))
            try:
                def exact(n):
                    b = b''
                    while len(b) < n:
                        assert native.process.poll() is None, 'QEMU exited'
                        part = usb.recv(n-len(b))
                        assert part, 'bridge EOF'
                        b += part
                    return b
                def recv():
                    size, = struct.unpack('>I', exact(4))
                    assert 16 <= size <= 1024*1024+16
                    b = exact(size)
                    kind, seq, status, actual = struct.unpack('>IIiI', b[:16])
                    return kind, seq, status, actual, b[16:]
                assert recv()[:3] == (4, 1, 0)
                qmp.command('qom-set', {'path': '/machine', 'property': 'user-key', 'value': True})
                qmp.command('cont')
                while recv()[:3] != (4, 1, 1): pass
                sequence = 0
                def transfer(kind=1, ep=0, in_=True, length=0, setup=bytes(8), data=b'', status=0):
                    global sequence
                    sequence += 1
                    body = struct.pack('>6I', kind, sequence, ep, in_, length, 0) + setup + data
                    usb.sendall(struct.pack('>I', len(body)) + body)
                    while True:
                        k, seq, result, actual, payload = recv()
                        if k == 4: continue
                        assert (k, seq, result) == (kind, sequence, status), (k, seq, result, actual)
                        return payload if in_ else actual
                def control(bm, request, value=0, index=0, length=0, data=b'', status=0):
                    setup = struct.pack('<BBHHH', bm, request, value, index, length)
                    return transfer(in_=bool(bm & 128), length=length, setup=setup, data=data, status=status)
                transfer(kind=3, in_=False)
                device = control(0x80, 6, 0x100, length=18)
                assert len(device) == 18 and device[8:12] == bytes.fromhex('83044157'), device.hex()
                control(0, 5, 1)
                config = control(0x80, 6, 0x200, length=255)
                assert len(config) == int.from_bytes(config[2:4], 'little') and config[4] == 3
                bos = control(0x80, 6, 0xf00, length=255)
                msos = control(0xc0, 0x17, index=7, length=512)
                assert b'WINUSB' in msos
                control(0, 9, 1)
                coding = bytes.fromhex('00960000000008')
                control(0x21, 0x20, length=7, data=coding)
                assert control(0xa1, 0x21, length=7) == coding
                control(0x21, 0x22, 3)
                control(0x80, 0xff, status=-32)
                assert control(0x80, 6, 0x100, length=18) == device
                # Actual existing trobot protocol encoder/decoder, no firmware edits.
                request = encode_frame(2, 1, 42)
                assert transfer(ep=5, in_=False, length=len(request), data=request) == len(request)
                frame = decode_frame(transfer(ep=4, length=16384))
                assert (frame.service, frame.command, frame.request_id, int(frame.status)) == (2, 1, 42, 0)
                info = decode_debug_info(frame.payload)
                assert info.hardware_failures == 0, info
                report.update(device=device.hex(), config=config.hex(), bos=bos.hex(), msos_bytes=len(msos), debug_info=vars(info))
            except BaseException:
                qmp.command('stop')
                for cmd in ['info registers', 'xp /8wx 0x40040000', 'xp /16wx 0x40040800', 'xp /4wx 0x40040024', 'xp /16wx 0x40040100', 'xp /8wx 0x40040900', 'xp /8wx 0x40040b00']:
                    print(cmd, qmp.command('human-monitor-command', {'command-line': cmd}), flush=True)
                raise
            finally:
                qmp.close()
    assert identity(report['identity']['pid']) != report['identity']
    report['status'] = 'passed'
finally:
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
