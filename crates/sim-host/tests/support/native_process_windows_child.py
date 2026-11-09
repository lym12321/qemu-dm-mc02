"""Actual Windows process/tree and TCP peer; independent test input only."""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading

mode, record, literal, *ports = sys.argv[1:]
record = Path(record)
descendant = None
if mode.startswith("descendant"):
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    descendant = subprocess.Popen([
        sys.executable, "-u", "-c",
        "import signal,threading; signal.signal(signal.SIGBREAK,signal.SIG_IGN); "
        "print('ready',flush=True); threading.Event().wait()",
    ], stdout=subprocess.PIPE, text=True)
    assert descendant.stdout.readline() == "ready\n"
record.write_text(json.dumps({
    "pid": os.getpid(),
    "descendant": None if descendant is None else descendant.pid,
    "literal": literal,
    "ports": [int(port) for port in ports],
}), encoding="utf-8")
if mode == "exit0":
    sys.stderr.write("discard-prefix" + "x" * 16000 + "retained-tail\n")
    sys.stderr.flush()
    sys.exit(0)
if mode == "idle":
    threading.Event().wait()
connections = [socket.create_connection(("127.0.0.1", int(port)), timeout=3) for port in ports]
for connection in connections:
    connection.settimeout(None)
connections[0].sendall(b"transport only")
connections[1].sendall(b"uart\x00\xff")
connections[0].recv(1)
if mode == "descendant-exit0":
    sys.exit(0)
threading.Event().wait()
