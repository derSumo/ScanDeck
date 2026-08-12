/* ==========================================================================
   Scan Deck — client logic
   ========================================================================== */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const LABELS = {
  source: { Platen: "Flachbett", Feeder: "Einzug" },
  color_mode: { RGB24: "Farbe", Grayscale8: "Grau" },
  output_format: { "application/pdf": "PDF", "image/jpeg": "JPEG" },
};

const QUICK_CYCLE = {
  source: ["Platen", "Feeder"],
  output_format: ["application/pdf", "image/jpeg"],
  resolution: [150, 200, 300, 600],
  color_mode: ["RGB24", "Grayscale8"],
};

const STAGE_TEXT = {
  merge: "Seiten werden zu einer PDF zusammengefügt …",
  start: "Scan wird vorbereitet …",
  connect: "Verbinde mit dem Scanner …",
  job: "Scanauftrag wird übermittelt …",
  capture: "Dokument wird erfasst …",
  store: "Scan wird gespeichert …",
  upload: "Übertrage nach Paperless-ngx …",
  done: "Fertig",
  error: "Fehlgeschlagen",
  cancelled: "Abgebrochen",
};

const STAGE_ORDER = ["connect", "capture", "store", "upload"];

const state = {
  config: {},
  defaultTags: [],
  sessionTags: [],
  wizardStep: 0,
  wizardTags: [],
  running: false,
  batch: { active: false, pages: [], replace_index: null },
  collections: { loaded: false },
  progressTitle: "Scan läuft",
  queue: 0,
  auth: { enabled: false, authenticated: true },
  capabilities: { known: false },
  pageIndex: 0,
  eta: null,
  etaAt: 0,
  scanStart: 0,
  previewTimer: null,
  elapsedTimer: null,
};

/* --- helpers ------------------------------------------------------------- */

// Fehler und Warnungen wollen gelesen werden; eine Bestaetigung darf huschen.
const TOAST_SECONDS = { error: 12, warning: 9 };

function toast(message, kind = "") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (element.className = "toast"), (TOAST_SECONDS[kind] || 3.6) * 1000);
  // Wer sie gelesen hat, klickt sie weg.
  element.onclick = () => {
    clearTimeout(toast.timer);
    element.className = "toast";
  };
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Die Sitzung kann jederzeit ablaufen; dann zurueck zur Anmeldung, statt
    // den Nutzer mit einem Fehler pro Hintergrundabfrage zu bewerfen.
    if (response.status === 401 && body.auth_required) {
      showLogin();
      throw Object.assign(new Error("Anmeldung erforderlich."), { authRequired: true });
    }
    throw new Error(body.error || `HTTP ${response.status}`);
  }
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

// Beidseitig gibt es nur im Einzug; auf dem Flachbett waere die Option Unsinn.
function syncDuplexRow() {
  const row = $("#duplex-row");
  if (!row) return;
  const feeder = getSegment("source", state.config.source) === "Feeder";
  const canDuplex = !state.capabilities.known || state.capabilities.duplex;
  row.hidden = !feeder || !canDuplex;
  if (!row.hidden) return;
  // Ein Geraet ohne beidseitigen Einzug soll den Schalter nicht nur verstecken,
  // sondern die Einstellung auch nicht heimlich gesetzt lassen.
  if (feeder && !canDuplex) setValue("duplex", false);
}

// Was das Geraet nicht kann, wird gar nicht erst angeboten: ein Legal-Scan auf
// einem A4-Vorlagenglas endete sonst in einem nackten "HTTP 409".
function syncDeviceLimits() {
  const caps = state.capabilities;
  const source = getSegment("source", state.config.source) || "Platen";
  const sizes = caps.known && caps.paper_sizes ? caps.paper_sizes[source] : null;
  const maxDpi = caps.known && caps.max_resolution ? caps.max_resolution[source] : null;

  $$('.seg[data-target="paper-size"] button').forEach((button) => {
    const ok = !sizes || sizes.includes(button.dataset.value);
    button.disabled = !ok;
    button.title = ok ? "" : `${button.dataset.value} passt nicht auf diese Quelle`;
    if (!ok && button.classList.contains("active")) setSegment("paper-size", sizes[0] || "A4");
  });

  // Die Liste der Stufen kennt das Geraet selbst; sie schlaegt die Obergrenze.
  const steps = caps.known && caps.resolutions ? caps.resolutions[source] : null;
  $$('.seg[data-target="resolution"] button').forEach((button) => {
    const dpi = Number(button.dataset.value);
    const ok = steps && steps.length ? steps.includes(dpi) : !maxDpi || dpi <= maxDpi;
    button.disabled = !ok;
    button.title = ok
      ? ""
      : steps && steps.length
      ? `Diese Quelle kann: ${steps.join(", ")} dpi`
      : `Diese Quelle scannt höchstens ${maxDpi} dpi`;
    if (!ok && button.classList.contains("active")) {
      const fallback = steps && steps.length
        ? steps.reduce((best, value) => (Math.abs(value - dpi) < Math.abs(best - dpi) ? value : best))
        : maxDpi;
      setSegment("resolution", fallback);
    }
  });
  syncDuplexRow();
}

