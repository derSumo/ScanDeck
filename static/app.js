/* ==========================================================================
   Scan Deck — client logic
   ========================================================================== */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const LABELS = {
  source: { Platen: "Flachbett", Feeder: "Einzug" },
  color_mode: { RGB24: "Farbe", Grayscale8: "Grau", BlackAndWhite1: "S/W" },
  output_format: { "application/pdf": "PDF", "image/jpeg": "JPEG" },
};

const QUICK_CYCLE = {
  source: ["Platen", "Feeder"],
  output_format: ["application/pdf", "image/jpeg"],
  resolution: [150, 200, 300, 600],
  color_mode: ["RGB24", "Grayscale8", "BlackAndWhite1"],
};

const STAGE_TEXT = {
  start: "Scan wird vorbereitet …",
  connect: "Verbinde mit dem Scanner …",
  job: "Scanauftrag wird übermittelt …",
  capture: "Dokument wird erfasst …",
  store: "Scan wird gespeichert …",
  upload: "Übertrage nach Paperless-ngx …",
  done: "Fertig",
  error: "Fehlgeschlagen",
};

const STAGE_ORDER = ["connect", "capture", "store", "upload"];

const state = {
  config: {},
  defaultTags: [],
  sessionTags: [],
  wizardStep: 0,
  wizardTags: [],
  running: false,
  scanStart: 0,
  previewTimer: null,
  elapsedTimer: null,
};

/* --- helpers ------------------------------------------------------------- */

function toast(message, kind = "") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (element.className = "toast"), 3600);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function setValue(id, value) {
  const element = document.getElementById(id);
  if (!element) return;
  if (element.type === "checkbox") element.checked = Boolean(value);
  else element.value = value ?? "";
}

function getValue(id) {
  const element = document.getElementById(id);
  if (!element) return undefined;
  return element.type === "checkbox" ? element.checked : element.value;
}

function setSegment(target, value) {
  $$(`.seg[data-target="${target}"] button`).forEach((button) =>
    button.classList.toggle("active", String(button.dataset.value) === String(value))
  );
}

function getSegment(target, fallback) {
  const active = $(`.seg[data-target="${target}"] button.active`);
  return active ? active.dataset.value : fallback;
}

function renderChips(container, tags, onRemove) {
  container.replaceChildren();
  tags.forEach((tag) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.append(document.createTextNode(tag));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `${tag} entfernen`);
    remove.addEventListener("click", () => onRemove(tag));
    chip.append(remove);
    container.append(chip);
  });
}

function addTag(inputId, collection, rerender) {
  const input = document.getElementById(inputId);
  const tag = input.value.trim();
  if (!tag) return;
  if (!collection.some((item) => item.localeCompare(tag, undefined, { sensitivity: "accent" }) === 0)) {
    collection.push(tag);
  }
  input.value = "";
  rerender();
}

function bindEnter(inputId, handler) {
  document.getElementById(inputId).addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      handler();
    }
  });
}

async function copyText(value, message = "Kopiert") {
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast(message, "success");
  } catch {
    // Clipboard API needs a secure context; fall back to a manual selection.
    const helper = document.createElement("textarea");
    helper.value = value;
    document.body.append(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
    toast(message, "success");
  }
}

/* --- rendering ----------------------------------------------------------- */

function renderTags() {
  renderChips($("#session-tag-list"), state.sessionTags, (tag) => {
    state.sessionTags = state.sessionTags.filter((item) => item !== tag);
    renderTags();
  });
  renderChips($("#default-tag-list"), state.defaultTags, (tag) => {
    state.defaultTags = state.defaultTags.filter((item) => item !== tag);
    renderTags();
  });
  $("#default-tags-hint").textContent = state.defaultTags.length
    ? `Immer mitgesendet: ${state.defaultTags.join(", ")}`
    : "Noch keine Standard-Tags hinterlegt.";
}

function renderQuickRow() {
  const config = state.config;
  $("#quick-source").textContent = LABELS.source[config.source] || "—";
  $("#quick-format").textContent = LABELS.output_format[config.output_format] || "—";
  $("#quick-resolution").textContent = config.resolution ?? "—";
  $("#quick-color").textContent = LABELS.color_mode[config.color_mode] || "—";
  $("#orb-sub").textContent = [
    LABELS.source[config.source] || "—",
    LABELS.output_format[config.output_format] || "—",
    `${config.resolution || 300} dpi`,
  ].join(" · ");
}

