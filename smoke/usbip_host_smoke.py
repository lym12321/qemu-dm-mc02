#!/usr/bin/env python3
"""Native Lab + independent USB/IP peer + real Linux vhci/CDC/host tool gate.

Run sudo modprobe vhci_hcd first. The Lab helper records and cleans its exact
port; the test never detaches arbitrary system devices.
"""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from smoke.support.process import stop, identity
p=argparse.ArgumentParser()
for name in ('sim','qemu','elf','trobot','dependencies','report'):p.add_argument('--'+name,type=Path,required=True)
p.add_argument('--port',type=int,default=33240)
p.add_argument('--gdb-port',type=int,default=33350)
a=p.parse_args()
sys.path.insert(0,str(a.trobot/'tools'))
from trobot_host.protocol import encode_frame,decode_frame
report={'status':'failed','inputs':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in (a.sim,a.qemu,a.elf)}}
a.report.parent.mkdir(parents=True,exist_ok=True)
log=a.report.with_suffix('.console.log').open('wb')
child=None
before=Path('/sys/devices/platform/vhci_hcd.0/status').read_text()
try:
 child=subprocess.Popen([str(a.sim.resolve()),'lab','interactive','--elf',str(a.elf.resolve()),'--qemu',str(a.qemu.resolve()),'--usbip',f'127.0.0.1:{a.port}','--gdb-port',str(a.gdb_port),'--paused','--timeout','90'],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 report['sim_identity']=identity(child.pid)
 def command(text):child.stdin.write((text+'\n').encode());child.stdin.flush()
 command('key press');command('cont')
 def exact(s,n):
  data=b''
  while len(data)<n:
   part=s.recv(n-len(data));assert part,'USB/IP EOF';data+=part
  return data
 def available():
  end=time.monotonic()+12
  while time.monotonic()<end:
   assert child.poll() is None,'Lab exited'
   with socket.create_connection(('127.0.0.1',a.port),timeout=2) as s:
    s.sendall(bytes.fromhex('0111800500000000'))
    header=exact(s,12)
    if int.from_bytes(header[8:12],'big')==1:return
   time.sleep(.05)
  raise RuntimeError('USB device did not become available')
 # Wait for the listener before asking it to enumerate the actual guest.
 end=time.monotonic()+5
 while True:
  try:
   with socket.create_connection(('127.0.0.1',a.port),timeout=.2):break
  except OSError:
   assert time.monotonic()<end,'listener missing';time.sleep(.03)
 available()
 with socket.create_connection(('127.0.0.1',a.port),timeout=5) as s:
  s.sendall(bytes.fromhex('0111800300000000')+b'1-1'+bytes(29));assert exact(s,320)[:8]==bytes.fromhex('0111000300000000')
  def submit(seq,ep,in_,length,setup=bytes(8),data=b''):
   s.sendall(struct.pack('>10I',1,seq,0x10001,int(in_),ep,0,length,0,0,0)+setup+data)
  def response():
   b=exact(s,48);v=struct.unpack('>10I',b[:40]);payload=exact(s,v[6]) if v[0]==3 and v[3]==1 else b''
   return v,payload
  submit(10,0,False,0,bytes.fromhex('0009010000000000'));v,_=response();assert v[1]==10 and v[5]==0,v
  submit(11,4,True,16384)
  request=encode_frame(2,1,77);submit(12,5,False,len(request),data=request)
  results=dict()
  for _ in range(2):
   v,b=response();assert v[5]==0,v;results[v[1]]=b
  frame=decode_frame(results[11]);assert frame.request_id==77 and int(frame.status)==0
  submit(13,3,True,8)
  s.sendall(struct.pack('>6I',2,14,0x10001,0,0,13)+bytes(24))
  v,_=response();assert v[0]==4 and v[1]==14 and v[5]==0xffffff98,v
  # Windows usbip-win2's hub reset convention must re-enumerate, not become
  # an unsupported trobot class request. Then a fresh configuration works.
  submit(15,0,False,0,bytes.fromhex('2303040001000000'));v,_=response();assert v[1]==15 and v[5]==0,v
  submit(16,0,False,0,bytes.fromhex('0009010000000000'));v,_=response();assert v[1]==16 and v[5]==0,v
 report['wire']={'concurrent_in_out':'passed','unlink':'passed','windows_reset_convention':'passed'}
 available()
 # Reject hostile signed/large transfer length before reading an OUT body.
 with socket.create_connection(('127.0.0.1',a.port),timeout=5) as s:
  s.sendall(bytes.fromhex('0111800300000000')+b'1-1'+bytes(29));exact(s,320)
  s.sendall(struct.pack('>10I',1,99,0x10001,0,5,0,0xffffffff,0,0,0)+bytes(8))
  assert s.recv(1)==b''
 report['wire']['oversize_disconnect']='passed'
 available()
 command('usb attach')
 end=time.monotonic()+12
 while time.monotonic()<end:
  devices=[d for d in Path('/sys/bus/usb/devices').iterdir() if (d/'idVendor').exists() and (d/'idVendor').read_text().strip()=='0483' and (d/'idProduct').read_text().strip()=='5741']
  if devices and (devices[0]/'bConfigurationValue').read_text().strip()=='1':break
  assert child.poll() is None;time.sleep(.05)
 else:raise RuntimeError('OS did not enumerate/configure device')
 # OS disappearance on socket close alone cannot prove that the importer
 # retained its assigned port. Require the public Lab state to expose it.
 end=time.monotonic()+5
 while time.monotonic()<end:
  command('usb state')
  states=[json.loads(line[4:] if line.startswith('USB ') else line) for line in a.report.with_suffix('.console.log').read_text().splitlines() if line.startswith(('USB {','{"address"'))]
  owned=[state for state in states if state.get('local_port') is not None]
  if owned:report['owned_port']=owned[-1]['local_port'];break
  time.sleep(.05)
 else:raise RuntimeError('Lab did not retain its assigned USB port')
 consumer=subprocess.run(['sudo','-n',str(Path(sys.executable).absolute()),str(ROOT/'smoke/trobot_usb_os_consumer.py'),'--trobot',str(a.trobot),'--dependencies',str(a.dependencies)],capture_output=True,text=True,timeout=15)
 report['os_consumer']={'exit':consumer.returncode,'stdout':consumer.stdout,'stderr':consumer.stderr}
 assert consumer.returncode==0,report['os_consumer']
 # Native owner cleanup must detach the port even when the user quits while
 # the computer is still attached.
 command('quit');child.wait(timeout=12);assert child.returncode==0,child.returncode
 end=time.monotonic()+3
 while Path('/sys/devices/platform/vhci_hcd.0/status').read_text()!=before and time.monotonic()<end:time.sleep(.02)
 assert Path('/sys/devices/platform/vhci_hcd.0/status').read_text()==before,'owned vhci port leaked'
 assert identity(report['sim_identity']['pid'])!=report['sim_identity']
 report['cleanup']='passed';report['status']='passed'
finally:
 if child is not None:
  if child.poll() is None:
   try:command('quit');child.wait(timeout=12)
   finally:stop(child)
 log.close();a.report.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