async function loadCapabilities() {
  const caps = await api("/api/scanner/capabilities").catch(() => ({ known: false }));
  state.capabilities = caps;
  syncDeviceLimits();
  renderDeviceSheet();
}

const COLOR_NAMES = { RGB24: "Farbe", Grayscale8: "Graustufen", BlackAndWhite1: "Schwarzweiß" };
const FORMAT_NAMES = { "application/pdf": "PDF", "image/jpeg": "JPEG" };

// Antwortet auf "was kann mein Drucker eigentlich?" - ausgelesen, nicht geraten.
function renderDeviceSheet() {
  const caps = state.capabilities;
  const card = $("#device-card");
  card.hidden = !caps.known || !(caps.sheet || []).length;
  if (card.hidden) return;

  $("#device-model").textContent = `${caps.model} · eSCL ${caps.version}`
    + (caps.duplex ? " · beidseitiger Einzug" : "");

  const container = $("#device-sheet");
  container.replaceChildren();
  caps.sheet.forEach((source) => {
    const box = document.createElement("div");
    box.className = "device-source";
    const title = document.createElement("b");
    title.textContent = source.label;
    const list = document.createElement("dl");
    const rows = [
      ["Auflösung", source.resolutions.length ? `${source.resolutions.join(", ")} dpi` : "nicht gemeldet"],
      ["Fläche", source.area_mm[0] ? `${source.area_mm[0]} × ${source.area_mm[1]} mm` : "nicht gemeldet"],
      ["Papier", source.paper_sizes.join(", ") || "—"],
      ["Farbe", source.color_modes.map((mode) => COLOR_NAMES[mode] || mode).join(", ") || "—"],
      ["Format", source.formats.map((format) => FORMAT_NAMES[format] || format).join(", ") || "—"],
    ];
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      list.append(dt, dd);
    });
    box.append(title, list);
    container.append(box);
  });
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

  if (config.version) {
    $("#version-chip-text").textContent = `v${config.version}`;
    $("#about-version").textContent = `ScanDeck ${config.version}`;
  }
  setValue("update-check-enabled", config.update_check);
  setValue("metadata-enabled", config.metadata_enabled);
  setValue("quick-tags-enabled", config.quick_tags_enabled);
  setValue("prewarm-enabled", config.prewarm_enabled);
  setValue("cleanup-enabled", config.cleanup_enabled);
  setValue("cleanup-hours", config.cleanup_hours ?? 24);
  $("#metadata-card").hidden = !config.metadata_enabled;
  $("#quick-tags").hidden = !config.quick_tags_enabled;

  setSegment("output-format", config.output_format);
  setSegment("source", config.source);
  setSegment("paper-size", config.paper_size || "A4");
  setSegment("resolution", config.resolution);
  setSegment("color-mode", config.color_mode);
  setValue("duplex", config.duplex);
  syncDeviceLimits();

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
    paper_size: getSegment("paper-size", state.config.paper_size || "A4"),
    duplex: getValue("duplex"),
    resolution: Number(getSegment("resolution", state.config.resolution)),
    color_mode: getSegment("color-mode", state.config.color_mode),
    output_format: getSegment("output-format", state.config.output_format),
    ha_enabled: getValue("ha-enabled"),
    ha_webhook_url: getValue("ha-webhook-url"),
    update_check: getValue("update-check-enabled"),
    metadata_enabled: getValue("metadata-enabled"),
    quick_tags_enabled: getValue("quick-tags-enabled"),
    prewarm_enabled: getValue("prewarm-enabled"),
    cleanup_enabled: getValue("cleanup-enabled"),
    cleanup_hours: Number(getValue("cleanup-hours") || 24),
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

/* --- batch mode ---------------------------------------------------------- */

const TOOL_ICONS = {
  // Rotate clockwise, rescan, discard.
  rotate: '<path d="M20 8a8 8 0 1 0 1.5 6"/><path d="M20 3v5h-5"/>',
  replace: '<path d="M4 8V5h3M20 8V5h-3M4 16v3h3M20 16v3h-3"/><circle cx="12" cy="12" r="3.2"/>',
  remove: '<path d="M6 6l12 12M18 6L6 18"/>',
};

