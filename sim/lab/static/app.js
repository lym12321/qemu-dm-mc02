"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const names = {idle: "未加载", starting: "正在加载", paused: "已暂停", running: "运行中",
    stopping: "正在停止", stopped: "已停止", failed: "运行失败"};
  const modes = ["输入", "输出", "复用", "模拟"];
  const modeTags = ["I", "O", "AF", "A"];
  const inactive = new Set(["idle", "stopped", "failed"]);
  const pending = new Map();
  const pinButtons = new Map();
  const records = [];
  const logLimit = 600;
  const byteLimit = 262144;
  let state = {status: "idle", session_id: null, firmware: null, board: null, pins: []};
  let socket = null;
  let connected = false;
  let uploading = false;
  let commandNumber = 0;
  let reconnectTimer = null;
  let logBytes = 0;
  let localDropped = 0;
  let lastSeq = 0n;
  let lastLogSession;
  let keyHeld = false;
  let logFrame = null;
  const requestPrefix = Math.random().toString(36).slice(2, 10);
  const encoder = new TextEncoder();
  const decoder = new TextDecoder("utf-8");

  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").classList.toggle("error", error);
    $("notice").hidden = !message;
  }

  function ns(value) {
    try {
      const number = BigInt(value);
      return `${number / 1000000000n}.${(number % 1000000000n).toString().padStart(9, "0").slice(0, 6)} s`;
    } catch (_) { return "—"; }
  }

  function integer(value, label, maximum) {
    if (!/^(?:0[xX][0-9a-fA-F]+|[0-9]+)$/.test(value.trim())) {
      throw new Error(`${label}应为非负整数，可使用 0x 十六进制。`);
    }
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number < 0 || number > maximum) {
      throw new Error(`${label}范围为 0–${maximum}。`);
    }
    return number;
  }

  function hexBytes(value, maximum) {
    const text = value.replace(/\s/g, "");
    if (!/^(?:[0-9a-fA-F]{2})*$/.test(text)) throw new Error("HEX 内容应为完整的字节对，例如 00 FF 41。");
    if (text.length / 2 > maximum) throw new Error(`最多发送 ${maximum} 字节。`);
    return Uint8Array.from(text.match(/../g) || [], (pair) => parseInt(pair, 16));
  }

  function toHex(bytes) { return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join(" "); }
  function fromBase64(value) { return Uint8Array.from(atob(value), (char) => char.charCodeAt(0)); }
  function toBase64(bytes) { return btoa(Array.from(bytes, (value) => String.fromCharCode(value)).join("")); }
  function safeText(bytes) {
    return decoder.decode(bytes).replace(/[\x00-\x1f\x7f-\x9f]/g, (char) => {
      const value = char.charCodeAt(0);
      return ({9: "\\t", 10: "\\n", 13: "\\r"})[value] || `\\x${value.toString(16).padStart(2, "0")}`;
    });
  }

  function displayRecord(record) {
    if (record.kind === "uart") return $("log-format").value === "hex" ? toHex(record.data) : safeText(record.data);
    return record.text;
  }

  function renderLog() {
    const log = $("log");
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 45;
    const filter = $("log-filter").value;
    const fragment = document.createDocumentFragment();
    let count = 0;
    for (const record of records) {
      if (filter !== "all" && (filter === "system" ? ["uart", "can"].includes(record.kind) : record.kind !== filter)) continue;
      const row = document.createElement("div");
      row.className = "log-row";
      row.dataset.kind = record.kind;
      if (record.seq) row.dataset.seq = record.seq;
      if (record.session !== undefined) row.dataset.sessionId = record.session || "";
      const meta = document.createElement("span");
      meta.className = "log-meta";
      meta.textContent = record.meta;
      const content = document.createElement("span");
      content.className = "log-data";
      content.textContent = displayRecord(record);
      row.append(meta, content);
      fragment.append(row);
      count += 1;
    }
    if (!count) {
      const empty = document.createElement("p");
      empty.id = "log-empty";
      empty.textContent = "暂无符合条件的记录。";
      fragment.append(empty);
    }
    log.replaceChildren(fragment);
    $("log-count").textContent = `${records.length} 条记录${localDropped ? ` · 本页已省略 ${localDropped} 条` : ""}`;
    if (atEnd) log.scrollTop = log.scrollHeight;
  }

  function appendRecord(record) {
    record.size = record.data ? record.data.length : encoder.encode(record.text || "").length;
    records.push(record);
    logBytes += record.size;
    while (records.length > logLimit || logBytes > byteLimit) {
      logBytes -= records.shift().size;
      localDropped += 1;
    }
    if (logFrame === null) logFrame = requestAnimationFrame(() => { logFrame = null; renderLog(); });
  }

  function systemLog(text, error = false) {
    appendRecord({kind: error ? "error" : "system", meta: "实验台", text});
  }

  function channelEvent(event) {
    if (typeof event.seq !== "string" || !/^\d+$/.test(event.seq)) return;
    const sequence = BigInt(event.seq);
    if (sequence <= lastSeq) return;
    lastSeq = sequence;
    if (event.session_id !== lastLogSession) {
      lastLogSession = event.session_id;
      appendRecord({kind: "session", session: event.session_id, meta: "会话", text: event.session_id || "无活动会话"});
    }
    const common = {seq: event.seq, session: event.session_id};
    if (event.type === "uart") {
      appendRecord({...common, kind: "uart", meta: `${event.endpoint.toUpperCase()} 输出\n${ns(event.timestamp_ns)}`,
        data: fromBase64(event.data_b64)});
    } else if (event.type === "can") {
      appendRecord({...common, kind: "can", meta: `${event.endpoint.toUpperCase()} 输出\n${ns(event.timestamp_ns)}`,
        text: `ID 0x${event.can_id.toString(16).toUpperCase()}  flags=${event.flags}  DLC=${event.dlc}  ${event.data_hex.match(/../g)?.join(" ").toUpperCase() || "∅"}`});
    } else if (event.type === "gap") {
      appendRecord({...common, kind: "gap", meta: "服务端记录缺口", text: `有界历史累计省略 ${event.count} 条 / ${event.bytes} 字节。`});
    } else {
      appendRecord({...common, kind: "system", meta: "会话", text: String(event.message || event.text || event.type)});
    }
  }

  function updateControls() {
    const active = ["paused", "running"].includes(state.status);
    const transitioning = ["starting", "stopping"].includes(state.status);
    const lifecyclePending = Array.from(pending.values()).some((item) => ["load", "run", "pause", "stop", "restart"].includes(item.action));
    $("firmware-input").disabled = uploading || !inactive.has(state.status) || lifecyclePending;
    document.querySelector(".upload-button").classList.toggle("disabled", $("firmware-input").disabled);
    $("load-button").disabled = !connected || uploading || !state.firmware || !inactive.has(state.status) || lifecyclePending;
    $("run-button").disabled = !connected || state.status !== "paused" || lifecyclePending;
    $("pause-button").disabled = !connected || state.status !== "running" || lifecyclePending;
    $("stop-button").disabled = !connected || !["starting", "paused", "running"].includes(state.status);
    $("restart-button").disabled = !connected || uploading || !state.firmware || transitioning || lifecyclePending;
    for (const id of ["key-button", "vin-send", "gpio-send", "uart-send", "can-send"]) $(id).disabled = !connected || !active;
    $("adc-send").disabled = !connected || state.status !== "running";
    $("adc-hint").textContent = state.status === "paused" ? "输入刺激，非传感器读数 · 请先运行再发送" : "输入刺激，非传感器读数 · 仅运行时可发送";
  }

  function selectPin(pin) {
    $("gpio-pin").value = pin;
    $("selected-pin").textContent = pin;
    for (const [name, button] of pinButtons) button.dataset.selected = String(pin === name);
    const observed = (state.pins || []).find((item) => item.pin === pin);
    $("pin-summary").textContent = observed ? `${modes[observed.mode]} · 采样 ${observed.level} · ODR ${observed.odr} · ${observed.output ? "输出掩码有效" : "输出掩码无效"}` : "等待板卡快照";
  }

  function applyState(value) {
    const changedOwner = value.owner_id !== state.owner_id;
    const changedSession = value.session_id !== state.session_id;
    const recovered = state.error && !value.error;
    if (changedOwner) {
      records.length = 0;
      logBytes = 0;
      localDropped = 0;
      lastSeq = 0n;
      lastLogSession = undefined;
      renderLog();
    }
    state = value;
    if (changedOwner || changedSession) keyHeld = false;
    if (recovered || (changedSession && !value.error)) notice("");
    $("session-status").textContent = names[value.status] || value.status;
    $("session-status").dataset.status = value.status;
    $("firmware-name").textContent = value.firmware?.name || "尚未选择固件";
    $("firmware-hint").textContent = uploading ? "正在上传…" : "上传后加载，初始停在复位处 · 最大 64 MiB";
    $("remaining-time").textContent = typeof value.remaining_seconds === "number" ? `${Math.max(0, Math.ceil(value.remaining_seconds))} s` : "—";
    $("gdb-address").textContent = value.gdb_port ? `GDB 127.0.0.1:${value.gdb_port}` : "GDB 未启用";
    $("observation-status").textContent = !value.board ? "等待板卡快照" : value.observation_stale ? "最后快照 · 已过期" : "原生快照";
    $("observation-status").dataset.stale = String(Boolean(value.observation_stale));
    $("virtual-time").textContent = value.board ? ns(value.board.timestamp_ns) : "—";
    $("raw-state").textContent = JSON.stringify(value.board, null, 2);
    const board = value.board;
    $("key-value").textContent = board ? board.key.pressed ? "按下" : "松开" : "—";
    $("key-level").textContent = board ? `采样电平 ${board.key.level} · 按住操作` : "按住按钮或空格键";
    $("key-pin").textContent = board?.key.pin || "A15";
    $("key-button").setAttribute("aria-pressed", String(Boolean(board?.key.pressed)));
    $("led-value").textContent = board ? `#${board.led.rgb.toString(16).padStart(6, "0").toUpperCase()}` : "—";
    $("led-pin").textContent = board?.led.pin || "A7";
    $("led-source").textContent = board ? `${board.led.source === "ws2812-dma" ? "WS2812 DMA 波形" : "GPIO latch 近似"} · 亮度 ${board.led.brightness}` : "等待原生观察";
    $("board-led").setAttribute("fill", board?.led.rgb ? `#${board.led.rgb.toString(16).padStart(6, "0")}` : "#526a5c");
    $("vin-value").textContent = board ? `${board.vin_mv} mV` : "—";
    for (const button of pinButtons.values()) { button.dataset.unknown = "true"; button.dataset.level = ""; button.textContent = "·"; }
    for (const pin of value.pins || []) {
      const button = pinButtons.get(pin.pin);
      if (!button) continue;
      button.dataset.unknown = "false";
      button.dataset.level = String(pin.level);
      button.dataset.mode = String(pin.mode);
      button.textContent = modeTags[pin.mode] || "?";
      button.title = `${pin.pin} · ${modes[pin.mode]} · 采样 ${pin.level} · ODR ${pin.odr}`;
      button.setAttribute("aria-label", button.title);
    }
    selectPin($("gpio-pin").value);
    if (value.error) notice([value.error, ...(value.notes || [])].join("\n"), true);
    updateControls();
  }

  function send(action, args = {}, quiet = false) {
    if (!connected || !socket || socket.readyState !== WebSocket.OPEN) {
      notice("连接已断开；请等待重新连接后再操作。", true);
      return false;
    }
    const id = `${requestPrefix}-${++commandNumber}`;
    pending.set(id, {action, args, quiet});
    try { socket.send(JSON.stringify({id, session_id: state.session_id ?? null, action, args})); }
    catch (error) { pending.delete(id); notice(error.message, true); return false; }
    updateControls();
    return true;
  }

  function commandResult(event) {
    const request = pending.get(event.id);
    if (!request) return;
    pending.delete(event.id);
    if (!event.ok) {
      const error = String(event.error || "请求被拒绝。");
      notice(error, true);
      systemLog(`${request.action}：${error}`, true);
    } else if (!request.quiet) {
      notice("请求已接受；请观察实际板卡或通道输出。");
      const labels = {load: "加载", run: "运行", pause: "暂停", stop: "停止", restart: "重新加载", gpio: "GPIO 输入", vin: "VIN 输入", uart: "UART 输入", can: "CAN 输入", adc: "ADC1 输入"};
      systemLog(`${labels[request.action] || request.action}请求已接受。`);
    }
    updateControls();
  }

  function connect() {
    clearTimeout(reconnectTimer);
    socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/ws`);
    socket.addEventListener("open", () => {
      connected = true;
      $("connection-status").textContent = "已连接";
      $("connection-dot").classList.add("connected");
      updateControls();
    });
    socket.addEventListener("message", (message) => {
      try {
        const event = JSON.parse(message.data);
        if (event.type === "state") applyState(event);
        else if (event.type === "command_result") commandResult(event);
        else channelEvent(event);
      } catch (error) { notice(`无法显示服务端消息：${error.message}`, true); }
    });
    socket.addEventListener("close", () => {
      connected = false;
      keyHeld = false;
      if (pending.size) systemLog(`${pending.size} 个请求的结果尚未收到；重连后不会重发。`);
      pending.clear();
      $("connection-status").textContent = "连接中断 · 正在重连";
      $("connection-dot").classList.remove("connected");
      updateControls();
      reconnectTimer = setTimeout(connect, 1500);
    });
    socket.addEventListener("error", () => { $("connection-status").textContent = "连接不可用"; });
  }

  function pressKey(event) {
    if ($("key-button").disabled || keyHeld) return;
    event.preventDefault();
    keyHeld = send("key", {pressed: true}, true);
    if (event.pointerId !== undefined) $("key-button").setPointerCapture(event.pointerId);
  }
  function releaseKey() {
    if (!keyHeld) return;
    keyHeld = false;
    send("key", {pressed: false}, true);
  }

  function form(id, action, args) {
    $(id).addEventListener("submit", (event) => {
      event.preventDefault();
      try { send(action, args()); }
      catch (error) { notice(error.message, true); }
    });
  }

  function initialize() {
    const grid = $("gpio-grid");
    const blank = document.createElement("span");
    grid.append(blank);
    for (let bit = 0; bit < 16; bit += 1) {
      const label = document.createElement("span");
      label.className = "gpio-label";
      label.textContent = bit;
      grid.append(label);
    }
    for (const port of "ABCDEFGH") {
      const label = document.createElement("span");
      label.className = "gpio-label gpio-port";
      label.textContent = port;
      grid.append(label);
      for (let bit = 0; bit < 16; bit += 1) {
        const pin = `${port}${bit}`;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "gpio-pin";
        button.dataset.pin = pin;
        button.dataset.unknown = "true";
        button.textContent = "·";
        button.title = pin;
        button.setAttribute("aria-label", pin);
        button.addEventListener("click", () => selectPin(pin));
        pinButtons.set(pin, button);
        grid.append(button);
        $("gpio-pin").add(new Option(pin, pin));
      }
    }
    for (let channel = 0; channel < 32; channel += 1) $("adc-channel").add(new Option(`CH${channel}`, channel));
    selectPin("B0");
    for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click", () => send(button.dataset.action));
    for (const button of document.querySelectorAll("[data-tab]")) {
      button.addEventListener("click", () => {
        for (const tab of document.querySelectorAll("[data-tab]")) {
          const selected = tab === button;
          tab.setAttribute("aria-selected", String(selected));
          $(tab.getAttribute("aria-controls")).hidden = !selected;
        }
      });
    }
    $("gpio-pin").addEventListener("change", () => selectPin($("gpio-pin").value));
    $("key-button").addEventListener("pointerdown", pressKey);
    $("key-button").addEventListener("pointerup", releaseKey);
    $("key-button").addEventListener("pointercancel", releaseKey);
    $("key-button").addEventListener("lostpointercapture", releaseKey);
    $("key-button").addEventListener("keydown", (event) => { if (event.code === "Space" || event.code === "Enter") pressKey(event); });
    $("key-button").addEventListener("keyup", (event) => { if (event.code === "Space" || event.code === "Enter") { event.preventDefault(); releaseKey(); } });
    $("key-button").addEventListener("blur", releaseKey);
    window.addEventListener("blur", releaseKey);
    window.addEventListener("pagehide", releaseKey);
    document.addEventListener("visibilitychange", () => { if (document.hidden) releaseKey(); });
    form("vin-form", "vin", () => ({millivolts: integer($("vin-input").value, "VIN", 4294967295)}));
    form("gpio-form", "gpio", () => ({pin: $("gpio-pin").value, level: Number($("gpio-level").value)}));
    form("uart-form", "uart", () => {
      const payload = $("uart-mode").value === "hex" ? hexBytes($("uart-data").value, 4096) : encoder.encode($("uart-data").value);
      const suffix = encoder.encode(({none: "", lf: "\n", crlf: "\r\n", cr: "\r"})[$("uart-newline").value]);
      if (payload.length + suffix.length > 4096) throw new Error("UART 单次最多发送 4096 字节，包含追加换行。");
      const bytes = new Uint8Array(payload.length + suffix.length);
      bytes.set(payload); bytes.set(suffix, payload.length);
      return {endpoint: $("uart-endpoint").value, data_b64: toBase64(bytes)};
    });
    form("can-form", "can", () => ({endpoint: $("can-endpoint").value,
      can_id: integer($("can-id").value, "CAN ID", 536870911), flags: integer($("can-flags").value, "Flags", 15),
      dlc: integer($("can-dlc").value, "DLC", 15), data_hex: toHex(hexBytes($("can-data").value, 64)).replaceAll(" ", "")}));
    form("adc-form", "adc", () => {
      const mode = $("adc-mode").value;
      return {channel: Number($("adc-channel").value), [mode]: integer($("adc-value").value, mode, mode === "raw" ? 65535 : 3300000)};
    });
    $("adc-mode").addEventListener("change", () => { $("adc-value").max = $("adc-mode").value === "raw" ? "65535" : "3300000"; });
    $("uart-mode").addEventListener("change", () => { $("uart-data").placeholder = $("uart-mode").value === "hex" ? "例如 00 FF 41 0A" : "输入要发送的文本"; });
    $("log-filter").addEventListener("change", renderLog);
    $("log-format").addEventListener("change", renderLog);
    $("clear-log").addEventListener("click", () => { records.length = 0; logBytes = 0; localDropped = 0; renderLog(); });
    $("firmware-input").addEventListener("change", async () => {
      const file = $("firmware-input").files[0];
      if (!file) return;
      if (file.size > 64 * 1024 * 1024) { notice("ELF 文件不能超过 64 MiB。", true); $("firmware-input").value = ""; return; }
      uploading = true; updateControls(); $("firmware-hint").textContent = "正在上传…";
      try {
        const body = new FormData(); body.append("firmware", file);
        const response = await fetch("/api/firmware", {method: "POST", body});
        const value = await response.json();
        if (!response.ok) throw new Error(value.error || `上传失败（HTTP ${response.status}）。`);
        applyState(value);
        notice("固件已选择。点击“加载”创建暂停在复位处的新会话。");
      } catch (error) { notice(error.message, true); }
      finally { uploading = false; $("firmware-input").value = ""; $("firmware-hint").textContent = "上传后加载，初始停在复位处 · 最大 64 MiB"; updateControls(); }
    });
    updateControls();
  }

  initialize();
  fetch("/api/state").then((response) => {
    if (!response.ok) throw new Error(`读取状态失败（HTTP ${response.status}）。`);
    return response.json();
  }).then(applyState).catch((error) => notice(error.message, true)).finally(connect);
})();
