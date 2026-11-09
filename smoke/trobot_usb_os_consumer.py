#!/usr/bin/env python3
"""Use the unchanged trobot host transport against a real OS USB device."""
import argparse
import dataclasses
import json
import os
from pathlib import Path
import sys
import termios
import time
import tty

p=argparse.ArgumentParser()
p.add_argument('--trobot',type=Path,required=True)
p.add_argument('--dependencies',type=Path,required=True)
a=p.parse_args()
sys.path[:0]=[str(a.dependencies),str(a.trobot/'tools')]
from trobot_host.transport import discover_devices, UsbTransport
from trobot_host.protocol import Service, LogCommand, decode_info

items=discover_devices()
assert len(items)==1,items
with UsbTransport(items[0]) as transport:
    assert transport.debug_info.hardware_failures==0,transport.debug_info
    first=transport.ping()
    info=decode_info(transport.request(Service.LOG,LogCommand.INFO))
    # CDC ACM is bound by the kernel, while interface2 remains claimed by
    # unchanged libusb/PyUSB host tooling.
    ports=list(Path('/sys/class/tty').glob('ttyACM*'))
    own=[]
    for port in ports:
        parent=(port/'device').resolve()
        while parent!=parent.parent:
            if (parent/'idVendor').exists() and (parent/'idProduct').exists():
                if (parent/'idVendor').read_text().strip()=='0483' and (parent/'idProduct').read_text().strip()=='5741':own.append('/dev/'+port.name)
                break
            parent=parent.parent
    assert len(own)==1,own
    fd=os.open(own[0],os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs=termios.tcgetattr(fd)
        attrs[4]=attrs[5]=termios.B115200
        termios.tcsetattr(fd,termios.TCSANOW,attrs)
        assert os.write(fd,b'usbip-cdc-probe\r\n')==17
    finally:os.close(fd)
    # Repeated bulk requests cross packet boundaries in the real response.
    for _ in range(3):assert transport.ping()==first
    print(json.dumps({'device':dataclasses.asdict(items[0]),'debug':dataclasses.asdict(first),'log_info':dataclasses.asdict(info),'cdc':own,'cdc_write':17,'cdc_read':'ordinary firmware registers no CDC output consumer'},indent=2))