function toolButton(kind, title, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = kind;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${TOOL_ICONS[kind]}</svg>`;
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick();
  });
  // Never let a tap on a button start a drag.
  button.addEventListener("pointerdown", (event) => event.stopPropagation());
  return button;
}

/* Drag to reorder — one implementation for mouse and touch via pointer events.
   Tiles swap in the DOM while dragging and are animated with a FLIP pass, so
   the grid stays correct no matter how many columns it has. */
function makeDraggable(tile) {
  // iOS shows its magnifier and share sheet on a long press; the drag needs
  // that gesture, so the context menu is refused outright.
  tile.addEventListener("contextmenu", (event) => event.preventDefault());

  tile.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 && event.pointerType === "mouse") return;

    const container = tile.parentElement;
    const startX = event.clientX;
    const startY = event.clientY;
    let dragging = false;
    let offsetX = 0;
    let offsetY = 0;

    // Safari keeps scrolling mid-gesture even with touch-action set, so the
    // scroll is refused explicitly while a page is being dragged.
    const blockScroll = (touchEvent) => {
      if (dragging) touchEvent.preventDefault();
    };
    document.addEventListener("touchmove", blockScroll, { passive: false });

    // Touch needs a short hold so a normal swipe still scrolls the page.
    const holdTimer =
      event.pointerType === "touch" ? setTimeout(() => begin(), 180) : null;

    function begin() {
      if (dragging) return;
      dragging = true;
      // Drop any text selection iOS may have started during the hold.
      window.getSelection?.()?.removeAllRanges?.();
      tile.setPointerCapture(event.pointerId);
      tile.classList.add("dragging");
      container.classList.add("reordering");
      document.body.classList.add("dragging-page");
      if (navigator.vibrate) navigator.vibrate(8);
    }

    function move(moveEvent) {
      const dx = moveEvent.clientX - startX;
      const dy = moveEvent.clientY - startY;
      if (!dragging) {
        if (event.pointerType === "touch") return; // waiting for the hold
        if (Math.hypot(dx, dy) < 6) return; // mouse: ignore a plain click
        begin();
      }
      moveEvent.preventDefault();
      offsetX = dx;
      offsetY = dy;
      tile.style.transform = `translate(${dx}px, ${dy}px) scale(1.06)`;

      const target = tileUnderPointer(container, tile, moveEvent.clientX, moveEvent.clientY);
      if (target) reinsert(container, tile, target, () => (tile.style.transform = `translate(${offsetX}px, ${offsetY}px) scale(1.06)`));
    }

    function finish() {
      if (holdTimer) clearTimeout(holdTimer);
      document.removeEventListener("touchmove", blockScroll);
      tile.removeEventListener("pointermove", move);
      tile.removeEventListener("pointerup", finish);
      tile.removeEventListener("pointercancel", finish);
      if (!dragging) return;
      tile.classList.remove("dragging");
      container.classList.remove("reordering");
      document.body.classList.remove("dragging-page");
      tile.style.transform = "";
      persistOrder(container);
    }

    tile.addEventListener("pointermove", move);
    tile.addEventListener("pointerup", finish);
    tile.addEventListener("pointercancel", finish);
  });
}

function tileUnderPointer(container, dragged, x, y) {
  return (
    Array.from(container.children).find((child) => {
      if (child === dragged) return false;
      const box = child.getBoundingClientRect();
      return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
    }) || null
  );
}

function reinsert(container, dragged, target, restoreDragTransform) {
  const siblings = Array.from(container.children);
  const from = siblings.indexOf(dragged);
  const to = siblings.indexOf(target);

  // FLIP: remember where everything sits, move the node, animate the delta.
  const before = new Map(siblings.map((child) => [child, child.getBoundingClientRect()]));
  container.insertBefore(dragged, from < to ? target.nextSibling : target);

  siblings.forEach((child) => {
    if (child === dragged) return;
    const start = before.get(child);
    const now = child.getBoundingClientRect();
    const dx = start.left - now.left;
    const dy = start.top - now.top;
    if (!dx && !dy) return;
    child.style.transition = "none";
    child.style.transform = `translate(${dx}px, ${dy}px)`;
    requestAnimationFrame(() => {
      child.style.transition = "";
      child.style.transform = "";
    });
  });
  restoreDragTransform();
  renumber(container);
}

function renumber(container) {
  Array.from(container.children).forEach((tile, position) => {
    tile.querySelector(".page-number").textContent = String(position + 1);
  });
}

async function persistOrder(container) {
  const order = Array.from(container.children).map((tile) => Number(tile.dataset.index));
  if (order.every((value, position) => value === position)) return;
  try {
    renderBatch(await api("/api/batch/order", { method: "POST", body: JSON.stringify({ order }) }));
    toast("Reihenfolge gespeichert");
  } catch (error) {
    toast(error.message, "error");
    loadBatch().catch(() => {});
  }
}

async function rotatePage(index) {
  try {
    renderBatch(await api(`/api/batch/page/${index}/rotate`, { method: "POST", body: JSON.stringify({ degrees: 90 }) }));
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderBatch(batch) {
  state.batch = batch;
  const pages = batch.pages || [];
  const armed = batch.replace_index;

  document.body.classList.toggle("batch-on", batch.active);
  setValue("batch-toggle", batch.active);
  togglePanel(batch.active);
  // While collecting pages nothing is uploaded, so that step is not shown.
  $("#progress-steps").classList.toggle("no-upload", batch.active);
  $("#batch-count").hidden = !batch.active || pages.length === 0;
  $("#batch-count").textContent = String(pages.length);
  $("#batch-finish").disabled = pages.length === 0;

  $("#batch-hint").textContent =
    armed !== null && armed !== undefined
      ? `Nächster Scan ersetzt Seite ${armed + 1}`
      : pages.length === 0
      ? "Scanne die erste Seite"
      : `${pages.length} Seite(n) — weiter scannen oder ablegen`;

  const container = $("#batch-pages");
  container.replaceChildren();
  pages.forEach((page, index) => {
    const tile = document.createElement("div");
    tile.className = `batch-page${index === armed ? " armed" : ""}`;
    tile.dataset.index = String(index);

    const image = document.createElement("img");
    // The rotation belongs in the URL so a turned page reloads its preview.
    image.src = `/api/batch/page/${index}/preview?v=${encodeURIComponent(page.name)}&r=${page.rotation || 0}`;
    image.alt = `Seite ${index + 1}`;
    image.loading = "lazy";
    image.draggable = false;

    const number = document.createElement("span");
    number.className = "page-number";
    number.textContent = String(index + 1);

    const grip = document.createElement("span");
    grip.className = "grip";
    grip.textContent = "⠿";

    const tools = document.createElement("div");
    tools.className = "page-tools";
    tools.append(
      toolButton("rotate", "Seite um 90° drehen", () => rotatePage(index)),
      toolButton("replace", index === armed ? "Ersetzen abbrechen" : "Diese Seite neu scannen", () =>
        armReplace(index === armed ? null : index)
      ),
      toolButton("remove", `Seite ${index + 1} entfernen`, () => removePage(index))
    );

    // Gross ansehen: auf dem Vorschaubild ist kaum zu erkennen, welche Seite
    // welche ist - beim Sortieren ist genau das aber die Frage.
    image.addEventListener("click", (event) => {
      event.stopPropagation();
      openPage(index);
    });

    tile.append(image, number, grip, tools);
    makeDraggable(tile);
    container.append(tile);
  });

  // Steht die Lupe offen, zeigt sie nach jeder Aenderung den neuen Stand.
  if (!$("#page-overlay").hidden) showPage(state.pageIndex);

  const armedNow = armed !== null && armed !== undefined;
  $("#orb-label").textContent = state.running
    ? "Scannt …"
    : armedNow
    ? `Seite ${armed + 1} ersetzen`
    : batch.active
    ? pages.length === 0
      ? "Erste Seite scannen"
      : "Seite hinzufügen"
    : "Scan starten";
}

function togglePanel(open) {
  const panel = $("#batch-panel");
  if (open) {
    clearTimeout(togglePanel.timer);
    panel.classList.remove("closing");
    panel.hidden = false;
    return;
  }
  if (panel.hidden) return;
  panel.classList.add("closing");
  togglePanel.timer = setTimeout(() => {
    panel.hidden = true;
    panel.classList.remove("closing");
    $("#batch-help").hidden = true;
    $("#batch-help-toggle").setAttribute("aria-expanded", "false");
  }, 290);
}

async function loadBatch() {
  renderBatch(await api("/api/batch"));
}

async function toggleBatch() {
  const on = getValue("batch-toggle");
  try {
    if (on) {
      renderBatch(await api("/api/batch/start", { method: "POST", body: "{}" }));
      toast("Stapel gestartet — jede Seite wird gesammelt");
    } else if ((state.batch?.pages || []).length && !window.confirm("Gesammelte Seiten verwerfen?")) {
      setValue("batch-toggle", true);
    } else {
      renderBatch(await api("/api/batch/cancel", { method: "POST", body: "{}" }));
    }
  } catch (error) {
    toast(error.message, "error");
    loadBatch().catch(() => {});
  }
}

async function armReplace(index) {
  try {
    renderBatch(await api("/api/batch/replace", { method: "POST", body: JSON.stringify({ index }) }));
    if (index !== null) toast(`Scan starten, um Seite ${index + 1} zu ersetzen`);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function removePage(index) {
  try {
    renderBatch(await api(`/api/batch/page/${index}`, { method: "DELETE" }));
  } catch (error) {
    toast(error.message, "error");
  }
}

async function finishBatch() {
  try {
    state.progressTitle = "Stapel wird abgeschlossen";
    setScanning(true);
    setProgress(10, "merge");
    await api("/api/batch/finish", {
      method: "POST",
      body: JSON.stringify({ session_tags: state.sessionTags, ...scanMetadata() }),
    });
  } catch (error) {
    setScanning(false);
    hideProgress();
    toast(error.message, "error");
  }
}

/* --- history ------------------------------------------------------------- */

const STATUS_LABEL = {
  success: "In Paperless",
  processing: "Wird verarbeitet",
  pending: "Wartet auf Upload",
  failed: "Fehlgeschlagen",
  duplicate: "Duplikat",
  local: "Nur lokal",
};

const HISTORY_ICONS = {
  retry: '<path d="M20 8a8 8 0 1 0 1.5 6"/><path d="M20 3v5h-5"/>',
  open: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  delete: '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/>',
};

function historyButton(kind, title, onClick, href) {
  const element = document.createElement(href ? "a" : "button");
  if (href) {
    element.href = href;
    element.target = "_blank";
    element.rel = "noopener";
  } else {
    element.type = "button";
    element.addEventListener("click", onClick);
  }
  element.className = kind;
  element.title = title;
  element.setAttribute("aria-label", title);
  element.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${HISTORY_ICONS[kind]}</svg>`;
  return element;
}

