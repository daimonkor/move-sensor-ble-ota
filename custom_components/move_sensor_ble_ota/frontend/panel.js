// Move Sensor BLE — панель интеграции move_sensor_ble_ota (v1.15).
// Одна страница: разделы Sensor / Settings / BLE / Wi-Fi / Firmware — сеткой стандартных карточек HA
// (entities). Набор сущностей приходит из интеграции (panel.config).
class MoveSensorBlePanel extends HTMLElement {
  constructor() {
    super();
    this._cards = [];
    this._built = false;
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built && this._config) this._build();
    for (const c of this._cards) c.hass = hass;
    if (this._menu) this._menu.hass = hass;
  }

  set panel(panel) {
    const cfg = (panel && panel.config) || {};
    const changed = JSON.stringify(cfg) !== JSON.stringify(this._config || null);
    this._config = cfg;
    if (changed) {
      this._built = false;
      if (this._hass) this._build();
    }
  }

  set narrow(v) {
    this._narrow = v;
    if (this._menu) this._menu.narrow = v;
  }

  // строка без имени устройства: «Move Sensor BLE Keep time» -> «Keep time»
  _row(e, strip) {
    if (typeof e !== "string") return e;
    const st = this._hass && this._hass.states[e];
    const fn = st && st.attributes && st.attributes.friendly_name;
    if (!fn) return e;
    for (const p of strip) {
      if (p && fn.startsWith(p + " ") && fn.length > p.length + 1) {
        return { entity: e, name: fn.slice(p.length + 1) };
      }
    }
    return e;
  }

  async _build() {
    if (this._building) return;
    this._building = true;
    try {
      const helpers = await window.loadCardHelpers();
      const root = this.shadowRoot;
      root.innerHTML = "";
      const style = document.createElement("style");
      style.textContent = `
        :host { display:block; background: var(--primary-background-color); min-height: 100vh; }
        header { display:flex; align-items:center; gap:12px; height:56px; padding:0 16px;
                 background: var(--app-header-background-color, var(--primary-color));
                 color: var(--app-header-text-color, var(--text-primary-color)); font-size:20px; }
        .dev { padding: 16px; }
        .dev h2 { margin: 8px 4px 12px; font-weight: 500; color: var(--primary-text-color); }
        .grid { display:grid; gap:16px; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); align-items:start; }
        .note { padding: 6px 16px 0; color: var(--secondary-text-color); font-size: 13px; }
      `;
      root.appendChild(style);
      const head = document.createElement("header");
      this._menu = document.createElement("ha-menu-button");
      this._menu.hass = this._hass;
      this._menu.narrow = this._narrow;
      head.appendChild(this._menu);
      const t = document.createElement("div");
      t.textContent = "Move Sensor BLE";
      head.appendChild(t);
      root.appendChild(head);
      this._cards = [];
      for (const dev of this._config.devices || []) {
        const wrap = document.createElement("div");
        wrap.className = "dev";
        const h = document.createElement("h2");
        h.textContent = dev.device_id ? `${dev.title} · ${dev.device_id}` : dev.title;
        wrap.appendChild(h);
        const grid = document.createElement("div");
        grid.className = "grid";
        for (const sec of dev.sections || []) {
          const holder = document.createElement("div");
          let cfg = {
            type: "entities",
            title: sec.title,
            state_color: true,
            show_header_toggle: false,
            entities: sec.entities && sec.entities.length
              ? sec.entities.map((e) => this._row(e, dev.strip || []))
              : [{ type: "section", label: "—" }],
          };
          if (sec.show_if) {
            // карточка видна только при условии (Wi-Fi — когда Link = wifi), иначе — подсказка
            const cond = [{ condition: "state", entity: sec.show_if.entity, state: sec.show_if.state }];
            const off = [{ condition: "state", entity: sec.show_if.entity, state_not: sec.show_if.state }];
            const both = helpers.createCardElement({
              type: "vertical-stack",
              cards: [
                { type: "conditional", conditions: cond, card: cfg },
                { type: "conditional", conditions: off,
                  card: { type: "markdown", title: sec.title, content: sec.hidden_note || "" } },
              ],
            });
            both.hass = this._hass;
            this._cards.push(both);
            holder.appendChild(both);
            grid.appendChild(holder);
            continue;
          }
          const card = helpers.createCardElement(cfg);
          card.hass = this._hass;
          this._cards.push(card);
          holder.appendChild(card);
          if (sec.note) {
            const n = document.createElement("div");
            n.className = "note";
            n.textContent = sec.note;
            holder.appendChild(n);
          }
          grid.appendChild(holder);
        }
        wrap.appendChild(grid);
        root.appendChild(wrap);
      }
      this._built = true;
    } finally {
      this._building = false;
    }
  }
}

if (!customElements.get("move-sensor-ble-panel")) {
  customElements.define("move-sensor-ble-panel", MoveSensorBlePanel);
}