function renderTiles() {
  const config = state.config;
  const scannerHost = config.scanner_url ? config.scanner_url.replace(/^https?:\/\//, "") : "—";
  const scanner = $("#tile-scanner");
  scanner.className = `tile ${config.scanner_url ? "ok" : "off"}`;
  $("#tile-scanner-value").textContent = config.scanner_url ? "Verbunden" : "Fehlt";
  $("#tile-scanner-sub").textContent = config.scanner_url ? scannerHost : "nicht konfiguriert";

  const paperless = $("#tile-paperless");
  const paperlessReady = config.upload_to_paperless && config.paperless_url && config.paperless_token_configured;
  paperless.className = `tile ${paperlessReady ? "ok" : config.upload_to_paperless ? "warn" : "off"}`;
  $("#tile-paperless-value").textContent = paperlessReady ? "Aktiv" : config.upload_to_paperless ? "Unvollständig" : "Aus";
  $("#tile-paperless-sub").textContent = config.paperless_url
    ? config.paperless_url.replace(/^https?:\/\//, "")
    : "nur lokale Ablage";

  const ha = $("#tile-ha");
  ha.className = `tile ${config.ha_enabled && config.ha_api_key_configured ? "ok" : "off"}`;
  $("#tile-ha-value").textContent = config.ha_enabled ? "Bereit" : "Aus";
  $("#tile-ha-sub").textContent = config.ha_enabled
    ? config.ha_webhook_url
      ? "Trigger + Webhook"
      : "Trigger aktiv"
    : "keine Automatisierung";
}

function renderLastScan(scanState) {
  const tile = $("#tile-last");
  if (!scanState.last_name) {
    tile.className = "tile off";
    $("#tile-last-value").textContent = "—";
    $("#tile-last-sub").textContent = "noch keiner";
    return;
  }
  tile.className = `tile ${scanState.last_error ? "warn" : "ok"}`;
  $("#tile-last-value").textContent = (scanState.last_kind || "").toUpperCase() === "PDF" ? "PDF" : "Bild";
  $("#tile-last-sub").textContent = scanState.last_name;
}

function setStatus(text, kind) {
  $("#status-pill").className = `status-pill ${kind}`;
  $("#status-text").textContent = text;
}

function renderHaYaml() {
  const origin = window.location.origin;
  const key = getValue("ha-api-key") || "<API-KEY>";
  $("#ha-yaml").textContent = `# configuration.yaml — Scan per Automatisierung auslösen
rest_command:
  scan_deck_scan:
    url: "${origin}/api/ha/scan"
    method: POST
    headers:
      X-API-Key: "${key}"
      Content-Type: "application/json"
    payload: '{"tags": ["Automatisiert"]}'

# Statussensor (optional)
sensor:
  - platform: rest
    name: Scan Deck
    resource: "${origin}/api/ha/state"
    headers:
      X-API-Key: "${key}"
    value_template: "{{ value_json.state }}"
    json_attributes:
      - progress
      - stage
      - last_file
      - last_error
    scan_interval: 15

# Beispiel: Bewegungsmelder löst einen Scan aus
automation:
  - alias: "Scan bei Bewegung am Schreibtisch"
    trigger:
      - platform: state
        entity_id: binary_sensor.schreibtisch_bewegung
        to: "on"
    condition:
      - condition: state
        entity_id: sensor.scan_deck
        state: "idle"
    action:
      - service: rest_command.scan_deck_scan`;
}

/* --- config load / save -------------------------------------------------- */

function applyConfig(config) {
  state.config = config;
  state.defaultTags = config.default_tags || [];

  setValue("scanner-url", config.scanner_url);
  setValue("verify-scanner-ssl", config.verify_scanner_ssl);
  setValue("discovery-subnet", config.discovery_subnet || config.suggested_subnet || "");
  setValue("paperless-url", config.paperless_url);
  setValue("paperless-token", "");
  setValue("upload-to-paperless", config.upload_to_paperless);
  setValue("title-prefix", config.title_prefix);
  setValue("output-dir", config.output_dir);
  setValue("preview-seconds", config.preview_seconds ?? 10);
  setValue("create-missing-tags", config.create_missing_tags);
  setValue("ha-enabled", config.ha_enabled);
  setValue("ha-webhook-url", config.ha_webhook_url);

  if (config.version) $("#app-version").textContent = `v${config.version}`;

  setSegment("output-format", config.output_format);
  setSegment("source", config.source);
  setSegment("resolution", config.resolution);
  setSegment("color-mode", config.color_mode);

  renderTags();
  renderQuickRow();
  renderTiles();
  renderHaYaml();
}

function collectConfig() {
  return {
    scanner_url: getValue("scanner-url"),
    verify_scanner_ssl: getValue("verify-scanner-ssl"),
    discovery_subnet: getValue("discovery-subnet"),
    paperless_url: getValue("paperless-url"),
    paperless_token: (getValue("paperless-token") || "").trim(),
    upload_to_paperless: getValue("upload-to-paperless"),
    title_prefix: getValue("title-prefix"),
    output_dir: getValue("output-dir"),
    preview_seconds: Number(getValue("preview-seconds") || 0),
    default_tags: state.defaultTags,
    create_missing_tags: getValue("create-missing-tags"),
    source: getSegment("source", state.config.source),
    resolution: Number(getSegment("resolution", state.config.resolution)),
    color_mode: getSegment("color-mode", state.config.color_mode),
    output_format: getSegment("output-format", state.config.output_format),
    ha_enabled: getValue("ha-enabled"),
    ha_webhook_url: getValue("ha-webhook-url"),
  };
}

async function saveSettings(announce = true) {
  const config = await api("/api/config", { method: "PUT", body: JSON.stringify(collectConfig()) });
  setValue("paperless-token", "");
  applyConfig(config);
  if (announce) toast("Gespeichert", "success");
  return config;
}

async function loadConfig() {
  const config = await api("/api/config");
  applyConfig(config);
  if (config.ha_enabled) {
    const { api_key: key } = await api("/api/ha/key").catch(() => ({ api_key: "" }));
    setValue("ha-api-key", key);
    renderHaYaml();
  }
  if (!config.setup_complete) openWizard(config);
  return config;
}

/* --- navigation ---------------------------------------------------------- */

function activateView(name) {
  document.body.dataset.view = name;
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* --- scanning ------------------------------------------------------------ */

function setProgress(percent, stage) {
  const value = Math.max(0, Math.min(100, percent));
  $("#progress-bar").style.width = `${value}%`;
  $("#progress-percent").textContent = `${Math.round(value)} %`;
  $("#orb-progress").style.strokeDashoffset = String(578 - (578 * value) / 100);

  if (stage) {
    $("#progress-stage").textContent = STAGE_TEXT[stage] || "Arbeite …";
    const activeIndex = STAGE_ORDER.indexOf(stage === "job" ? "connect" : stage);
    $$("#progress-steps li").forEach((item, index) => {
      item.classList.toggle("active", index === activeIndex);
      item.classList.toggle("done", activeIndex > index || stage === "done");
    });
  }
}

function showProgress() {
  const overlay = $("#progress-overlay");
  overlay.hidden = false;
  overlay.classList.remove("closing");
  $("#progress-title").textContent = "Scan läuft";
  state.scanStart = Date.now();
  clearInterval(state.elapsedTimer);
  state.elapsedTimer = setInterval(() => {
    $("#progress-elapsed").textContent = `${Math.round((Date.now() - state.scanStart) / 1000)} s`;
  }, 500);
}

function hideOverlay(overlay) {
  if (overlay.hidden) return;
  overlay.classList.add("closing");
  setTimeout(() => {
    overlay.hidden = true;
    overlay.classList.remove("closing");
  }, 280);
}

function hideProgress() {
  clearInterval(state.elapsedTimer);
  hideOverlay($("#progress-overlay"));
}

function setScanning(running) {
  if (state.running === running) return;
  state.running = running;
  document.body.classList.toggle("is-scanning", running);
  $("#scan-button").disabled = running;
  $("#orb-label").textContent = running ? "Scannt …" : "Scan starten";
  if (running) {
    showProgress();
    setStatus("Scannt", "scanning");
  } else {
    setStatus("Bereit", "ready");
    setTimeout(() => setProgress(0), 900);
  }
}

async function startScan() {
  if (!state.config.scanner_url) {
    toast("Zuerst einen Scanner einrichten", "error");
    activateView("settings");
    return;
  }
  try {
    setScanning(true);
    setProgress(2, "start");
    await api("/api/scan", { method: "POST", body: JSON.stringify({ session_tags: state.sessionTags }) });
  } catch (error) {
    setScanning(false);
    hideProgress();
    toast(error.message, "error");
  }
}

/* --- preview ------------------------------------------------------------- */

function showPreview(filename) {
  const seconds = Number(state.config.preview_seconds ?? 10);
  if (!seconds) return;

  const overlay = $("#preview-overlay");
  const image = $("#preview-image");
  $("#preview-name").textContent = filename || "Scan";
  $("#preview-meta").textContent = state.config.upload_to_paperless
    ? "Gespeichert und an Paperless übergeben"
    : "Lokal gespeichert";
  image.src = `/api/preview?ts=${Date.now()}`;
  image.onerror = () => {
    image.alt = "Vorschau konnte nicht geladen werden";
  };
  overlay.hidden = false;
  overlay.classList.remove("closing");

  const ring = $("#countdown-ring");
  const countdownBox = ring.closest(".countdown");
  countdownBox.classList.remove("paused");
  let remaining = seconds;
  $("#countdown-value").textContent = String(remaining);
  ring.style.strokeDashoffset = "0";

  clearInterval(state.previewTimer);
  state.previewTimer = setInterval(() => {
    remaining -= 1;
    $("#countdown-value").textContent = String(Math.max(0, remaining));
    ring.style.strokeDashoffset = String(100.5 * (1 - remaining / seconds));
    if (remaining <= 0) closePreview();
  }, 1000);
}

function closePreview() {
  clearInterval(state.previewTimer);
  hideOverlay($("#preview-overlay"));
}

function keepPreview() {
  clearInterval(state.previewTimer);
  $("#countdown-ring").closest(".countdown").classList.add("paused");
  $("#countdown-value").textContent = "∞";
  toast("Vorschau bleibt offen");
}

/* --- live events --------------------------------------------------------- */

function appendLog(event) {
  const log = $("#log-lines");
  log.querySelector(".log-empty")?.remove();
  const row = document.createElement("p");
  row.className = event.level || "info";
  row.textContent = `${event.time}  ${event.message}`;
  log.append(row);
  while (log.children.length > 120) log.firstElementChild.remove();
  log.scrollTop = log.scrollHeight;
}

function handleProgress(event) {
  if (event.stage !== "done" && event.stage !== "error") setScanning(true);
  setProgress(event.progress, event.stage);

  if (event.stage === "done") {
    $("#progress-title").textContent = "Fertig";
    setTimeout(() => {
      hideProgress();
      setScanning(false);
      showPreview(event.file);
      refreshState().catch(() => {});
    }, 620);
  }
  if (event.stage === "error") {
    $("#progress-title").textContent = "Fehlgeschlagen";
    $("#progress-stage").textContent = event.error || "Unbekannter Fehler";
    setTimeout(() => {
      hideProgress();
      setScanning(false);
      setStatus("Fehler", "error");
      toast(event.error || "Scan fehlgeschlagen", "error");
    }, 900);
  }
}

function connectEvents() {
  const stream = new EventSource("/api/logs");
  stream.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.kind === "progress") handleProgress(event);
    else appendLog(event);
  };
  stream.onerror = () => setStatus("Offline", "offline");
  stream.onopen = () => {
    if (!state.running) setStatus("Bereit", "ready");
  };
}

async function refreshState() {
  const scanState = await api("/api/state");
  renderLastScan(scanState);
  if (scanState.running) {
    setScanning(true);
    setProgress(scanState.progress, scanState.stage);
  } else if (state.running) {
    // The stream may have been missed (e.g. app resumed from background).
    setScanning(false);
    hideProgress();
  }
  return scanState;
}

/* --- discovery ----------------------------------------------------------- */

async function runDiscovery(subnetId, containerId, targetId) {
  const container = document.getElementById(containerId);
  container.replaceChildren(Object.assign(document.createElement("p"), { textContent: "Suche läuft …" }));
  try {
    const found = await api("/api/discover/scanners", {
      method: "POST",
      body: JSON.stringify({ discovery_subnet: getValue(subnetId) }),
    });
    container.replaceChildren();
    if (!found.devices.length) {
      container.append(Object.assign(document.createElement("p"), { textContent: "Kein eSCL-Scanner gefunden." }));
      return;
    }
    found.devices.forEach((device) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "device";
      item.append(
        Object.assign(document.createElement("b"), { textContent: device.model }),
        Object.assign(document.createElement("span"), { textContent: `${device.url} · eSCL ${device.version}` })
      );
      item.addEventListener("click", () => {
        setValue(targetId, device.url);
        toast(`${device.model} übernommen`, "success");
      });
      container.append(item);
    });
  } catch (error) {
    container.replaceChildren(Object.assign(document.createElement("p"), { textContent: error.message }));
    toast(error.message, "error");
  }
}