function renderHistory(data) {
  const list = $("#history-list");
  list.replaceChildren();
  const entries = data.jobs || [];

  $("#history-summary").textContent = entries.length
    ? data.open
      ? `${entries.length} Scans · ${data.open} noch offen`
      : `${entries.length} Scans · alles übertragen`
    : "Noch keine Scans.";

  entries.forEach((job) => {
    const row = document.createElement("div");
    const open = job.status === "pending" || job.status === "processing";
    const bad = job.status === "failed";
    row.className = `history-item${open ? " is-open" : ""}${bad ? " is-bad" : ""}`;

    let thumb;
    if (job.exists) {
      thumb = document.createElement("img");
      thumb.className = "history-thumb";
      thumb.src = `/api/history/${job.id}/preview`;
      thumb.alt = "";
      thumb.loading = "lazy";
    } else {
      thumb = document.createElement("div");
      thumb.className = "history-thumb missing";
      thumb.textContent = "✓";
      thumb.title = "Lokale Kopie wurde aufgeräumt";
    }

    const main = document.createElement("div");
    main.className = "history-main";
    main.append(Object.assign(document.createElement("b"), { textContent: job.name }));

    const meta = document.createElement("span");
    meta.className = "history-meta";
    const badge = document.createElement("span");
    badge.className = `badge ${job.status}`;
    badge.textContent = STATUS_LABEL[job.status] || job.status;
    const when = new Date(job.created).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
    meta.append(badge, document.createTextNode(` ${when}`));
    if (job.pages > 1) meta.append(document.createTextNode(` · ${job.pages} Seiten`));
    if (job.document_id) meta.append(document.createTextNode(` · #${job.document_id}`));
    main.append(meta);

    if (job.error) {
      main.append(
        Object.assign(document.createElement("span"), { className: "history-error", textContent: job.error, title: job.error })
      );
    }

    const tools = document.createElement("div");
    tools.className = "history-tools";
    if (job.exists) {
      tools.append(historyButton("retry", "Erneut an Paperless senden", () => retryJob(job.id)));
      tools.append(historyButton("open", "Datei öffnen", null, `/api/history/${job.id}/file`));
    }
    tools.append(historyButton("delete", "Eintrag und Datei löschen", () => deleteJob(job.id, job.name)));

    row.append(thumb, main, tools);
    list.append(row);
  });
}

