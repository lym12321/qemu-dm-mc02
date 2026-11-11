"use strict";

// xterm owns UTF-8/VT parsing and screen state. Raw history is only for HEX.
class UartConsole {
  static byteLimit = 262144;

  constructor(pane, count, tx) {
    this.pane = pane;
    this.count = count;
    this.tx = tx;
    this.buffers = new Map();
    this.hex = document.createElement("pre");
    this.hex.className = "uart-hex mono";
    pane.append(this.hex);
    new ResizeObserver(() => this.fit()).observe(pane);
  }

  buffer(endpoint) {
    if (!this.buffers.has(endpoint)) {
      const terminal = new Terminal({convertEol: true, disableStdin: true, scrollback: 1000,
        fontSize: 13, fontFamily: '"Cascadia Code", Consolas, monospace',
        theme: {background: "#ffffff", foreground: "#202124"}});
      const fit = new FitAddon.FitAddon();
      terminal.loadAddon(fit);
      const host = document.createElement("div");
      host.className = "uart-terminal";
      host.hidden = true;
      this.pane.append(host);
      this.buffers.set(endpoint, {terminal, fit, host, chunks: [], bytes: 0, received: 0,
        pending: 0, blocked: false, tx: ""});
    }
    return this.buffers.get(endpoint);
  }

  receive(endpoint, bytes) {
    const buffer = this.buffer(endpoint);
    buffer.chunks.push(bytes);
    buffer.bytes += bytes.length;
    buffer.received += bytes.length;
    while (buffer.bytes > UartConsole.byteLimit) {
      const excess = buffer.bytes - UartConsole.byteLimit;
      const first = buffer.chunks[0];
      if (first.length <= excess) {
        buffer.chunks.shift();
        buffer.bytes -= first.length;
      } else {
        buffer.chunks[0] = first.slice(excess);
        buffer.bytes -= excess;
      }
    }
    if (buffer.blocked) return;
    if (buffer.pending + bytes.length > UartConsole.byteLimit) {
      buffer.blocked = true;
      return;
    }
    const viewport = buffer.terminal.buffer.active.viewportY;
    buffer.pending += bytes.length;
    buffer.terminal.write(bytes, () => {
      buffer.pending -= bytes.length;
      if (this.follow) buffer.terminal.scrollToBottom();
      else buffer.terminal.scrollToLine(viewport);
    });
  }

  render(endpoint, hex, follow) {
    this.endpoint = endpoint;
    this.follow = follow;
    const buffer = this.buffer(endpoint);
    const reveal = buffer.host.hidden && !hex;
    for (const other of this.buffers.values()) other.host.hidden = other !== buffer || hex;
    this.hex.hidden = !hex;
    if (hex) {
      const top = this.hex.scrollTop;
      this.hex.textContent = buffer.chunks.map(bytes => Array.from(bytes,
        byte => byte.toString(16).padStart(2, "0").toUpperCase()).join(" ")).join(" ");
      this.hex.scrollTop = follow ? this.hex.scrollHeight : top;
    } else if (reveal) {
      if (!buffer.terminal.element) buffer.terminal.open(buffer.host);
      this.fit();
    }
    this.count.textContent = `RX ${buffer.received} B`
      + (buffer.received > buffer.bytes ? ` · HEX 已省略 ${buffer.received - buffer.bytes} B` : "")
      + (buffer.blocked ? " · 终端积压，清空后恢复" : "");
    this.tx.textContent = buffer.tx;
  }

  fit() {
    const buffer = this.buffers.get(this.endpoint);
    if (buffer?.terminal.element && buffer.host.clientWidth && buffer.host.clientHeight) buffer.fit.fit();
  }

  clear(endpoint) {
    const buffer = this.buffers.get(endpoint);
    if (!buffer) return;
    buffer.terminal.dispose();
    buffer.host.remove();
    this.buffers.delete(endpoint);
  }

  reset() {
    for (const endpoint of this.buffers.keys()) this.clear(endpoint);
  }

  accepted(endpoint, bytes) {
    this.buffer(endpoint).tx = `TX ${bytes} B · 已接受`;
  }
}
