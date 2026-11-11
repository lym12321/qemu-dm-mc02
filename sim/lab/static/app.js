"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  function setText(id, text) {
    const element = $(id);
    // Replacing an unchanged slotted label can interrupt a held mouse click.
    if (element.textContent !== text) element.textContent = text;
  }
  mdui.setColorScheme("#36618e");
  const UARTS = ["uart1", "uart2", "uart3", "uart5", "uart7", "uart10"];
  const CANS = ["can1", "can2", "can3"];
  const portName = (port) => `${port.toUpperCase()}${["uart2", "uart3"].includes(port) ? " · RS485" : ""}`;
  const names = {idle: "未启动", starting: "启动中", paused: "已暂停", running: "运行中", stopping: "停止中", stopped: "已停止", failed: "失败"};
  const modes = ["输入", "输出", "复用", "模拟"];
  const encoder = new TextEncoder();
  const pending = new Map();
  const uart = new UartConsole($("uart-rx"), $("uart-count"), $("uart-tx-status"));
  const canRecords = [];
  const events = [];
  const requestPrefix = Math.random().toString(36).slice(2, 10);
  const LIFECYCLE = new Set(["start", "load", "run", "pause", "stop", "restart"]);
  let state = {status: "idle", session_id: null, firmware: null, board: null, pins: []};
  let project = {schema: "sim/lab-project/v1", firmware: null, watch_elf: false, uarts: [...UARTS], cans: [...CANS], adc: true,
    startup: "run", user_hold_ms: 1000, startup_timeout_secs: 10, timeout_secs: 3600,
    imu: "stationary", gdb_port: null, usbip: null, actions: {}};
  let projectPath = null;
  let projectLoaded = false;
  let socket = null;
  let connected = false;
  let uploading = null;
  let saving = false;
  let commandNumber = 0;
  let reconnectTimer = null;
  let keyHeld = false;
  let lastSeq = 0n;
  let lastLogSession;
  let renderFrame = null;
  const renderQueue = new Set();
  let lastStateError = null;
  let sessionCommandError = null;
  let sessionOperation = null;

  function showError(area, message) {
    const element = $(`${area}-error`);
    element.textContent = message || "";
    element.hidden = !message;
  }
  function eventLog(message, error = false) {
    events.push({time: new Date().toLocaleTimeString("zh-CN", {hour12: false}), message, error});
    if (events.length > 600) events.shift();
    scheduleRender(renderEvents);
  }
  function fail(area, error) {
    const message = error instanceof Error ? error.message : String(error);
    if (area === "session") sessionCommandError = message;
    showError(area, message);
    eventLog(message, true);
  }
  function ns(value, unit = true) {
    try {
      const number = BigInt(value);
      return `${number / 1000000000n}.${(number % 1000000000n).toString().padStart(9, "0").slice(0, 6)}${unit ? " s" : ""}`;
    } catch (_) { return "—"; }
  }
  function integer(value, label, maximum) {
    if (!/^(?:0[xX][0-9a-fA-F]+|[0-9]+)$/.test(String(value).trim())) throw new Error(`${label}需要非负整数。`);
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number < 0 || number > maximum) throw new Error(`${label}范围为 0–${maximum}。`);
    return number;
  }
  function hexBytes(value, maximum) {
    const text = value.replace(/\s/g, "");
    if (!/^(?:[0-9a-fA-F]{2})*$/.test(text)) throw new Error("HEX 需要完整的字节对。");
    if (text.length / 2 > maximum) throw new Error(`最多 ${maximum} 字节。`);
    return Uint8Array.from(text.match(/../g) || [], (pair) => parseInt(pair, 16));
  }
  function toHex(bytes) { return Array.from(bytes, (value) => value.toString(16).padStart(2, "0").toUpperCase()).join(" "); }
  function fromBase64(value) { return Uint8Array.from(atob(value), (char) => char.charCodeAt(0)); }
  function toBase64(bytes) { return btoa(Array.from(bytes, (value) => String.fromCharCode(value)).join("")); }
  function renderUart() {
    if (!$("console-uart").hidden) uart.render($("uart-endpoint").value,
      $("uart-receive-format").value === "hex", $("uart-scroll").checked);
  }
  function frameType(flags) {
    return [flags & 1 ? "EXT" : "STD", flags & 2 ? "RTR" : "", flags & 4 ? "FD" : "", flags & 8 ? "BRS" : ""].filter(Boolean).join(" ");
  }
  function renderCan() {
    if ($("console-can").hidden) return;
    const pane = $("can-rx");
    const scrollTop = pane.scrollTop;
    const fragment = document.createDocumentFragment();
    let count = 0;
    for (const record of canRecords) {
      if (record.endpoint !== $("can-endpoint").value) continue;
      const row = document.createElement("tr");
      row.dataset.direction = record.direction;
      for (const value of [record.timestamp_ns == null ? "—" : ns(record.timestamp_ns, false), record.direction,
        `0x${record.can_id.toString(16).toUpperCase()}`, frameType(record.flags), record.data.length,
        toHex(record.data)]) {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      }
      row.title = `DLC ${record.dlc}${record.direction === "TX" ? " · 后端已接受" : ""}`;
      fragment.append(row);
      count += 1;
    }
    $("can-records").replaceChildren(fragment);
    $("can-count").textContent = `${count} 帧`;
    pane.scrollTop = $("can-scroll").checked ? pane.scrollHeight : scrollTop;
  }
  function renderEvents() {
    if (!$("events-dialog").open) return;
    const pane = $("event-log");
    const atEnd = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 45;
    const fragment = document.createDocumentFragment();
    for (const event of events) {
      const row = document.createElement("div"); row.className = "event-row"; row.dataset.error = String(event.error);
      const time = document.createElement("time"); time.textContent = event.time;
      const text = document.createElement("span"); text.textContent = event.message;
      row.append(time, text); fragment.append(row);
    }
    pane.replaceChildren(fragment);
    if (atEnd) pane.scrollTop = pane.scrollHeight;
  }
  function renderConsole() { renderUart(); renderCan(); renderEvents(); }
  function scheduleRender(render = renderConsole) {
    renderQueue.add(render);
    if (renderFrame === null) renderFrame = requestAnimationFrame(() => {
      renderFrame = null;
      for (const update of renderQueue) update();
      renderQueue.clear();
    });
  }
  function addCan(record) {
    canRecords.push(record);
    if (canRecords.length > 1000) canRecords.shift();
    scheduleRender(renderCan);
  }
  function channelEvent(event) {
    if (typeof event.seq !== "string" || !/^\d+$/.test(event.seq)) return;
    const sequence = BigInt(event.seq);
    if (sequence <= lastSeq) return;
    lastSeq = sequence;
    if (event.session_id !== lastLogSession) {
      lastLogSession = event.session_id;
      uart.reset();
      scheduleRender(renderUart);
    }
    if (event.type === "uart") {
      uart.receive(event.endpoint, fromBase64(event.data_b64));
      scheduleRender(renderUart);
    }
    else if (event.type === "can") addCan({...event, direction: "RX", data: hexBytes(event.data_hex, 64)});
    else if (event.type === "reset") eventLog("仿真输入已重新初始化（RESET）");
    else if (event.type === "gap") eventLog(`历史缺口：${event.count} 条 / ${event.bytes} B`, true);
    else eventLog(String(event.message || event.text || event.type));
  }
  function activeChannels(kind) {
    const active = ["paused", "running"].includes(state.status);
    return (active ? state.config?.[kind] : null) || project[kind];
  }
  function updateControls() {
    const active = ["paused", "running"].includes(state.status);
    const transitioning = ["starting", "stopping"].includes(state.status);
    const lifecycle = Array.from(pending.values()).filter((item) => LIFECYCLE.has(item.action));
    const operation = lifecycle.find((item) => item.action === "stop") || lifecycle[0];
    const lifecyclePending = lifecycle.length > 0;
    const stopPending = operation?.action === "stop";
    const ready = connected && projectLoaded;
    const cancellable = active || transitioning || lifecyclePending || uploading?.startRequested;
    const operationLabel = ({start: "启动中…", load: "加载中…", restart: "重载中…", stop: "停止中…", run: "继续中…", pause: "暂停中…"})[operation?.action]
      || (state.status === "starting" ? "启动中…" : state.status === "stopping" ? "停止中…" : "");
    const progressLabel = uploading ? "上传中…" : saving ? "保存中…" : operationLabel || (!connected ? "连接中…" : "");
    $("session-progress").hidden = !progressLabel;
    $("session-progress").setAttribute("aria-label", progressLabel);
    setText("primary-button", uploading ? (uploading.startRequested ? "加载中…" : "加载后启动")
      : operationLabel || (state.status === "running" ? "暂停" : state.status === "paused" ? "继续" : "启动"));
    $("primary-button").disabled = !ready || !!uploading?.startRequested || transitioning || lifecyclePending
      || (!active && !uploading && !project.firmware && !state.firmware);
    $("restart-button").disabled = !ready || !!uploading || transitioning || lifecyclePending || (!project.firmware && !state.firmware);
    $("stop-button").disabled = !connected || !cancellable || stopPending;
    setText("stop-button", stopPending ? "停止中…" : "停止");
    for (const id of ["key-button", "vin-send", "gpio-send"]) $(id).disabled = !connected || !active || transitioning;
    for (const kind of ["uart", "can"]) {
      const enabled = activeChannels(kind === "uart" ? "uarts" : "cans");
      const selector = $(`${kind}-endpoint`);
      for (const option of selector.children) option.disabled = !enabled.includes(option.value);
      selector.disabled = !enabled.length;
      if (enabled.length && !enabled.includes(selector.value)) {
        selector.value = enabled[0]; scheduleRender();
      }
      $(`${kind}-send`).disabled = !connected || !active || !enabled.includes(selector.value);
    }
    $("adc-send").disabled = !connected || state.status !== "running" || !(state.config?.adc ?? project.adc);
    $("adc-send").title = state.status === "paused" ? "暂停时无法施加 ADC 输入" : "";
    const usb = state.usb || {state: "disabled"};
    $("usb-attach").disabled = !connected || !active || usb.state !== "available" || usb.importer_busy;
    $("usb-detach").disabled = !connected || !active || usb.local_port == null || usb.importer_busy;
    $("upload-button").disabled = !ready || !!uploading || active || transitioning || lifecyclePending;
    setText("upload-button", uploading ? "上传中…" : "打开 ELF");
    $("firmware-input").disabled = $("upload-button").disabled;
    $("upload-button").title = active ? "停止后更换固件" : "打开 ELF";
    $("restart-button").hidden = !active && !transitioning;
    $("stop-button").hidden = !cancellable;
    $("settings-button").disabled = !ready || !!uploading;
    $("settings-apply").disabled = saving || !projectLoaded;
    setText("settings-apply", saving ? "保存中…" : projectPath ? "保存" : "完成");
    $("settings-cancel").disabled = saving;
    $("settings-dialog").closeOnOverlayClick = !saving;
    $("settings-dialog").closeOnEsc = !saving;
    updateProjectName();
  }
  function updateProjectName() {
    const path = project.firmware;
    $("firmware-name").textContent = path ? path.split(/[\\/]/).pop() : state.firmware?.name || "未选择 ELF";
    $("firmware-name").title = path || state.firmware?.name || "";
  }
  function selectPeripheral(name) {
    for (const item of ["gpio", "adc", "vin", "usb"]) $(`peripheral-${item}`).hidden = item !== name;
    showError("board", "");
  }
  function selectPin(pin) {
    $("gpio-pin").value = pin;
    const observed = (state.pins || []).find((item) => item.pin === pin);
    $("pin-summary").textContent = observed ? `${pin} · ${modes[observed.mode]} · 采样 ${observed.level} · ODR ${observed.odr}` : "—";
  }
  function selectConsole(kind, endpoint) {
    $("channel-tabs").value = kind;
    for (const item of ["uart", "can"]) $(`console-${item}`).hidden = item !== kind;
    if (endpoint) $(`${kind}-endpoint`).value = endpoint;
    renderConsole();
    updateControls();
  }
  function applyState(value) {
    const changedOwner = value.owner_id !== state.owner_id;
    const changedSession = value.session_id !== state.session_id;
    if (changedOwner) {
      uart.reset(); canRecords.length = 0; events.length = 0;
      lastSeq = 0n; lastLogSession = undefined;
      scheduleRender();
    }
    state = value;
    if (changedOwner || changedSession) keyHeld = false;
    $("session-status").textContent = names[value.status] || value.status;
    $("session-status").dataset.status = value.status;
    const board = value.board;
    $("virtual-time").textContent = board ? ns(board.timestamp_ns) : "—";
    $("virtual-time").title = typeof value.remaining_seconds === "number" ? `剩余 ${Math.max(0, Math.ceil(value.remaining_seconds))} s` : "";
    $("observation-status").textContent = board && value.observation_stale ? "快照已过期" : "";
    if ($("events-dialog").open) $("raw-state").textContent = JSON.stringify(board, null, 2);
    $("vin-value").textContent = board ? `${board.vin_mv} mV` : "—";
    $("key-button").title = board?.key.pressed ? "USER 已按下" : "按住 USER";
    $("key-button").setAttribute("aria-pressed", String(Boolean(board?.key.pressed)));
    const color = board ? `#${board.led.rgb.toString(16).padStart(6, "0").toUpperCase()}` : null;
    $("led-value").textContent = color || "—";
    $("board-led").style.backgroundColor = color || "transparent";
    $("board-led").setAttribute("aria-label", color ? `LED ${color}` : "LED 无数据");
    $("led-observation").title = board ? `${board.led.source === "ws2812-dma" ? "WS2812 DMA 波形" : "GPIO latch 近似"} · RGB ${(board.led.rgb >>> 16) & 255}, ${(board.led.rgb >>> 8) & 255}, ${board.led.rgb & 255} · 亮度 ${board.led.brightness}` : "LED 无数据";
    const usb = value.usb || {state: "disabled"};
    $("usb-status").textContent = "USB · " + (({disabled: "未启用", disconnected: "未连接", enumerating: "枚举中", available: "可连接", attached: "已连接"})[usb.state] || usb.state);
    $("usb-status").hidden = usb.state === "disabled";
    $("usb-message").textContent = usb.message || (usb.state === "disabled" ? "在设置中启用 USB/IP 后重新启动。" : $("usb-status").textContent);
    $("gdb-address").textContent = value.gdb_port ? `GDB 127.0.0.1:${value.gdb_port}` : "";
    selectPin($("gpio-pin").value);
    if (value.error || value.reload_error) {
      if (value.error) sessionCommandError = null;
      const error = [value.error || value.reload_error, ...(value.notes || [])].join("\n");
      showError("session", error);
      if (error !== lastStateError) eventLog(error, true);
      lastStateError = error;
    } else { showError("session", sessionCommandError); lastStateError = null; }
    updateControls();
  }
  function send(action, args = {}, area = "board", accepted = null) {
    // A custom element may dispatch another click before its disabled button
    // renders. Stop remains available to cancel an in-flight lifecycle action.
    if (LIFECYCLE.has(action) && Array.from(pending.values()).some((item) =>
      LIFECYCLE.has(item.action) && (action !== "stop" || item.action === "stop"))) return false;
    if (area === "session") sessionCommandError = null;
    showError(area, "");
    if (!connected || !socket || socket.readyState !== WebSocket.OPEN) { fail(area, "连接已断开。"); return false; }
    const id = `${requestPrefix}-${++commandNumber}`;
    if (area === "session") sessionOperation = id;
    pending.set(id, {action, args, area, accepted});
    try { socket.send(JSON.stringify({id, session_id: state.session_id ?? null, action, args})); }
    catch (error) { pending.delete(id); fail(area, error); return false; }
    updateControls();
    return true;
  }
  function commandResult(event) {
    const request = pending.get(event.id);
    if (!request) return;
    pending.delete(event.id);
    if (!event.ok) {
      if (request.area !== "session" || event.id === sessionOperation) fail(request.area, String(event.error || "请求被拒绝。"));
    } else if (request.accepted) request.accepted(event);
    updateControls();
  }
  function connect() {
    clearTimeout(reconnectTimer);
    socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/ws`);
    socket.addEventListener("open", () => { connected = true; $("connection-status").textContent = ""; updateControls(); });
    socket.addEventListener("message", (message) => {
      try {
        const event = JSON.parse(message.data);
        if (event.type === "state") applyState(event);
        else if (event.type === "command_result") commandResult(event);
        else channelEvent(event);
      } catch (error) { fail("session", `服务端消息：${error.message}`); }
    });
    socket.addEventListener("close", () => {
      connected = false; keyHeld = false;
      if (uploading) uploading.startRequested = false;
      if (pending.size) eventLog(`${pending.size} 个请求未收到结果；不会重发。`, true);
      pending.clear(); $("connection-status").textContent = "重新连接…"; updateControls();
      reconnectTimer = setTimeout(connect, 1500);
    });
    socket.addEventListener("error", () => { $("connection-status").textContent = "连接不可用"; });
  }
  function pressKey(event) {
    if ($("key-button").disabled || keyHeld) return;
    event.preventDefault();
    keyHeld = send("key", {pressed: true});
    if (keyHeld && event.pointerId !== undefined) $("key-button").setPointerCapture(event.pointerId);
  }
  function releaseKey() {
    if (!keyHeld) return;
    keyHeld = false;
    send("key", {pressed: false});
  }
  function form(id, action, args, area = "board", accepted = null) {
    $(id).addEventListener("submit", (event) => {
      event.preventDefault();
      try { const value = args(); send(action, value, area, accepted ? (reply) => accepted(value, reply) : null); }
      catch (error) { fail(area, error); }
    });
  }
  function menuItem(value, text) {
    const item = document.createElement("mdui-menu-item"); item.setAttribute("value", value); item.textContent = text; return item;
  }
  function renderSettings() {
    $("project-path").textContent = projectPath || "临时项目";
    $("project-firmware").value = project.firmware || "";
    $("project-watch").checked = project.watch_elf;
    $("project-startup").value = project.startup;
    $("project-user-hold").value = String(project.user_hold_ms);
    $("project-startup-timeout").value = String(project.startup_timeout_secs);
    $("project-timeout").value = String(project.timeout_secs);
    $("project-imu").value = project.imu;
    $("project-gdb").checked = project.gdb_port != null;
    $("project-gdb-port").value = String(project.gdb_port || 1234);
    $("project-usb").checked = project.usbip != null;
    $("project-usb-address").value = project.usbip || "127.0.0.1:3240";
    for (const kind of ["uarts", "cans"]) for (const endpoint of kind === "uarts" ? UARTS : CANS) $(`project-${endpoint}`).checked = project[kind].includes(endpoint);
    $("project-adc").checked = project.adc;
    updateSettingsControls();
  }
  function updateSettingsControls() {
    $("project-watch").disabled = !$("project-firmware").value.trim();
    if ($("project-watch").disabled) $("project-watch").checked = false;
    $("project-user-hold").hidden = $("project-startup").value !== "user";
    $("project-gdb-port").disabled = !$("project-gdb").checked;
    $("project-usb-address").disabled = !$("project-usb").checked;
  }
  function seconds(value) {
    const number = Number(String(value).trim());
    if (!Number.isFinite(number) || number <= 0) throw new Error("期限需要为正数。");
    return number;
  }
  function readSettings() {
    const next = {...project, firmware: $("project-firmware").value.trim() || null,
      watch_elf: $("project-watch").checked,
      startup: $("project-startup").value,
      user_hold_ms: integer($("project-user-hold").value, "USER", 4294967295),
      startup_timeout_secs: seconds($("project-startup-timeout").value),
      timeout_secs: seconds($("project-timeout").value),
      imu: $("project-imu").value,
      gdb_port: $("project-gdb").checked ? integer($("project-gdb-port").value, "GDB 端口", 65535) : null,
      usbip: $("project-usb").checked ? $("project-usb-address").value.trim() : null,
      uarts: UARTS.filter((port) => $(`project-${port}`).checked),
      cans: CANS.filter((port) => $(`project-${port}`).checked), adc: $("project-adc").checked};
    if (!next.user_hold_ms || !next.startup_timeout_secs || !next.timeout_secs) throw new Error("期限和 USER 时长需要大于 0。");
    if (next.gdb_port === 0) throw new Error("GDB 端口需要大于 0。");
    if (next.startup === "wait_gdb" && next.gdb_port == null) throw new Error("等待 GDB 需要启用 GDB。");
    if (next.usbip === "") throw new Error("USB/IP 地址不能为空。");
    return next;
  }
  async function applySettings() {
    showError("settings", "");
    try {
      let next = readSettings();
      if (projectPath) {
        saving = true; updateControls();
        const response = await fetch("/api/project", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(next)});
        const value = await response.json();
        if (!response.ok) throw new Error(value.error || `保存失败（HTTP ${response.status}）。`);
        next = value.project;
      }
      project = next; updateControls(); $("settings-dialog").open = false;
    } catch (error) { fail("settings", error); }
    finally { saving = false; updateControls(); }
  }
  async function exportIde() {
    try {
      showError("settings", "");
      const response = await fetch("/api/ide", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(readSettings())});
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || `导出失败（HTTP ${response.status}）。`);
      const format = $("ide-format").value;
      const config = format === "vscode" ? {name: "launch.json", mime: "application/json", text: JSON.stringify(value.vscode, null, 2) + "\n"}
        : format === "gdb" ? {name: "attach.gdb", mime: "text/plain", text: value.gdb}
        : {name: "clion-debug.txt", mime: "text/plain", text: `Remote Debug\nDebugger: ${value.clion.debugger}\nTarget: ${value.clion.target}\nSymbol file: ${value.clion.symbol_file}\n`};
      const url = URL.createObjectURL(new Blob([config.text], {type: config.mime}));
      const link = document.createElement("a"); link.href = url; link.download = config.name; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { fail("settings", error); }
  }
  function initialize() {
    for (const kind of ["uart", "can"]) for (const endpoint of kind === "uart" ? UARTS : CANS) {
      const name = portName(endpoint);
      $(`${kind}-endpoint`).append(menuItem(endpoint, name));
      const checkbox = document.createElement("mdui-checkbox"); checkbox.id = `project-${endpoint}`; checkbox.textContent = name;
      $("project-channels").append(checkbox);
    }
    const adc = document.createElement("mdui-checkbox"); adc.id = "project-adc"; adc.textContent = "ADC1"; $("project-channels").append(adc);
    for (const port of "ABCDEFGH") for (let bit = 0; bit < 16; bit += 1) {
      const pin = `${port}${bit}`; $("gpio-pin").append(menuItem(pin, pin));
    }
    for (let channel = 0; channel < 32; channel += 1) $("adc-channel").append(menuItem(String(channel), `CH${channel}`));
    // MDUI's initial select render precedes dynamically assigned menu options.
    for (const id of ["uart-endpoint", "can-endpoint", "gpio-pin", "adc-channel"]) {
      const selector = $(id);
      selector.updateComplete.then(() => selector.requestUpdate());
    }
    $("peripherals-button").addEventListener("click", () => { $("peripherals-dialog").open = true; });
    $("peripheral-tabs").addEventListener("change", () => selectPeripheral($("peripheral-tabs").value));
    $("peripheral-close").addEventListener("click", () => { $("peripherals-dialog").open = false; });
    $("events-button").addEventListener("click", () => { $("events-dialog").open = true; $("raw-state").textContent = JSON.stringify(state.board, null, 2); renderEvents(); });
    $("events-close").addEventListener("click", () => { $("events-dialog").open = false; });
    $("gpio-pin").addEventListener("change", () => selectPin($("gpio-pin").value));
    $("channel-tabs").addEventListener("change", () => selectConsole($("channel-tabs").value));
    for (const kind of ["uart", "can"]) {
      $(`${kind}-endpoint`).addEventListener("change", () => selectConsole(kind, $(`${kind}-endpoint`).value));
      $(`${kind}-scroll`).addEventListener("change", renderConsole);
    }
    // A format always has one selection; MDUI otherwise allows toggling it off.
    for (const id of ["uart-mode", "uart-receive-format"]) {
      const group = $(id);
      group.addEventListener("click", (event) => {
        const option = event.composedPath().find((element) => element.tagName === "MDUI-SEGMENTED-BUTTON");
        if (option?.value === group.value) event.stopPropagation();
      }, true);
    }
    $("uart-data").addEventListener("keydown", (event) => {
      if (event.ctrlKey && event.key === "Enter" && !$("uart-send").disabled) {
        event.preventDefault(); $("uart-form").requestSubmit();
      }
    });
    $("uart-receive-format").addEventListener("change", renderUart);
    $("uart-clear").addEventListener("click", () => { uart.clear($("uart-endpoint").value); renderUart(); });
    $("can-clear").addEventListener("click", () => { for (let index = canRecords.length - 1; index >= 0; index -= 1) if (canRecords[index].endpoint === $("can-endpoint").value) canRecords.splice(index, 1); renderCan(); });
    $("events-clear").addEventListener("click", () => { events.length = 0; renderEvents(); });
    $("primary-button").addEventListener("click", () => {
      if (uploading) {
        uploading.startRequested = true; sessionCommandError = null; showError("session", ""); updateControls(); return;
      }
      if (state.status === "running") send("pause", {}, "session");
      else if (state.status === "paused") send("run", {}, "session");
      else send("start", {config: structuredClone(project)}, "session");
    });
    $("restart-button").addEventListener("click", () => send("restart", {config: structuredClone(project)}, "session"));
    $("stop-button").addEventListener("click", () => {
      if (uploading?.startRequested) { uploading.startRequested = false; updateControls(); return; }
      send("stop", {}, "session");
    });
    $("usb-attach").addEventListener("click", () => send("usb_attach")); $("usb-detach").addEventListener("click", () => send("usb_detach"));
    $("key-button").addEventListener("pointerdown", pressKey);
    for (const event of ["pointerup", "pointercancel", "lostpointercapture", "blur"]) $("key-button").addEventListener(event, releaseKey);
    $("key-button").addEventListener("keydown", (event) => { if (["Space", "Enter"].includes(event.code)) pressKey(event); });
    $("key-button").addEventListener("keyup", (event) => { if (["Space", "Enter"].includes(event.code)) { event.preventDefault(); releaseKey(); } });
    window.addEventListener("blur", releaseKey); window.addEventListener("pagehide", releaseKey);
    document.addEventListener("visibilitychange", () => { if (document.hidden) releaseKey(); });
    form("vin-form", "vin", () => ({millivolts: integer($("vin-input").value, "VIN", 4294967295)}));
    form("gpio-form", "gpio", () => ({pin: $("gpio-pin").value, level: Number($("gpio-level").value)}));
    form("uart-form", "uart", () => {
      const payload = $("uart-mode").value === "hex" ? hexBytes($("uart-data").value, 4096) : encoder.encode($("uart-data").value);
      const suffix = encoder.encode(({none: "", lf: "\n", crlf: "\r\n", cr: "\r"})[$("uart-newline").value]);
      if (payload.length + suffix.length > 4096) throw new Error("UART 最多 4096 字节，含换行。");
      const bytes = new Uint8Array(payload.length + suffix.length); bytes.set(payload); bytes.set(suffix, payload.length);
      return {endpoint: $("uart-endpoint").value, data_b64: toBase64(bytes)};
    }, "uart", (args) => {
      const bytes = fromBase64(args.data_b64); uart.accepted(args.endpoint, bytes.length);
      eventLog(`${args.endpoint.toUpperCase()} TX ${bytes.length} B · 已接受`); renderUart();
    });
    form("can-form", "can", () => ({endpoint: $("can-endpoint").value,
      can_id: integer($("can-id").value, "CAN ID", 536870911),
      flags: ["ext", "rtr", "fd", "brs"].reduce((flags, name, index) => flags | ($(`can-${name}`).checked ? 1 << index : 0), 0),
      dlc: integer($("can-dlc").value, "DLC", 15), data_hex: toHex(hexBytes($("can-data").value, 64)).replaceAll(" ", "")}),
    "can", (args, reply) => addCan({...args, direction: "TX", timestamp_ns: reply.timestamp_ns ?? null, data: hexBytes(args.data_hex, 64)}));
    form("adc-form", "adc", () => { const mode = $("adc-mode").value; return {channel: Number($("adc-channel").value), [mode]: integer($("adc-value").value, mode, mode === "raw" ? 65535 : 3300000)}; });
    $("adc-mode").addEventListener("change", () => { $("adc-value").max = $("adc-mode").value === "raw" ? "65535" : "3300000"; });
    $("settings-button").addEventListener("click", () => { renderSettings(); showError("settings", ""); $("settings-dialog").open = true; });
    $("settings-cancel").addEventListener("click", () => { $("settings-dialog").open = false; });
    $("settings-apply").addEventListener("click", applySettings);
    $("project-firmware").addEventListener("input", updateSettingsControls);
    $("project-startup").addEventListener("change", () => {
      if ($("project-startup").value === "wait_gdb") $("project-gdb").checked = true;
      updateSettingsControls();
    });
    for (const id of ["project-gdb", "project-usb"]) $(id).addEventListener("change", updateSettingsControls);
    $("ide-download").addEventListener("click", exportIde);
    $("upload-button").addEventListener("click", () => { if (!$("upload-button").disabled) $("firmware-input").click(); });
    $("firmware-input").addEventListener("change", async () => {
      const file = $("firmware-input").files[0]; if (!file) return;
      if (file.size > 64 * 1024 * 1024) { fail("session", "ELF 不能超过 64 MiB。"); $("firmware-input").value = ""; return; }
      const upload = {startRequested: false};
      uploading = upload; updateControls();
      try {
        const body = new FormData(); body.append("firmware", file);
        const response = await fetch("/api/firmware", {method: "POST", body}); const value = await response.json();
        if (!response.ok) throw new Error(value.error || `上传失败（HTTP ${response.status}）。`);
        project.firmware = null; project.watch_elf = false; $("project-firmware").value = ""; updateSettingsControls(); applyState(value); showError("session", "");
      } catch (error) { upload.startRequested = false; fail("session", error); }
      finally { uploading = null; $("firmware-input").value = ""; updateControls(); }
      if (upload.startRequested) send("start", {config: structuredClone(project)}, "session");
    });
    renderSettings(); selectPin("B0"); selectConsole("uart", "uart1"); updateControls();
  }
  initialize();
  Promise.allSettled([
    fetch("/api/project").then(async (response) => { const value = await response.json(); if (!response.ok) throw new Error(value.error || "无法读取项目。"); project = {...project, ...value.project}; projectPath = value.path; projectLoaded = true; renderSettings(); }),
    fetch("/api/state").then((response) => { if (!response.ok) throw new Error(`读取状态失败（HTTP ${response.status}）。`); return response.json(); }).then(applyState)
  ]).then((results) => { for (const result of results) if (result.status === "rejected") fail("session", result.reason); }).finally(connect);
})();