async function loadHistory() {
  try {
    renderHistory(await api("/api/history"));
  } catch (error) {
    $("#history-summary").textContent = error.message;
  }
}

async function retryJob(id) {
  try {
    await api(`/api/history/${id}/retry`, { method: "POST", body: "{}" });
    toast("Wird erneut gesendet");
    setTimeout(loadHistory, 1200);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteJob(id, name) {
  if (!window.confirm(`${name} löschen? Die lokale Datei wird mit entfernt.`)) return;
  try {
    await api(`/api/history/${id}`, { method: "DELETE" });
    loadHistory();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* --- Paperless pickers --------------------------------------------------- */

function fillSelect(id, items, selected) {
  const select = document.getElementById(id);
  const keep = selected ?? select.value;
  select.replaceChildren(Object.assign(document.createElement("option"), { value: "", textContent: "— nicht setzen —" }));
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = String(item.id);
    option.textContent = item.count ? `${item.name} (${item.count})` : item.name;
    select.append(option);
  });
  select.value = keep || "";
}

function renderQuickTags(tags) {
  const box = $("#quick-tags");
  box.replaceChildren();
  tags.slice(0, 12).forEach((tag) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = tag.name;
    button.className = state.sessionTags.includes(tag.name) ? "used" : "";
    button.addEventListener("click", () => {
      if (state.sessionTags.includes(tag.name)) {
        state.sessionTags = state.sessionTags.filter((item) => item !== tag.name);
      } else {
        state.sessionTags.push(tag.name);
      }
      renderTags();
      renderQuickTags(state.collections.tags || []);
    });
    box.append(button);
  });
  box.hidden = !tags.length;
}

async function loadCollections(force = false) {
  const config = state.config;
  if (!config.metadata_enabled && !config.quick_tags_enabled) return;
  if (state.collections.loaded && !force) return;
  try {
    const data = await api("/api/paperless/collections");
    state.collections = { ...data, loaded: true };
    if (config.metadata_enabled) {
      fillSelect("correspondent", data.correspondents || []);
      fillSelect("document-type", data.document_types || []);
    }
    if (config.quick_tags_enabled) renderQuickTags(data.tags || []);
  } catch (error) {
    if (force) toast(error.message, "error");
  }
}

function scanMetadata() {
  if (!state.config.metadata_enabled) return {};
  const meta = {};
  const correspondent = getValue("correspondent");
  const documentType = getValue("document-type");
  if (correspondent) meta.correspondent = Number(correspondent);
  if (documentType) meta.document_type = Number(documentType);
  return meta;
}

/* --- update check -------------------------------------------------------- */

async function checkForUpdate(force = false) {
  const chip = $("#version-chip");
  const link = $("#about-link");
  const status = $("#about-status");
  const releases = state.config.releases_url || "#";

  try {
    const info = await api(`/api/update${force ? "?force=1" : ""}`);
    chip.href = info.url || releases;
    link.href = info.url || releases;

    if (info.disabled) {
      status.textContent = "Update-Prüfung ist deaktiviert.";
      chip.classList.remove("has-update");
      link.hidden = true;
      return info;
    }
    if (info.update_available) {
      chip.classList.add("has-update");
      $("#version-chip-text").textContent = info.latest;
      status.textContent = `Version ${info.latest} ist verfügbar.`;
      link.hidden = false;
      if (force) toast(`Update ${info.latest} verfügbar`, "success");
    } else {
      chip.classList.remove("has-update");
      $("#version-chip-text").textContent = `v${info.current}`;
      status.textContent = info.error
        ? "GitHub lieferte keine Versionsinfo."
        : info.latest
        ? "Aktuellste Version installiert."
        : "Auf GitHub ist noch keine Version veröffentlicht.";
      link.hidden = true;
      if (force && !info.error) toast("Alles aktuell", "success");
    }
    return info;
  } catch (error) {
    status.textContent = "Update-Prüfung fehlgeschlagen.";
    if (force) toast(error.message, "error");
    return null;
  }
}

/* --- navigation ---------------------------------------------------------- */

function activateView(name) {
  document.body.dataset.view = name;
  if (name === "history") loadHistory();
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

function renderElapsed() {
  const elapsed = Math.round((Date.now() - state.scanStart) / 1000);
  // Count the estimate down between two events instead of letting it jump.
  const remaining =
    state.eta === null ? null : Math.max(0, state.eta - Math.round((Date.now() - state.etaAt) / 1000));
  $("#progress-elapsed").textContent = remaining
    ? `${elapsed} s · noch ca. ${remaining} s`
    : `${elapsed} s`;
}

function showProgress() {
  const overlay = $("#progress-overlay");
  overlay.hidden = false;
  overlay.classList.remove("closing");
  $("#progress-title").textContent = state.progressTitle || "Scan läuft";
  const cancel = $("#scan-cancel");
  cancel.disabled = false;
  cancel.hidden = false;
  cancel.textContent = "Scan abbrechen";
  state.scanStart = Date.now();
  state.eta = null;
  state.etaAt = 0;
  clearInterval(state.elapsedTimer);
  state.elapsedTimer = setInterval(renderElapsed, 500);
  renderElapsed();
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
    state.progressTitle = state.batch?.active ? "Seite wird gescannt" : "Scan läuft";
    setScanning(true);
    setProgress(2, "start");
    await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ session_tags: state.sessionTags, overrides: scanMetadata() }),
    });
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
  if (event.eta !== undefined) {
    state.eta = event.eta;
    state.etaAt = Date.now();
    renderElapsed();
  }
  setProgress(event.progress, event.stage);

  if (event.stage === "done") {
    $("#progress-title").textContent = "Fertig";
    setTimeout(() => {
      hideProgress();
      setScanning(false);
      if (event.batch) {
        // Inside a batch the thumbnail strip is the feedback; a full-screen
        // preview after every page would only be in the way.
        loadBatch().catch(() => {});
        // Ein Einzug liefert mehrere Blatt auf einmal; die Meldung muss das sagen.
        const added = event.added ?? 1;
        const what = added > 1 ? `${added} Seiten erfasst` : `Seite ${(event.page_index ?? 0) + 1} erfasst`;
        toast(`${what} · ${event.pages} im Stapel`, "success");
      } else {
        showPreview(event.file);
        loadBatch().catch(() => {});
        loadHistory().catch(() => {});
      }
      refreshState().catch(() => {});
    }, 620);
  }
  if (event.stage === "cancelled") {
    $("#progress-title").textContent = "Abgebrochen";
    $("#scan-cancel").hidden = true;
    setTimeout(() => {
      hideProgress();
      setScanning(false);
      loadBatch().catch(() => {});
      loadHistory().catch(() => {});
      toast("Scan abgebrochen", "warning");
    }, 700);
  }

  if (event.stage === "error") {
    $("#progress-title").textContent = "Fehlgeschlagen";
    $("#progress-stage").textContent = event.error || "Unbekannter Fehler";
    $("#scan-cancel").hidden = true;
    // Fehler bleiben laenger stehen: sie wollen gelesen werden, nicht erahnt.
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
  const badge = $("#queue-badge");
  badge.hidden = !scanState.queue;
  badge.textContent = String(scanState.queue || 0);
  if (document.body.dataset.view === "history" && scanState.queue !== state.queue) loadHistory();
  state.queue = scanState.queue;
  if (scanState.batch) {
    // Keeps a second device (or a batch driven by Home Assistant) in sync.
    const changed = JSON.stringify(scanState.batch) !== JSON.stringify(state.batch);
    if (changed) renderBatch(scanState.batch);
  }
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

async function runDiscovery(subnetId, containerId, targetId, auto = false) {
  const container = document.getElementById(containerId);
  const status = Object.assign(document.createElement("p"), {
    textContent: auto ? "Netzwerke werden durchsucht … das dauert einen Moment." : "Suche läuft …",
  });
  container.replaceChildren(status);
  try {
    const found = await api("/api/discover/scanners", {
      method: "POST",
      body: JSON.stringify({ discovery_subnet: auto ? "" : getValue(subnetId), auto }),
    });
    container.replaceChildren();
    if (!found.devices.length) {
      container.append(
        Object.assign(document.createElement("p"), {
          textContent: auto
            ? "Kein Scanner gefunden. Trage das Netzwerk unten manuell ein."
            : "Kein eSCL-Scanner gefunden.",
        })
      );
      return;
    }
    if (found.subnet) {
      setValue(subnetId, found.subnet);
      container.append(
        Object.assign(document.createElement("p"), { textContent: `Gefunden in ${found.subnet}` })
      );
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
    if (["output-format", "source", "paper-size", "resolution", "color-mode"].includes(group.dataset.target)) {
      state.config[group.dataset.target.replace("-", "_")] =
        group.dataset.target === "resolution" ? Number(button.dataset.value) : button.dataset.value;
      renderQuickRow();
      syncDeviceLimits();
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
    // Der Test liefert die Geraetegrenzen gleich mit, also direkt anwenden.
    state.capabilities = { ...result, known: true };
    syncDeviceLimits();
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

function bindDiscovery(buttonId, subnetId, containerId, targetId, auto) {
  const button = $(`#${buttonId}`);
  button.addEventListener("click", async () => {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Suche läuft …";
    await runDiscovery(subnetId, containerId, targetId, auto);
    button.textContent = label;
    button.disabled = false;
  });
}

bindDiscovery("discover-scanners", "discovery-subnet", "discovery-results", "scanner-url", true);
bindDiscovery("discover-manual", "discovery-subnet", "discovery-results", "scanner-url", false);
bindDiscovery("wiz-discover", "wiz-subnet", "wiz-devices", "wiz-scanner-url", true);
bindDiscovery("wiz-discover-manual", "wiz-subnet", "wiz-devices", "wiz-scanner-url", false);

$("#history-reload").addEventListener("click", loadHistory);
$("#metadata-reload").addEventListener("click", () => loadCollections(true));
$$("#metadata-enabled, #quick-tags-enabled").forEach((toggle) =>
  toggle.addEventListener("change", async () => {
    try {
      applyConfig(await saveSettings(false));
      state.collections.loaded = false;
      await loadCollections(true);
    } catch (error) {
      toast(error.message, "error");
    }
  })
);

$("#batch-toggle").addEventListener("change", toggleBatch);
$("#batch-help-toggle").addEventListener("click", () => {
  const help = $("#batch-help");
  const open = help.hidden;
  help.hidden = !open;
  $("#batch-help-toggle").setAttribute("aria-expanded", String(open));
});
$("#batch-finish").addEventListener("click", finishBatch);
$("#batch-cancel").addEventListener("click", async () => {
  if ((state.batch?.pages || []).length && !window.confirm("Gesammelte Seiten verwerfen?")) return;
  try {
    renderBatch(await api("/api/batch/cancel", { method: "POST", body: "{}" }));
    toast("Stapel verworfen");
  } catch (error) {
    toast(error.message, "error");
  }
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

$("#update-check").addEventListener("click", async () => {
  const button = $("#update-check");
  button.disabled = true;
  try {
    await saveSettings(false);
    await checkForUpdate(true);
  } finally {
    button.disabled = false;
  }
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

/* --- Seitenlupe ----------------------------------------------------------- */

function openPage(index) {
  $("#page-overlay").hidden = false;
  document.body.classList.add("is-locked");
  showPage(index);
}

function showPage(index) {
  const pages = state.batch.pages || [];
  if (!pages.length) return closePage();
  state.pageIndex = Math.max(0, Math.min(index, pages.length - 1));
  const page = pages[state.pageIndex];
  $("#page-title").textContent = `Seite ${state.pageIndex + 1} von ${pages.length}`;
  const image = $("#page-image");
  image.src = `/api/batch/page/${state.pageIndex}/preview?v=${encodeURIComponent(page.name)}&r=${page.rotation || 0}`;
  image.alt = `Seite ${state.pageIndex + 1}`;
  $("#page-prev").disabled = state.pageIndex === 0;
  $("#page-next").disabled = state.pageIndex === pages.length - 1;
  $("#page-replace").textContent =
    state.batch.replace_index === state.pageIndex ? "Ersetzen abbrechen" : "Neu scannen";
}

function closePage() {
  $("#page-overlay").hidden = true;
  document.body.classList.remove("is-locked");
  $("#page-image").removeAttribute("src");
}

$("#page-close").addEventListener("click", closePage);
$("#page-overlay").addEventListener("click", (event) => {
  if (event.target === $("#page-overlay")) closePage();
});
$("#page-prev").addEventListener("click", () => showPage(state.pageIndex - 1));
$("#page-next").addEventListener("click", () => showPage(state.pageIndex + 1));
$("#page-rotate").addEventListener("click", () => rotatePage(state.pageIndex));
$("#page-replace").addEventListener("click", async () => {
  const armed = state.batch.replace_index === state.pageIndex;
  await armReplace(armed ? null : state.pageIndex);
  closePage();
});
$("#page-remove").addEventListener("click", async () => {
  const index = state.pageIndex;
  await removePage(index);
  const pages = state.batch.pages || [];
  if (!pages.length) closePage();
  else showPage(Math.min(index, pages.length - 1));
});

document.addEventListener("keydown", (event) => {
  if ($("#page-overlay").hidden) return;
  if (event.key === "Escape") closePage();
  if (event.key === "ArrowLeft") showPage(state.pageIndex - 1);
  if (event.key === "ArrowRight") showPage(state.pageIndex + 1);
});

/* --- Scan abbrechen ------------------------------------------------------- */

$("#scan-cancel").addEventListener("click", async () => {
  const button = $("#scan-cancel");
  button.disabled = true;
  button.textContent = "Wird abgebrochen …";
  try {
    await api("/api/scan/cancel", { method: "POST" });
  } catch (error) {
    toast(error.message, "error");
  }
});

/* --- Zugriffsschutz ------------------------------------------------------- */

function showLogin(message = "") {
  const overlay = $("#login-overlay");
  if (!overlay.hidden && !message) return;
  overlay.hidden = false;
  document.body.classList.add("is-locked");
  const error = $("#login-error");
  error.hidden = !message;
  error.textContent = message;
  $("#login-password").focus();
}

function hideLogin() {
  $("#login-overlay").hidden = true;
  document.body.classList.remove("is-locked");
  $("#login-password").value = "";
  $("#login-error").hidden = true;
}

function renderAuthCard(authState) {
  state.auth = authState;
  $("#auth-on").hidden = !authState.enabled;
  $("#auth-off").hidden = Boolean(authState.enabled);
}

async function refreshAuth() {
  const authState = await api("/api/auth/state");
  renderAuthCard(authState);
  return authState;
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#login-submit");
  button.disabled = true;
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: $("#login-password").value }),
    });
    hideLogin();
    await boot();
  } catch (error) {
    // Der 401 der Anmeldung ist die Antwort selbst, nicht der Rauswurf.
    $("#login-error").hidden = false;
    $("#login-error").textContent = error.message;
    $("#login-password").select();
  } finally {
    button.disabled = false;
  }
});

$("#auth-enable").addEventListener("click", async () => {
  const password = getValue("auth-password");
  if (password !== getValue("auth-password-repeat")) {
    toast("Die Passwörter stimmen nicht überein", "error");
    return;
  }
  try {
    renderAuthCard(await api("/api/auth/enable", { method: "POST", body: JSON.stringify({ password }) }));
    setValue("auth-password", "");
    setValue("auth-password-repeat", "");
    toast("Zugriffsschutz eingeschaltet", "success");
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#auth-change").addEventListener("click", async () => {
  try {
    renderAuthCard(await api("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ current: getValue("auth-current"), password: getValue("auth-new") }),
    }));
    setValue("auth-current", "");
    setValue("auth-new", "");
    toast("Passwort geändert — andere Geräte müssen sich neu anmelden", "success");
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#auth-disable").addEventListener("click", async () => {
  if (!confirm("Zugriffsschutz ausschalten? Danach kommt jeder im Netz ohne Passwort an die Oberfläche.")) return;
  try {
    renderAuthCard(await api("/api/auth/disable", { method: "POST" }));
    toast("Zugriffsschutz ausgeschaltet", "success");
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#auth-logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

/* --- boot ---------------------------------------------------------------- */

const params = new URLSearchParams(window.location.search);
if (params.get("view") === "settings") activateView("settings");

let started = false;

async function boot() {
  const authState = await refreshAuth();
  if (!authState.authenticated) {
    showLogin();
    return;
  }
  hideLogin();

  const config = await loadConfig();
  await refreshState().catch(() => {});
  checkForUpdate().catch(() => {});
  loadCollections().catch(() => {});
  loadCapabilities().catch(() => {});
  prewarmScanner();

  if (!started) {
    started = true;
    // Erst nach der Anmeldung verbinden: ein Ereignisstrom gegen eine
    // geschlossene Tuer wuerde nur endlos neu verbinden.
    connectEvents();
    pollState();
    // A long-lived PWA tab should still notice a release eventually.
    setInterval(() => checkForUpdate().catch(() => {}), 6 * 3600 * 1000);
  }
  if (params.get("action") === "scan" && config.setup_complete) startScan();
}

boot().catch((error) => {
  if (!error.authRequired) toast(error.message, "error");
});

// The event stream carries the live picture; this poll only catches what it
// misses (a resumed PWA, a batch driven from another device). So it runs fast
// while something is happening and backs off when nothing is — a phone in a
// pocket should not wake its radio every four seconds, and every open tab
// holds one of the handful of worker threads the container has.
function pollDelay() {
  if (document.hidden) return 60000;
  if (state.running) return 2000;
  return state.queue ? 8000 : 15000;
}

function pollState() {
  const tick = () => {
    const again = () => setTimeout(tick, pollDelay());
    // Nicht gegen die Anmeldemaske pollen: das brächte nur 401 im Takt.
    if (document.hidden || !$("#login-overlay").hidden) return again();
    refreshState().catch(() => {}).finally(again);
  };
  setTimeout(tick, pollDelay());
}

// Keep the UI honest after the phone wakes the PWA back up.
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !$("#login-overlay").hidden) return;
  refreshState().catch(() => {});
  prewarmScanner();
});

// Weckt den Scanner, waehrend der Nutzer noch waehlt: HP-Geraete schlafen ein
// und brauchen sonst beim ersten Scan spuerbar laenger.
function prewarmScanner() {
  if (!state.config.prewarm_enabled || !state.config.scanner_url || state.running) return;
  if (Date.now() - (state.prewarmedAt || 0) < 60000) return;
  state.prewarmedAt = Date.now();
  fetch("/api/scanner/prewarm", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
    .catch(() => {});
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", async () => {
    try {
      const registration = await navigator.serviceWorker.register("/sw.js");
      // Ohne das zeigt eine installierte App weiter die alte Oberflaeche, bis
      // sie irgendwann von selbst neu startet.
      registration.update().catch(() => {});
      let reloading = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (reloading) return;
        reloading = true;
        window.location.reload();
      });
    } catch {
      /* ohne Service Worker laeuft die App normal weiter */
    }
  });
}