/* --- wizard -------------------------------------------------------------- */

const WIZARD_STEPS = 5;

function openWizard(config) {
  const wizard = $("#wizard");
  wizard.hidden = false;
  state.wizardStep = 0;
  state.wizardTags = [...(config.default_tags || [])];
  setValue("wiz-subnet", config.discovery_subnet || config.suggested_subnet || "");
  setValue("wiz-scanner-url", config.scanner_url);
  setValue("wiz-verify-ssl", config.verify_scanner_ssl);
  setValue("wiz-upload", config.upload_to_paperless);
  setValue("wiz-paperless-url", config.paperless_url);
  setValue("wiz-output-dir", config.output_dir || "/scans");
  setValue("wiz-title-prefix", config.title_prefix || "Scan");
  setValue("wiz-ha", config.ha_enabled);
  setSegment("wiz-format", config.output_format || "application/pdf");
  setSegment("wiz-resolution", config.resolution || 300);
  setSegment("wiz-color", config.color_mode || "RGB24");
  renderWizardTags();
  renderWizardStep();
}

function renderWizardTags() {
  renderChips($("#wiz-tag-list"), state.wizardTags, (tag) => {
    state.wizardTags = state.wizardTags.filter((item) => item !== tag);
    renderWizardTags();
  });
}

