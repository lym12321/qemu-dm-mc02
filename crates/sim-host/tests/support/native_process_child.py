"""Actual OS child for Rust lifecycle tests; never a production launcher."""

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

mode, record, path = sys.argv[1:4]
Path(record).write_text(f"{os.getpid()}\n{Path(path).parent}\n{os.getpgrp()}\n")
if mode == "exit0":
    sys.stderr.write("discard-prefix" + "x" * 13000 + "retained-tail\n")
    sys.stderr.flush()
    sys.exit(0)
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "descendant":
    child = subprocess.Popen([
        sys.executable, "-u", "-c",
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); signal.pause()",
    ], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline() == "ready\n"
    with open(record, "a") as output:
        output.write(f"{child.pid}\n")
if mode == "regular-file":
    Path(path).write_text("not a socket")
else:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(path)
    listener.listen()
    if mode in ("ready", "active-exit0"):
        connection, _ = listener.accept()
        connection.sendall(b"transport only")
        connection.recv(1)
        if mode == "active-exit0":
            sys.exit(0)
signal.pause()
