// Move Sensor BLE — integration panel (v1.20).
// Tabs Overview / Settings / Advanced built from Home Assistant's own cards (tile + card features,
// heading badges, history graph, entities). No custom card libraries needed.
//  * Overview: status badges, "Needs attention" (only when something is wrong), four tiles.
//    Tap a tile = our sheet (history + that value's settings); hold = Home Assistant's own dialog.
//  * Settings: the everyday settings, rarely used ones under "Show more".
//  * Advanced: firmware, Bluetooth, Wi-Fi (service mode), calibration, diagnostics — collapsed sections.
// Entities come from the integration (panel.config.devices[].ids, keyed by unique_id suffix).

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "settings", label: "Settings" },
  { id: "advanced", label: "Advanced" },
];

// keys used by the layout; anything else the integration adds lands in Advanced › Diagnostics
const SHEETS = {
  motion: { entity: "occupancy", title: "Motion", info: ["last_motion", "pir_stuck"],
            settings: ["setting_keep_time", "setting_led", "setting_detect_delay", "setting_motion_refresh"] },
  temp: { entity: "temperature", title: "Temperature", info: [],
          settings: ["setting_climate_interval", "setting_temp_offset"] },
  hum: { entity: "humidity", title: "Humidity", info: [],
         settings: ["setting_climate_interval", "setting_hum_offset"] },
  bat: { entity: "battery", title: "Battery", info: ["battery_days_left", "battery_remaining", "voltage"],
         settings: ["setting_battery_capacity", "setting_battery_low"] },
};

const GROUPS = [
  { title: "Motion", rows: ["setting_keep_time", "setting_led"], more: ["setting_detect_delay", "setting_motion_refresh"] },
  { title: "Temperature & humidity", rows: ["setting_climate_interval"], more: ["setting_temp_offset", "setting_hum_offset"] },
  { title: "Battery", rows: ["setting_battery_capacity", "setting_battery_low"], more: [] },
];

const SECTIONS = [
  { id: "firmware", title: "Firmware", icon: "mdi:chip" },
  { id: "bluetooth", title: "Bluetooth", icon: "mdi:bluetooth",
    keys: ["link", "signal_level", "signal", "last_seen", "tx_power", "setting_motion_window",
           "setting_climate_window", "auto_window", "identify"] },
  { id: "wifi", title: "Wi-Fi (service mode)", icon: "mdi:wifi" },
  { id: "calibration", title: "Calibration", icon: "mdi:tune-vertical",
    keys: ["setting_voltage_factor", "pir_stuck_threshold"] },
  { id: "diagnostics", title: "Diagnostics", icon: "mdi:stethoscope",
    keys: ["device_id", "voltage", "battery_remaining", "last_reset_reason", "brownout_count", "crash_count",
           "ota_key", "settings_status", "settings_change", "settings_read", "wifi_reset"] },
];

// how each setting is changed (card feature): buttons = − value +, slider, box = text field (keyboard)
const CONTROL = {
  setting_battery_low: "slider",
  setting_battery_capacity: "box",
};
const SHORT = { battery_days_left: "Days left", battery_remaining: "Remaining", voltage: "Voltage",
                last_motion: "Last motion", pir_stuck: "PIR stuck", wifi_reset: "Reset Wi-Fi" };
const ACTION_NAMES = { identify: "Blink LED", settings_read: "Read", wifi_reset: "Reset" };
const DANGER = { wifi_reset: "Reset Wi-Fi on the sensor to the network built into its firmware?" };
const LAYOUT_KEYS = new Set([
  ...Object.values(SHEETS).flatMap((s) => [s.entity, ...s.info, ...s.settings]),
  ...GROUPS.flatMap((g) => [...g.rows, ...g.more]),
  ...SECTIONS.flatMap((s) => s.keys || []),
  "firmware", "ota_status", "ota_progress", "firmware_version", "ota_flash", "ota_build_flash", "ota_cancel",
]);

class MoveSensorBlePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cards = [];
    this._tab = this._tabFromHash();
    this._sheet = null;
    this._more = {};
    this._open = { bluetooth: false };
    this._onHash = () => { const t = this._tabFromHash(); if (t !== this._tab) { this._tab = t; this._render(); } };
    this._onKey = (e) => { if (e.key === "Escape" && this._sheet) this._closeSheet(); };
    this._onVV = () => this._keyboard();
  }

  connectedCallback() {
    window.addEventListener("hashchange", this._onHash);
    window.addEventListener("keydown", this._onKey);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", this._onVV);
      window.visualViewport.addEventListener("scroll", this._onVV);
    }
  }

  disconnectedCallback() {
    window.removeEventListener("hashchange", this._onHash);
    window.removeEventListener("keydown", this._onKey);
    if (window.visualViewport) {
      window.visualViewport.removeEventListener("resize", this._onVV);
      window.visualViewport.removeEventListener("scroll", this._onVV);
    }
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this._config) { this._render(); return; }
    for (const c of this._cards) c.hass = hass;
    if (this._menu) this._menu.hass = hass;
    this._refreshLive();
  }

  set panel(panel) {
    const cfg = (panel && panel.config) || {};
    const changed = JSON.stringify(cfg) !== JSON.stringify(this._config || null);
    this._config = cfg;
    if (changed && this._hass) this._render();
  }

  set narrow(v) {
    const changed = !!v !== !!this._narrow;
    this._narrow = v;
    if (this._menu) this._menu.narrow = v;
    if (changed && this._hass && this._config && !this._sheet) this._render();   // phone layout of the tiles
  }

  _tabFromHash() {
    const h = (location.hash || "").replace("#", "");
    return TABS.some((t) => t.id === h) ? h : "overview";
  }

  get _dev() {
    return ((this._config && this._config.devices) || [])[0];
  }

  _id(key) {
    const d = this._dev;
    return d && d.ids ? d.ids[key] : undefined;
  }

  _st(key) {
    const id = this._id(key);
    return id && this._hass ? this._hass.states[id] : undefined;
  }

  // «Move Sensor BLE Keep time» -> «Keep time»
  _name(entityId) {
    const st = this._hass && this._hass.states[entityId];
    const fn = (st && st.attributes && st.attributes.friendly_name) || entityId;
    for (const p of (this._dev && this._dev.strip) || []) {
      if (p && fn.startsWith(p + " ") && fn.length > p.length + 1) return fn.slice(p.length + 1);
    }
    return fn;
  }

  _card(cfg) {
    const el = this._helpers.createCardElement(cfg);
    el.hass = this._hass;
    this._cards.push(el);
    return el;
  }

  _wide(el) {
    el.classList.add("wide");
    return el;
  }

  _el(tag, attrs = {}, children = []) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(children)) if (c) el.appendChild(c);
    return el;
  }

  // ---------- card configs ----------
  _settingCard(key) {
    const id = this._id(key);
    if (!id) return null;
    const domain = id.split(".")[0];
    const name = this._name(id);
    if (domain === "switch") {
      return { type: "tile", entity: id, name, tap_action: { action: "toggle" }, icon_tap_action: { action: "toggle" },
               features: [{ type: "toggle" }], features_position: name.length <= 14 ? "inline" : "bottom" };
    }
    if (domain === "number") {
      const mode = CONTROL[key] || "buttons";
      if (mode === "box") return { type: "entities", entities: [{ entity: id, name }] };
      return { type: "tile", entity: id, name, hold_action: { action: "more-info" },
               features: [{ type: "numeric-input", style: mode }],
               // control beside the name only when the name fits; long names keep it below (no cut-off text)
               features_position: mode === "buttons" && name.length <= 14 ? "inline" : "bottom" };
    }
    return this._infoCard(key);
  }

  _infoCard(key) {
    const id = this._id(key);
    if (!id) return null;
    const domain = id.split(".")[0];
    const name = this._name(id);
    if (domain === "button") {
      const action = { action: "perform-action", perform_action: "button.press", target: { entity_id: id } };
      if (DANGER[key]) action.confirmation = { text: DANGER[key] };
      // a button row says what happens («Blink LED») — a tile would show the last press time instead
      return { type: "entities", entities: [{ type: "button", entity: id, name: SHORT[key] || name, action_name: ACTION_NAMES[key] || "Press",
                                              tap_action: action }] };
    }
    if (domain === "number" || domain === "switch") return this._settingCard(key);
    return { type: "tile", entity: id, name, tap_action: { action: "more-info" } };
  }

  // trend graph to the card edges: the tile keeps a 12 px padding around its features and has no
  // CSS variable for it, so add one rule inside the tile itself (only for tiles with a graph)
  _edgeToEdge(card) {
    const apply = (tries = 0) => {
      const sr = card.shadowRoot;
      if (!sr || !sr.querySelector("hui-card-features")) { if (tries < 50) setTimeout(() => apply(tries + 1), 100); return; }
      if (sr.querySelector("style[data-msb-edge]")) return;
      const st = document.createElement("style");
      st.setAttribute("data-msb-edge", "");
      // the trend graph closes its fill 3.5 of 42 units above its bottom (hui-graph-base),
      // which leaves a strip under the fill: shift the graph down 4px, ha-card clips it
      st.textContent = "hui-card-features { padding: 0 !important; transform: translateY(4px); } ha-card { overflow: hidden; }";
      sr.appendChild(st);
    };
    Promise.resolve(card.updateComplete).then(() => apply());
  }

  // ha-expansion-panel tints its header on any focus (.top.focused), so after a mouse click the header
  // stays highlighted; keep the tint for keyboard focus only. Adopted sheets win ties, so the selector is stronger
  _noClickTint(panel) {
    const apply = (tries = 0) => {
      const sr = panel.shadowRoot;
      if (!sr || !sr.querySelector(".top")) { if (tries < 50) setTimeout(() => apply(tries + 1), 100); return; }
      if (sr.querySelector("style[data-msb-tint]")) return;
      const st = document.createElement("style");
      st.setAttribute("data-msb-tint", "");
      st.textContent = ":host div.top.focused { background: none; } :host div.top.focused:has(#summary:focus-visible) { background: var(--input-fill-color); }";
      sr.appendChild(st);
    };
    Promise.resolve(panel.updateComplete).then(() => apply());
  }

  // ---------- render ----------
  async _render() {
    if (this._rendering) { this._again = true; return; }
    this._rendering = true;
    try {
      this._helpers = this._helpers || (await window.loadCardHelpers());
      this._cards = [];
      const root = this.shadowRoot;
      root.innerHTML = "";
      root.appendChild(this._el("style", { text: STYLE }));
      const head = this._el("div", { class: "toolbar" });
      this._menu = document.createElement("ha-menu-button");
      this._menu.hass = this._hass;
      this._menu.narrow = this._narrow;
      head.appendChild(this._menu);
      head.appendChild(this._el("div", { class: "title", text: "Move Sensor BLE" }));
      root.appendChild(head);

      const tabs = this._el("div", { class: "tabs", role: "tablist", "aria-label": "Panel sections" });
      for (const t of TABS) {
        tabs.appendChild(this._el("button", {
          role: "tab", "aria-selected": t.id === this._tab ? "true" : "false", class: t.id === this._tab ? "tab on" : "tab",
          text: t.label, onclick: () => { location.hash = t.id; },
        }));
      }
      root.appendChild(tabs);

      const main = this._el("div", { class: "content" });
      root.appendChild(main);
      if (!this._dev) {
        main.appendChild(this._el("p", { class: "note", text: "No Move Sensor BLE sensors are set up." }));
        return;
      }
      if (this._tab === "overview") this._overview(main);
      else if (this._tab === "settings") this._settings(main);
      else this._advanced(main);
      if (this._sheet) this._renderSheet();
      this._refreshLive();
    } finally {
      this._rendering = false;
      if (this._again) { this._again = false; this._render(); }
    }
  }

  _overview(main) {
    const badges = ["link", "signal_level", "last_seen", "settings_status"]
      .map((k) => this._id(k)).filter(Boolean)
      .map((e) => ({ type: "entity", entity: e, show_name: true, show_state: true, show_icon: true, name: this._name(e) }));
    // on a phone the name is already in the toolbar — keep the row for the badges
    main.appendChild(this._card(this._narrow ? { type: "heading", heading: "", badges }
      : { type: "heading", heading: this._dev.title, heading_style: "title", icon: "mdi:motion-sensor", badges }));
    this._attention = this._el("div", { class: "attention", role: "status", hidden: true });
    this._attKey = null;   // new box: fill it even if the items did not change
    main.appendChild(this._attention);

    const grid = this._el("div", { class: "tiles" });
    for (const [key, sh] of Object.entries(SHEETS)) {
      const id = this._id(sh.entity);
      if (!id) continue;
      const open = { action: "fire-dom-event", msb_sheet: key };
      const cfg = { type: "tile", entity: id, name: sh.title, tap_action: open, icon_tap_action: open,
                    hold_action: { action: "more-info" } };
      if (this._narrow) { cfg.vertical = true; cfg.content_layout = "vertical"; }
      // "Detected · 2 hours ago" shows a stuck PIR at a glance; on a phone the motion tile takes the whole
      // row so the time is not cut off by the ellipsis of a half-width tile
      if (key === "motion") cfg.state_content = ["state", "last_changed"];
      else cfg.features = [{ type: "trend-graph", hours_to_show: 24 }];
      const wrap = this._el("div", { class: key === "motion" ? "tilewrap full" : "tilewrap" });
      const tile = this._card(cfg);
      if (cfg.features) this._edgeToEdge(tile);
      wrap.appendChild(tile);
      wrap.appendChild(this._el("button", {
        class: "cog", "aria-label": `${sh.title}: history and settings`, title: "History and settings",
        onclick: () => this._openSheet(key),
      }, [this._el("ha-icon", { icon: "mdi:cog-outline" })]));
      grid.appendChild(wrap);
    }
    grid.addEventListener("ll-custom", (e) => {
      const k = e.detail && e.detail.msb_sheet;
      if (k) { e.stopPropagation(); this._openSheet(k); }
    });
    main.appendChild(grid);
    main.appendChild(this._el("p", { class: "note",
      text: "Tap a card for its history and settings. Press and hold for the standard Home Assistant dialog." }));
  }

  _settings(main) {
    this._sync = this._el("div", { class: "sync", role: "status" });
    main.appendChild(this._sync);
    const cols = this._el("div", { class: "groups" });
    for (const g of GROUPS) {
      const col = this._el("section", { class: "group", "aria-label": g.title });
      col.appendChild(this._el("h2", { text: g.title }));
      for (const k of g.rows) { const c = this._settingCard(k); if (c) col.appendChild(this._card(c)); }
      const more = g.more.filter((k) => this._id(k));
      if (more.length) {
        const open = !!this._more[g.title];
        col.appendChild(this._el("button", {
          class: "textbtn", "aria-expanded": open ? "true" : "false", text: open ? "Show less" : "Show more",
          onclick: () => { this._more[g.title] = !open; this._render(); },
        }));
        if (open) for (const k of more) col.appendChild(this._card(this._settingCard(k)));
      }
      cols.appendChild(col);
    }
    main.appendChild(cols);
    main.appendChild(this._el("p", { class: "note",
      text: "Changes reach the sensor over Bluetooth on its next wake, or right away while Wi-Fi service mode is on." }));
  }

  _advanced(main) {
    main.appendChild(this._el("p", { class: "note", text: "For tuning and troubleshooting. The sensor works fine with the defaults." }));
    const ids = (this._dev && this._dev.ids) || {};
    const extra = Object.keys(ids).filter((k) => !LAYOUT_KEYS.has(k)).sort();
    for (const sec of SECTIONS) {
      const panel = document.createElement("ha-expansion-panel");
      panel.outlined = true;
      panel.header = sec.title;
      panel.leftChevron = false;
      panel.expanded = !!this._open[sec.id];
      panel.setAttribute("header", sec.title);
      const sum = this._summary(sec.id);
      if (sum) { panel.secondary = sum; panel.setAttribute("secondary", sum); }
      panel.addEventListener("expanded-changed", (e) => { this._open[sec.id] = e.detail.expanded; });
      const icon = this._el("ha-icon", { slot: "leading-icon", icon: sec.icon });
      panel.appendChild(icon);
      const body = this._el("div", { class: "secgrid" });
      if (sec.id === "firmware") {
        const rows = (this._dev.firmware || []).map((r) => (typeof r === "string" ? { entity: r, name: "Firmware" } : r));
        if (rows.length) body.appendChild(this._wide(this._card({ type: "entities", entities: rows, state_color: true })));
      } else if (sec.id === "wifi") {
        this._wifiBody = body;
        this._fillWifi();
      } else {
        const keys = [...sec.keys, ...(sec.id === "diagnostics" ? extra : [])];
        for (const k of keys) { const c = this._infoCard(k); if (c) body.appendChild(this._card(c)); }
        if (sec.id === "diagnostics") {
          body.appendChild(this._card({ type: "entities", entities: [{
            type: "button", name: "Configure", icon: "mdi:cog", action_name: "Open",
            tap_action: { action: "navigate", navigation_path: this._dev.configure } }] }));
        }
      }
      panel.appendChild(body);
      this._noClickTint(panel);
      this._secPanels = this._secPanels || {};
      this._secPanels[sec.id] = panel;
      main.appendChild(panel);
    }
  }

  _summary(id) {
    const s = (k) => this._st(k);
    const val = (k) => { const x = s(k); return x && !["unknown", "unavailable"].includes(x.state) ? x : null; };
    const fmt = (k) => { const x = val(k); return x ? this._hass.formatEntityState(x) : null; };
    if (id === "firmware") {
      const fw = val("firmware");
      if (!fw) return "";
      const cur = fw.attributes.installed_version || "";
      return fw.state === "on" ? `${cur} installed · ${fw.attributes.latest_version || ""} available` : `${cur} · up to date`;
    }
    if (id === "bluetooth") {
      const seen = val("last_seen");
      const mins = seen && !isNaN(Date.parse(seen.state)) ? Math.round((Date.now() - Date.parse(seen.state)) / 60000) : null;
      return [fmt("signal_level") && `Signal ${fmt("signal_level")}`,
              mins !== null && (mins < 1 ? "seen just now" : `seen ${mins} min ago`)].filter(Boolean).join(" · ");
    }
    if (id === "wifi") return this._wifiState().title;
    if (id === "calibration") return fmt("setting_voltage_factor") ? `Voltage factor ${fmt("setting_voltage_factor")}` : "";
    if (id === "diagnostics") return [fmt("last_reset_reason") && `Last reset: ${fmt("last_reset_reason")}`, fmt("voltage")].filter(Boolean).join(" · ");
    return "";
  }

  // Wi-Fi (service mode): "wifi" — Home Assistant reaches it; "away" — on, on a router Home Assistant
  // can't reach (another network); "own_ap" — on, no router (only its own access point); "off".
  _wifiState() {
    const link = this._st("link");
    const svc = link && link.state === "wifi" ? "wifi" : ((link && link.attributes && link.attributes.service) || "off");
    const titles = {
      wifi: "On",
      away: "On — on another network, Home Assistant can't reach it",
      own_ap: "On — not connected to a router (only its own Wi-Fi)",
      off: "Off — the sensor talks over Bluetooth" };
    const notes = {
      away: "The Wi-Fi switch is on, but the sensor is on a network Home Assistant can't reach (for example a guest network). Join its own Wi-Fi from a phone and pick the home network in the Wi-Fi window.",
      own_ap: "The Wi-Fi switch is on, but the sensor is not connected to any router. Join its own Wi-Fi from a phone and pick a network in the Wi-Fi window.",
      off: "Wi-Fi is off and the sensor talks over Bluetooth. Turn on the Wi-Fi switch on the sensor (service mode) to change its network or update it faster." };
    return { svc, title: titles[svc] || titles.off, note: notes[svc] || notes.off };
  }

  _fillWifi() {
    const body = this._wifiBody;
    if (!body) return;
    const ws = this._wifiState();
    const on = ws.svc === "wifi";
    this._wifiShown = ws.svc;
    body.innerHTML = "";
    // 2026-11-19: только сущности с живым состоянием (прошивка 1.7.0 убрала часть Wi-Fi-сущностей;
    // в реестре они остаются — в карточке было бы «Объект не найден»)
    const wifi = ((this._dev && this._dev.wifi) || []).filter((e) => {
      const s = this._hass && this._hass.states[e];
      return s && !(s.attributes && s.attributes.restored);
    });
    if (on && wifi.length) {
      body.appendChild(this._wide(this._card({ type: "entities",
        entities: wifi.map((e) => ({ entity: e, name: this._name(e) })) })));
    } else {
      body.appendChild(this._el("p", { class: "note", text: wifi.length
        ? ws.note
        : "No ESPHome device found for this sensor." }));
    }
  }

  // ---------- live bits (no re-render) ----------
  _refreshLive() {
    if (this._attention && this._attention.isConnected) this._fillAttention();
    if (this._sync && this._sync.isConnected) {
      const st = this._st("settings_status");
      const s = st ? st.state : "";
      const txt = { in_sync: "All settings are applied.", waiting: "Saved. The sensor picks the changes up on its next wake (motion wakes it).",
                    not_read: "The sensor's settings haven't been read yet.", error: "The sensor didn't accept the last change. Try again." }[s] || "";
      this._sync.textContent = txt;
      this._sync.className = "sync " + (s || "none");
    }
    // Section headers (firmware version, Bluetooth "seen …", …) follow the entities without a re-render:
    // after a flash the header kept the old version until the page was reopened.
    for (const [id, p] of Object.entries(this._secPanels || {})) {
      if (id === "wifi" || !p.isConnected) continue;
      const sum = this._summary(id) || "";
      if (p.secondary !== sum) { p.secondary = sum; p.setAttribute("secondary", sum); }
    }
    if (this._wifiBody && this._wifiBody.isConnected) {
      const ws = this._wifiState();
      if (ws.svc !== this._wifiShown) {
        this._fillWifi();
        // заголовок раздела тоже (раньше обновлялся только при полной перерисовке)
        const p = this._secPanels && this._secPanels.wifi;
        if (p) { p.secondary = ws.title; p.setAttribute("secondary", ws.title); }
      }
    }
  }

  _fillAttention() {
    const items = [];
    const bat = this._st("battery");
    const low = this._st("setting_battery_low");
    const lowLevel = low && !isNaN(parseFloat(low.state)) ? parseFloat(low.state) : 10;
    if (bat && !isNaN(parseFloat(bat.state)) && parseFloat(bat.state) <= lowLevel) {
      const days = this._st("battery_days_left");
      const d = days && !isNaN(parseFloat(days.state)) ? ` About ${Math.round(parseFloat(days.state))} days left.` : "";
      items.push({ icon: "mdi:battery-alert-variant-outline", text: `Battery is at ${Math.round(parseFloat(bat.state))} %. Charge it soon.${d}` });
    }
    // same rule as the integration's Repairs notice (ota.py silent_limit_min)
    const seen = this._st("last_seen");
    const ciSt = this._st("setting_climate_interval");
    const ci = ciSt && !isNaN(parseFloat(ciSt.state)) ? parseFloat(ciSt.state) : 5;
    const opt = (this._dev && this._dev.silent_after) || "auto";
    const limit = ci <= 0 ? null : (/^\d+$/.test(opt) ? Math.max(parseFloat(opt), 2 * ci) : Math.max(15, 3 * ci));
    let silent = false;
    if (limit !== null && seen && seen.state && !isNaN(Date.parse(seen.state))) {
      const mins = (Date.now() - Date.parse(seen.state)) / 60000;
      silent = mins > limit;
      if (silent) items.push({ icon: "mdi:bluetooth-off", text: `No data from the sensor for ${Math.round(mins)} minutes. Check the battery and that a Bluetooth proxy is in range.` });
    }
    const stuck = this._st("pir_stuck");
    if (!silent && stuck && stuck.state === "on") items.push({ icon: "mdi:motion-sensor-off", text: "Motion has been “detected” for too long. The motion sensor may be stuck." });
    const fw = this._st("firmware");
    if (fw && fw.state === "on") {
      items.push({ icon: "mdi:package-up", text: `Firmware ${fw.attributes.latest_version || ""} is available.`.replace("  ", " "),
                   action: "Open firmware", go: () => { this._open.firmware = true; location.hash = "advanced"; } });
    }
    const st = this._st("settings_status");
    if (st && st.state === "error") items.push({ icon: "mdi:cog-off-outline", text: "The sensor didn't accept the last settings change." });
    const key = JSON.stringify(items.map((i) => i.text));
    if (key === this._attKey) return;
    this._attKey = key;
    const box = this._attention;
    box.innerHTML = "";
    if (!items.length) { box.hidden = true; return; }
    box.hidden = false;
    box.appendChild(this._el("div", { class: "atthead", text: "Needs attention" }));
    for (const it of items) {
      box.appendChild(this._el("div", { class: "attrow" }, [
        this._el("ha-icon", { icon: it.icon }),
        this._el("span", { text: it.text }),
        it.action ? this._el("button", { class: "textbtn", text: it.action, onclick: it.go }) : null,
      ]));
    }
  }

  // ---------- sheet: our dialog (bottom sheet on phones) ----------
  _openSheet(key) {
    this._lastFocus = this.shadowRoot.activeElement || document.activeElement;
    this._sheet = key;
    this._renderSheet();
  }

  _closeSheet() {
    this._sheet = null;
    if (this._sheetEl) this._sheetEl.remove();
    this._sheetEl = null;
    this._cards = this._cards.filter((c) => c.isConnected);
    if (this._lastFocus && this._lastFocus.focus) this._lastFocus.focus();
  }

  _moreInfo(entityId) {
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  _renderSheet() {
    if (this._sheetEl) this._sheetEl.remove();
    const sh = SHEETS[this._sheet];
    const id = sh && this._id(sh.entity);
    if (!id) { this._sheet = null; return; }
    const scrim = this._el("div", { class: "scrim", onclick: (e) => { if (e.target === scrim) this._closeSheet(); } });
    const dlg = this._el("div", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": sh.title });
    dlg.appendChild(this._el("div", { class: "grip", "aria-hidden": "true" }));
    const close = this._el("button", { class: "iconbtn", "aria-label": "Close", onclick: () => this._closeSheet() },
                           [this._el("ha-icon", { icon: "mdi:close" })]);
    dlg.appendChild(this._el("div", { class: "sheethead" }, [
      close,
      this._el("div", { class: "sheettitle", text: sh.title }),
      this._el("button", { class: "iconbtn", "aria-label": "Home Assistant history and statistics", title: "Home Assistant history and statistics",
                           onclick: () => this._moreInfo(id) }, [this._el("ha-icon", { icon: "mdi:chart-line" })]),
      this._el("button", { class: "textbtn", text: "All settings", onclick: () => { this._closeSheet(); location.hash = "settings"; } }),
    ]));
    const body = this._el("div", { class: "sheetbody" });
    body.appendChild(this._card({ type: "tile", entity: id, name: sh.title, tap_action: { action: "more-info" },
                                  state_content: ["state", "last_changed"] }));
    body.appendChild(this._card({ type: "history-graph", hours_to_show: 24, entities: [{ entity: id, name: sh.title }] }));
    const info = sh.info.map((k) => this._id(k)).filter(Boolean);
    if (info.length) body.appendChild(this._card({ type: "glance", show_name: true, show_state: true,
      entities: sh.info.filter((k) => this._id(k)).map((k) => ({ entity: this._id(k), name: SHORT[k] || this._name(this._id(k)) })) }));
    body.appendChild(this._el("h3", { text: "Settings" }));
    for (const k of sh.settings) { const c = this._settingCard(k); if (c) body.appendChild(this._card(c)); }
    this._sheetBody = body;
    dlg.appendChild(body);
    scrim.appendChild(dlg);
    this.shadowRoot.appendChild(scrim);
    this._sheetEl = scrim;
    this._keyboard();
    setTimeout(() => close.focus(), 0);
    // keep a focused field above the on-screen keyboard
    body.addEventListener("focusin", (e) => {
      const t = e.composedPath()[0];
      setTimeout(() => { try { t.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) { /* old browsers */ } }, 300);
    });
  }

  _keyboard() {
    if (!this._sheetEl || !window.visualViewport) return;
    const vv = window.visualViewport;
    const kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    this._sheetEl.style.paddingBottom = kb > 80 ? `${kb}px` : "";
  }
}

const STYLE = `
  :host { display: block; background: var(--primary-background-color); min-height: 100vh; color: var(--primary-text-color);
          font-family: var(--ha-font-family-body, Roboto, sans-serif); }
  .toolbar { display: flex; align-items: center; gap: var(--ha-space-3, 12px); height: var(--header-height, 56px); padding: 0 var(--ha-space-3, 12px);
             background: var(--app-header-background-color); color: var(--app-header-text-color, var(--primary-text-color));
             border-bottom: var(--app-header-border-bottom, none); font-size: 20px; }
  .tabs { display: flex; gap: var(--ha-space-1, 4px); padding: 0 var(--ha-space-4, 16px);
          background: var(--app-header-background-color); border-bottom: 1px solid var(--divider-color); }
  .tab { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--secondary-text-color); font: inherit;
         font-size: 14px; font-weight: 500; padding: 0 var(--ha-space-4, 16px); min-height: 48px; cursor: pointer; }
  .tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .tab:focus-visible, button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  .content { max-width: 1180px; margin: 0 auto; padding: var(--ha-space-4, 16px); display: flex; flex-direction: column; gap: var(--ha-space-4, 16px); }
  .note { color: var(--secondary-text-color); font-size: 13px; margin: 0; }
  .tiles { display: grid; gap: var(--ha-space-3, 12px); grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .tilewrap { position: relative; }
  .cog { position: absolute; top: 8px; inset-inline-end: 8px; width: 40px; height: 40px; border-radius: 20px; border: 0;
         background: transparent; color: var(--secondary-text-color); cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .cog:hover { background: var(--secondary-background-color); color: var(--primary-color); }
  .attention[hidden] { display: none; }
  .attention { border-radius: var(--ha-card-border-radius, 12px); padding: var(--ha-space-3, 12px) var(--ha-space-4, 16px);
               background: rgba(var(--rgb-warning-color, 255, 166, 0), 0.12); border: 1px solid rgba(var(--rgb-warning-color, 255, 166, 0), 0.4);
               display: flex; flex-direction: column; gap: var(--ha-space-2, 8px); }
  .atthead { font-weight: 500; color: var(--warning-color); }
  .attrow { display: flex; align-items: center; gap: var(--ha-space-3, 12px); flex-wrap: wrap; }
  .attrow ha-icon { color: var(--warning-color); flex: none; }
  .attrow span { flex: 1 1 240px; }
  .groups { display: grid; gap: var(--ha-space-6, 24px) var(--ha-space-4, 16px); grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); align-items: start; }
  .group { display: flex; flex-direction: column; gap: var(--ha-space-2, 8px); }
  .group h2, .sheetbody h3 { font-size: 18px; font-weight: 500; margin: var(--ha-space-1, 4px) var(--ha-space-1, 4px) 0; }
  .textbtn { align-self: flex-start; background: none; border: 0; color: var(--primary-color); font: inherit; font-weight: 500;
             min-height: 48px; padding: 0 var(--ha-space-2, 8px); cursor: pointer; border-radius: 24px; }
  .sync { color: var(--secondary-text-color); }
  .sync.waiting { color: var(--info-color, var(--primary-color)); }
  .sync.error { color: var(--error-color); }
  /* content padding lives on our grid: on the panel itself it stays visible (an empty 16 px strip) while collapsed */
  ha-expansion-panel { --expansion-panel-summary-padding: var(--ha-space-2, 8px) var(--ha-space-4, 16px); --expansion-panel-content-padding: 0;
                       background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); }
  ha-expansion-panel ha-icon[slot="leading-icon"] { color: var(--secondary-text-color); margin-inline-end: var(--ha-space-2, 8px); }
  .secgrid { padding: var(--ha-space-1, 4px) var(--ha-space-4, 16px) var(--ha-space-4, 16px); display: grid; gap: var(--ha-space-2, 8px); grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); align-items: start; }
  .secgrid > .wide, .secgrid > p { grid-column: 1 / -1; }
  /* a full-width list (firmware, Wi-Fi) is the section's own content, not a card inside a card: no border or
     background, and its 16 px padding replaces ours so the row icons line up with the section icon */
  .secgrid > .wide { --ha-card-border-width: 0; --ha-card-background: transparent; --ha-card-box-shadow: none;
                     margin: 0 calc(-1 * var(--ha-space-4, 16px)); }
  /* a note in a section lines up with the section title (icon 24 px + its 8 px gap), not with the icon */
  .secgrid > p.note { padding-inline-start: calc(24px + var(--ha-space-2, 8px)); }
  .scrim { position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5); display: flex; align-items: center; justify-content: center;
           padding: var(--ha-space-6, 24px); box-sizing: border-box; animation: fade var(--ha-animation-base-duration, 350ms) ease-out; }
  .sheet { width: 100%; max-width: 560px; max-height: calc(100% - 48px); overflow: auto; background: var(--card-background-color);
           border-radius: var(--ha-dialog-border-radius, 28px); box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
           animation: up var(--ha-animation-base-duration, 350ms) ease-out; }
  .grip { display: none; width: 36px; height: 4px; border-radius: 2px; background: var(--divider-color); margin: 10px auto 0; }
  .sheethead { display: flex; align-items: center; gap: var(--ha-space-1, 4px); padding: var(--ha-space-3, 12px) var(--ha-space-3, 12px) 0; position: sticky; top: 0;
               background: var(--card-background-color); z-index: 1; }
  .sheettitle { flex: 1 1 auto; font-size: 22px; }
  .iconbtn { width: 48px; height: 48px; border-radius: 24px; border: 0; background: none; color: var(--primary-text-color); cursor: pointer;
             display: flex; align-items: center; justify-content: center; flex: none; }
  .iconbtn:hover { background: var(--secondary-background-color); }
  .sheetbody { padding: var(--ha-space-2, 8px) var(--ha-space-4, 16px) var(--ha-space-6, 24px); display: flex; flex-direction: column; gap: var(--ha-space-3, 12px); }
  @keyframes up { from { transform: translateY(48px); opacity: 0; } to { transform: none; opacity: 1; } }
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
  @media (max-width: 600px) {
    .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--ha-space-2, 8px); }
    .tilewrap.full { grid-column: 1 / -1; }
    .tab { flex: 1 1 0; padding: 0 var(--ha-space-2, 8px); }
    .tabs { padding: 0; }
    .content { padding: var(--ha-space-3, 12px); }
    .scrim { align-items: flex-end; padding: 0; }
    .sheet { max-width: none; max-height: 90%; border-radius: var(--ha-dialog-border-radius, 28px) var(--ha-dialog-border-radius, 28px) 0 0; }
    .grip { display: block; }
  }
  @media (prefers-reduced-motion: reduce) { .scrim, .sheet { animation: none; } }
`;

if (!customElements.get("move-sensor-ble-panel")) {
  customElements.define("move-sensor-ble-panel", MoveSensorBlePanel);
}