function renderWizardStep(direction = 1) {
  const dots = $("#wizard-dots");
  dots.replaceChildren(
    ...Array.from({ length: WIZARD_STEPS }, (_, index) => {
      const dot = document.createElement("i");
      if (index === state.wizardStep) dot.className = "active";
      else if (index < state.wizardStep) dot.className = "done";
      return dot;
    })
  );

  $$(".wizard-step").forEach((step) => {
    const active = Number(step.dataset.step) === state.wizardStep;
    step.classList.toggle("back", direction < 0);
    step.classList.toggle("active", active);
  });

  $("#wiz-back").classList.toggle("hidden", state.wizardStep === 0);
  $("#wiz-next").textContent =
    state.wizardStep === 0 ? "Los geht's" : state.wizardStep === WIZARD_STEPS - 1 ? "Einrichtung abschließen" : "Weiter";
  $("#wizard-track").scrollTo({ top: 0, behavior: "smooth" });
  syncWizardConditionals();
}

function syncWizardConditionals() {
  $("#wiz-paperless-fields").style.display = getValue("wiz-upload") ? "" : "none";
  $("#wiz-ha-fields").style.display = getValue("wiz-ha") ? "" : "none";
}

function wizardPayload() {
  return {
    scanner_url: getValue("wiz-scanner-url"),
    verify_scanner_ssl: getValue("wiz-verify-ssl"),
    discovery_subnet: getValue("wiz-subnet"),
    upload_to_paperless: getValue("wiz-upload"),
    paperless_url: getValue("wiz-paperless-url"),
    paperless_token: (getValue("wiz-paperless-token") || "").trim(),
    output_dir: getValue("wiz-output-dir") || "/scans",
    title_prefix: getValue("wiz-title-prefix") || "Scan",
    default_tags: state.wizardTags,
    output_format: getSegment("wiz-format", "application/pdf"),
    resolution: Number(getSegment("wiz-resolution", 300)),
    color_mode: getSegment("wiz-color", "RGB24"),
    ha_enabled: getValue("wiz-ha"),
  };
}

async function wizardNext() {
  const button = $("#wiz-next");
  if (state.wizardStep === 1 && !getValue("wiz-scanner-url").trim()) {
    toast("Bitte einen Scanner auswählen oder eintragen", "error");
    return;
  }
  if (state.wizardStep === 2 && getValue("wiz-upload")) {
    if (!getValue("wiz-paperless-url").trim()) {
      toast("Paperless-URL fehlt", "error");
      return;
    }
  }

  if (state.wizardStep < WIZARD_STEPS - 1) {
    button.disabled = true;
    try {
      // Persist progress after every step so a reload never loses the input.
      if (state.wizardStep >= 1) await api("/api/config", { method: "PUT", body: JSON.stringify(wizardPayload()) });
      state.wizardStep += 1;
      renderWizardStep(1);
      if (state.wizardStep === 4 && getValue("wiz-ha") && !getValue("wiz-ha-key")) await ensureHaKey("wiz-ha-key");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
    return;
  }

  button.disabled = true;
  try {
    const config = await api("/api/setup/complete", { method: "POST", body: JSON.stringify(wizardPayload()) });
    applyConfig(config);
    if (config.ha_enabled) {
      const { api_key: key } = await api("/api/ha/key").catch(() => ({ api_key: "" }));
      setValue("ha-api-key", key);
      renderHaYaml();
    }
    const wizard = $("#wizard");
    wizard.classList.add("closing");
    setTimeout(() => {
      wizard.hidden = true;
      wizard.classList.remove("closing");
      toast("Alles eingerichtet — bereit zum Scannen", "success");
    }, 340);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function ensureHaKey(targetId) {
  const existing = await api("/api/ha/key").catch(() => ({ api_key: "" }));
  const key = existing.api_key || (await api("/api/ha/key", { method: "POST", body: "{}" })).api_key;
  setValue(targetId, key);
  setValue("ha-api-key", key);
  renderHaYaml();
  return key;
}

/* --- wiring -------------------------------------------------------------- */

$$(".tab").forEach((tab) => tab.addEventListener("click", () => activateView(tab.dataset.view)));

$$(".seg").forEach((group) =>
  group.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    group.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    if (["output-format", "source", "resolution", "color-mode"].includes(group.dataset.target)) {
      state.config[group.dataset.target.replace("-", "_")] =
        group.dataset.target === "resolution" ? Number(button.dataset.value) : button.dataset.value;
      renderQuickRow();
    }
  })
);

$$(".quick").forEach((button) =>
  button.addEventListener("click", async () => {
    const key = button.dataset.quick;
    const options = QUICK_CYCLE[key];
    const current = options.findIndex((option) => String(option) === String(state.config[key]));
    const next = options[(current + 1) % options.length];
    state.config[key] = next;
    setSegment(key.replace("_", "-"), next);
    renderQuickRow();
    button.classList.add("flash");
    setTimeout(() => button.classList.remove("flash"), 320);
    try {
      applyConfig(await api("/api/config", { method: "PUT", body: JSON.stringify({ [key]: next }) }));
    } catch (error) {
      toast(error.message, "error");
    }
  })
);

$("#scan-button").addEventListener("click", startScan);

$("#session-tag-add").addEventListener("click", () => addTag("session-tag-input", state.sessionTags, renderTags));
bindEnter("session-tag-input", () => addTag("session-tag-input", state.sessionTags, renderTags));
$("#default-tag-add").addEventListener("click", () => addTag("default-tag-input", state.defaultTags, renderTags));
bindEnter("default-tag-input", () => addTag("default-tag-input", state.defaultTags, renderTags));

$("#log-toggle").addEventListener("click", () => {
  const log = $("#log-lines");
  const open = log.hidden;
  log.hidden = !open;
  $("#log-toggle").setAttribute("aria-expanded", String(open));
});

$("#save-settings").addEventListener("click", () =>
  saveSettings().catch((error) => toast(error.message, "error"))
);

$("#test-scanner").addEventListener("click", async () => {
  const button = $("#test-scanner");
  button.disabled = true;
  try {
    await saveSettings(false);
    const result = await api("/api/test/scanner", { method: "POST", body: "{}" });
    toast(`${result.model} · eSCL ${result.version}`, "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("#test-paperless").addEventListener("click", async () => {
  const button = $("#test-paperless");
  button.disabled = true;
  try {
    await saveSettings(false);
    await api("/api/test/paperless", { method: "POST", body: "{}" });
    toast("Paperless-ngx verbunden", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("#discover-scanners").addEventListener("click", async () => {
  const button = $("#discover-scanners");
  button.disabled = true;
  await runDiscovery("discovery-subnet", "discovery-results", "scanner-url");
  button.disabled = false;
});

$("#wiz-discover").addEventListener("click", async () => {
  const button = $("#wiz-discover");
  button.disabled = true;
  await runDiscovery("wiz-subnet", "wiz-devices", "wiz-scanner-url");
  button.disabled = false;
});

$("#wiz-test-scanner").addEventListener("click", async () => {
  const button = $("#wiz-test-scanner");
  button.disabled = true;
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(wizardPayload()) });
    const result = await api("/api/test/scanner", { method: "POST", body: "{}" });
    toast(`${result.model} · eSCL ${result.version}`, "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("#wiz-test-paperless").addEventListener("click", async () => {
  const button = $("#wiz-test-paperless");
  button.disabled = true;
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify(wizardPayload()) });
    await api("/api/test/paperless", { method: "POST", body: "{}" });
    toast("Paperless-ngx verbunden", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("#wiz-tag-add").addEventListener("click", () => addTag("wiz-tag-input", state.wizardTags, renderWizardTags));
bindEnter("wiz-tag-input", () => addTag("wiz-tag-input", state.wizardTags, renderWizardTags));
$("#wiz-upload").addEventListener("change", syncWizardConditionals);
$("#wiz-ha").addEventListener("change", async () => {
  syncWizardConditionals();
  if (getValue("wiz-ha") && !getValue("wiz-ha-key")) await ensureHaKey("wiz-ha-key").catch(() => {});
});
$("#wiz-ha-copy").addEventListener("click", () => copyText(getValue("wiz-ha-key"), "API-Key kopiert"));
$("#wiz-next").addEventListener("click", wizardNext);
$("#wiz-back").addEventListener("click", () => {
  if (state.wizardStep === 0) return;
  state.wizardStep -= 1;
  renderWizardStep(-1);
});

$("#ha-copy").addEventListener("click", () => copyText(getValue("ha-api-key"), "API-Key kopiert"));
$("#ha-yaml-copy").addEventListener("click", () => copyText($("#ha-yaml").textContent, "YAML kopiert"));
$("#ha-rotate").addEventListener("click", async () => {
  try {
    const { api_key: key } = await api("/api/ha/key", { method: "POST", body: "{}" });
    setValue("ha-api-key", key);
    setValue("ha-enabled", true);
    renderHaYaml();
    toast("Neuer API-Key erzeugt", "success");
  } catch (error) {
    toast(error.message, "error");
  }
});
$("#ha-test").addEventListener("click", async () => {
  try {
    await saveSettings(false);
    const key = getValue("ha-api-key");
    if (!key) throw new Error("Kein API-Key vorhanden.");
    const response = await fetch("/api/ha/test", { method: "POST", headers: { "X-API-Key": key } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    toast("Schnittstelle erreichbar", "success");
  } catch (error) {
    toast(error.message, "error");
  }
});
$("#ha-enabled").addEventListener("change", async () => {
  if (getValue("ha-enabled") && !getValue("ha-api-key")) await ensureHaKey("ha-api-key").catch(() => {});
});

$("#reset-config").addEventListener("click", async () => {
  if (!window.confirm("Konfiguration wirklich löschen und neu einrichten?")) return;
  try {
    const config = await api("/api/setup/reset", { method: "POST", body: "{}" });
    state.sessionTags = [];
    applyConfig(config);
    openWizard(config);
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#preview-close").addEventListener("click", closePreview);
$("#preview-keep").addEventListener("click", keepPreview);
$("#preview-overlay").addEventListener("click", (event) => {
  if (event.target === $("#preview-overlay")) closePreview();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePreview();
});

/* --- boot ---------------------------------------------------------------- */

const params = new URLSearchParams(window.location.search);
if (params.get("view") === "settings") activateView("settings");

loadConfig()
  .then(async (config) => {
    await refreshState().catch(() => {});
    if (params.get("action") === "scan" && config.setup_complete) startScan();
  })
  .catch((error) => toast(error.message, "error"));

connectEvents();
setInterval(() => refreshState().catch(() => {}), 4000);

// Keep the UI honest after the phone wakes the PWA back up.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshState().catch(() => {});
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
