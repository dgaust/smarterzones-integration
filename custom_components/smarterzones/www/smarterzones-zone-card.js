/**
 * SmarterZones Zone Card
 *
 * A complete dashboard card for a single SmarterZones zone. Pick the zone's
 * device and the card discovers its controls automatically: target temperature,
 * smart-control toggle, open/closed status, and (optionally) the offset settings.
 *
 * Dependency-free custom element (no external Lit/CDN) so it works across HA
 * versions without a build step.
 */

const CARD_VERSION = "1.16.2";

function fireEvent(node, type, detail) {
  const event = new Event(type, { bubbles: true, composed: true, cancelable: false });
  event.detail = detail || {};
  node.dispatchEvent(event);
  return event;
}

class SmarterZonesZoneCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("smarterzones-zone-card-editor");
  }

  static getStubConfig(hass) {
    let device = "";
    try {
      const entities = (hass && hass.entities) || {};
      const byDevice = {};
      for (const e of Object.values(entities)) {
        if (e.platform !== "smarterzones" || !e.device_id) continue;
        (byDevice[e.device_id] = byDevice[e.device_id] || []).push(e);
      }
      for (const [did, ents] of Object.entries(byDevice)) {
        const hasTarget = ents.some(
          (e) => e.entity_id.startsWith("number.") && e.entity_category !== "config"
        );
        const hasSwitch = ents.some((e) => e.entity_id.startsWith("switch."));
        if (hasTarget && hasSwitch) {
          device = did;
          break;
        }
      }
    } catch (err) {
      /* ignore */
    }
    return {
      type: "custom:smarterzones-zone-card",
      device,
      status_display: "full",
    };
  }

  // Resolve the single status_display setting ("full" | "compact"), migrating the
  // old show_status/compact_status booleans (and the removed "hidden" value) for
  // existing dashboards.
  _statusMode(config) {
    let mode = config.status_display;
    if (!mode) {
      mode = config.show_status === false || config.compact_status ? "compact" : "full";
    }
    return mode === "hidden" ? "compact" : mode;
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this._config = Object.assign({}, config);
    this._config.status_display = this._statusMode(this._config);
    this._sig = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    try {
      this._discover();
      const mode = this._ids.target ? "zone" : "empty";
      const sig = [
        mode,
        this._config.device || "",
        this._config.status_display || "full",
        this._config.show_deviation !== false ? 1 : 0,
      ].join("|");
      if (this._sig !== sig) {
        this._build();
        this._sig = sig;
      }
      this._update();
    } catch (err) {
      this._renderError(err);
    }
  }

  disconnectedCallback() {
    // Don't leave an orphaned popup if the card is removed while it's open.
    this._closeDetails();
  }

  _renderError(err) {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const msg = (err && err.message) || String(err);
    this.shadowRoot.innerHTML =
      `<style>${this._styles()}</style>` +
      `<ha-card><div class="empty"><ha-icon icon="mdi:alert-circle-outline"></ha-icon>` +
      `<div>Zone card error: ${msg}</div></div></ha-card>`;
    // eslint-disable-next-line no-console
    console.error("smarterzones-zone-card:", err);
  }

  getCardSize() {
    let size = 3;
    if (this._config && this._config.status_display === "full") size += 2;
    return size;
  }

  _discover() {
    const hass = this._hass;
    const deviceId = this._config.device;
    this._ids = {
      smart: null, openZone: null, target: null, zoneOpen: null, conditions: null,
      projected: null, coolRange: null, heatRange: null,
      coolUpper: null, coolLower: null, heatUpper: null, heatLower: null,
      currentTemp: null, humidity: null,
    };
    this._deviceName = "Zone";
    if (!deviceId || !hass) return;

    const entities = hass.entities || {};
    const states = hass.states || {};
    for (const e of Object.values(entities)) {
      if (e.device_id !== deviceId) continue;
      const id = e.entity_id;
      const domain = id.split(".")[0];
      const st = states[id];
      const attrs = (st && st.attributes) || {};
      const dc = attrs.device_class;
      const cat = e.entity_category;
      const name = (attrs.friendly_name || e.original_name || e.name || "").toLowerCase();

      if (domain === "switch") {
        // The smart-control switch is a CONFIG entity; the manual open/close
        // damper switch is a primary control. Fall back to the name if the
        // category isn't populated.
        if (cat === "config" || name.indexOf("smart") !== -1) this._ids.smart = id;
        else this._ids.openZone = id;
      } else if (domain === "number") {
        if (cat === "config") {
          const upper = name.indexOf("upper") !== -1;
          const lower = name.indexOf("lower") !== -1;
          if (name.indexOf("cool") !== -1) {
            if (upper) this._ids.coolUpper = id;
            else if (lower) this._ids.coolLower = id;
          } else if (name.indexOf("heat") !== -1) {
            if (upper) this._ids.heatUpper = id;
            else if (lower) this._ids.heatLower = id;
          }
        } else {
          this._ids.target = id;
        }
      } else if (domain === "binary_sensor") {
        if (dc === "opening") this._ids.zoneOpen = id;
        else this._ids.conditions = id;
      } else if (domain === "sensor") {
        if (dc === "temperature") this._ids.currentTemp = id;
        else if (dc === "humidity") this._ids.humidity = id;
        else if (name.indexOf("project") !== -1) this._ids.projected = id;
        else if (name.indexOf("cool") !== -1) this._ids.coolRange = id;
        else if (name.indexOf("heat") !== -1) this._ids.heatRange = id;
      }
    }

    const dev = hass.devices && hass.devices[deviceId];
    if (dev) this._deviceName = dev.name_by_user || dev.name || "Zone";
  }

  _moreInfo(entityId) {
    if (entityId) fireEvent(this, "hass-more-info", { entityId });
  }

  _setNumber(entityId, value) {
    if (!entityId || !this._hass) return;
    this._hass.callService("number", "set_value", { entity_id: entityId, value });
  }

  _switchEntity(which) {
    return which === "zone" ? this._ids.openZone : this._ids.smart;
  }

  _toggleSwitch(which) {
    const id = this._switchEntity(which);
    if (!id || !this._hass) return;
    const st = this._hass.states[id];
    if (!st || st.state === "unavailable" || st.state === "unknown") return;
    this._hass.callService(
      "switch", st.state === "on" ? "turn_off" : "turn_on", { entity_id: id }
    );
  }

  _setSwitch(which, on) {
    const id = this._switchEntity(which);
    if (!id || !this._hass) return;
    const st = this._hass.states[id];
    if (!st || st.state === "unavailable" || st.state === "unknown") return;
    if ((st.state === "on") === on) return;
    this._hass.callService("switch", on ? "turn_on" : "turn_off", { entity_id: id });
  }

  _stepNumber(entityId, dir) {
    const st = this._hass.states[entityId];
    if (!st) return;
    const a = st.attributes || {};
    const step = Number(a.step) || 0.5;
    const min = a.min !== undefined ? Number(a.min) : -Infinity;
    const max = a.max !== undefined ? Number(a.max) : Infinity;
    let v = Number(st.state);
    if (isNaN(v)) v = 0;
    v = Math.round((v + dir * step) / step) * step;
    v = Math.min(max, Math.max(min, v));
    this._setNumber(entityId, Number(v.toFixed(2)));
  }

  // ----------------------------- details popup -----------------------------

  _openDetails() {
    if (this._detailsHost) return;
    const host = document.createElement("div");
    // Mounted at document body in its own shadow root so it overlays the whole
    // UI regardless of dashboard stacking contexts; theme variables still apply.
    const sr = host.attachShadow({ mode: "open" });
    const offsetRow = (key, label) =>
      this._ids[key]
        ? `<div class="d-offset">
             <span class="d-o-label">${label}</span>
             <div class="d-stepper">
               <button data-dstep="${key}" data-dir="-1" aria-label="decrease">−</button>
               <span data-dval="${key}">–</span>
               <button data-dstep="${key}" data-dir="1" aria-label="increase">+</button>
             </div>
           </div>`
        : "";
    const statusRow = (attr, label) =>
      `<div class="d-stat"><span>${label}</span><span data-d="${attr}">–</span></div>`;
    const hasStatus =
      this._ids.conditions || this._ids.coolRange || this._ids.heatRange;
    const hasOffsets =
      this._ids.coolUpper || this._ids.coolLower || this._ids.heatUpper || this._ids.heatLower;

    sr.innerHTML = `
      <style>${this._detailStyles()}</style>
      <div class="backdrop" id="backdrop">
        <div class="modal" role="dialog" aria-modal="true">
          <div class="m-head">
            <span class="m-title" data-d="name">${this._config.name || this._deviceName}</span>
            <button class="m-close" id="close" aria-label="Close">✕</button>
          </div>
          <div class="m-body">
            ${hasStatus ? `
            <div class="m-section">Status</div>
            ${this._ids.conditions ? statusRow("dcond", "Conditions") : ""}
            ${this._ids.conditions ? `<div class="d-conditions" data-d-conditions></div>` : ""}
            ${this._ids.coolRange ? statusRow("dcool", "Cooling band") : ""}
            ${this._ids.heatRange ? statusRow("dheat", "Heating band") : ""}` : ""}
            ${hasOffsets ? `
            <div class="m-section">Temperature offsets</div>
            <div class="m-hint">How far the room may drift from target before the zone switches.</div>
            ${offsetRow("coolUpper", "Cooling upper")}
            ${offsetRow("coolLower", "Cooling lower")}
            ${offsetRow("heatUpper", "Heating upper")}
            ${offsetRow("heatLower", "Heating lower")}` : ""}
            ${!hasStatus && !hasOffsets ? `
            <div class="m-hint">No additional details for this zone.</div>` : ""}
          </div>
        </div>
      </div>`;

    document.body.appendChild(host);
    this._detailsHost = host;

    const close = () => this._closeDetails();
    sr.getElementById("close").addEventListener("click", close);
    sr.getElementById("backdrop").addEventListener("click", (ev) => {
      if (ev.target === ev.currentTarget) close();
    });
    this._detailsKeyHandler = (ev) => { if (ev.key === "Escape") close(); };
    window.addEventListener("keydown", this._detailsKeyHandler);
    sr.querySelectorAll("button[data-dstep]").forEach((btn) => {
      btn.addEventListener("click", () => {
        this._stepNumber(this._ids[btn.getAttribute("data-dstep")], Number(btn.getAttribute("data-dir")));
      });
    });
    this._updateDetails();
  }

  _closeDetails() {
    if (this._detailsKeyHandler) {
      window.removeEventListener("keydown", this._detailsKeyHandler);
      this._detailsKeyHandler = null;
    }
    if (this._detailsHost) {
      this._detailsHost.remove();
      this._detailsHost = null;
    }
  }

  _updateDetails() {
    const host = this._detailsHost;
    if (!host || !this._hass) return;
    const sr = host.shadowRoot;
    const states = this._hass.states;
    const set = (attr, val) => { const el = sr.querySelector(`[data-d="${attr}"]`); if (el) el.textContent = val; };
    const num = (id, fixed, suffix) => {
      const st = id && states[id];
      if (!st) return "–";
      const v = Number(st.state);
      if (isNaN(v)) return st.state;
      return `${v.toFixed(fixed)}${suffix || ""}`;
    };

    const esc = (s) => String(s).replace(/[&<>"]/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

    set("name", this._config.name || this._deviceName);

    if (this._ids.conditions) {
      const c = states[this._ids.conditions];
      set("dcond", c ? (c.state === "on" ? "Met" : "Not met") : "–");
      // Per-condition breakdown so it's clear which one isn't met.
      const listEl = sr.querySelector("[data-d-conditions]");
      if (listEl) {
        const list = (c && c.attributes && c.attributes.conditions) || [];
        listEl.innerHTML = list.length
          ? list.map((cond) => {
              const src = states[cond.entity];
              const name =
                (src && src.attributes && src.attributes.friendly_name) || cond.entity;
              const ok = !!cond.ok;
              const icon = ok ? "mdi:check-circle-outline" : "mdi:alert-circle-outline";
              const detail = ok
                ? esc(cond.current)
                : `${esc(cond.current)} · needs ${esc(cond.required)}`;
              return `<div class="d-cond ${ok ? "ok" : "bad"}">
                <ha-icon icon="${icon}"></ha-icon>
                <span class="d-cond-name">${esc(name)}</span>
                <span class="d-cond-state">${detail}</span>
              </div>`;
            }).join("")
          : `<div class="m-hint">No conditions configured.</div>`;
      }
    }
    if (this._ids.coolRange) { const s = states[this._ids.coolRange]; set("dcool", s ? s.state : "–"); }
    if (this._ids.heatRange) { const s = states[this._ids.heatRange]; set("dheat", s ? s.state : "–"); }

    ["coolUpper", "coolLower", "heatUpper", "heatLower"].forEach((key) => {
      const id = this._ids[key];
      if (!id) return;
      const el = sr.querySelector(`[data-dval="${key}"]`);
      if (el) el.textContent = num(id, 1);
    });
  }

  _detailStyles() {
    return `
      :host { all: initial; }
      ha-icon { opacity: .85; }
      .backdrop {
        position: fixed; inset: 0; z-index: 9999;
        background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center;
        padding: 16px;
        font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, "Helvetica Neue", Arial, sans-serif));
      }
      .modal {
        width: 100%; max-width: 420px; max-height: 85vh; overflow: auto;
        background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);
        border-radius: 16px; box-shadow: 0 12px 40px rgba(0,0,0,.35);
      }
      .m-head { display: flex; align-items: center; justify-content: space-between;
                padding: 16px 16px 8px 16px; }
      .m-title { font-size: 1.2rem; font-weight: var(--ha-font-weight-medium, 500); }
      .m-close { border: none; background: transparent; cursor: pointer; font-size: 1.1rem;
                 color: var(--secondary-text-color, #727272); width: 32px; height: 32px; border-radius: 50%; }
      .m-close:hover { background: var(--secondary-background-color, #e0e0e0); }
      .m-body { padding: 4px 16px 16px 16px; }
      .m-section { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
                   color: var(--secondary-text-color, #727272); margin: 14px 0 6px 0;
                   border-top: 1px solid var(--divider-color, #e0e0e0); padding-top: 12px; }
      .m-section:first-child { margin-top: 4px; border-top: none; padding-top: 0; }
      .m-hint { font-size: .82rem; color: var(--secondary-text-color, #727272); margin-bottom: 8px; }
      .d-stat { display: flex; justify-content: space-between; padding: 4px 0; font-size: .95rem; }
      .d-stat span:last-child { font-weight: var(--ha-font-weight-medium, 500); }
      .d-conditions { display: grid; gap: 2px; margin: 2px 0 4px 0; }
      .d-cond { display: flex; align-items: center; gap: 8px; padding: 3px 0 3px 8px; font-size: .9rem; }
      .d-cond ha-icon { --mdc-icon-size: 18px; flex: 0 0 auto; }
      .d-cond-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                     color: var(--primary-text-color); }
      .d-cond-state { flex: 0 0 auto; font-weight: var(--ha-font-weight-medium, 500); color: var(--secondary-text-color); }
      .d-cond.ok ha-icon { color: var(--secondary-text-color); opacity: .7; }
      .d-cond.ok .d-cond-name { color: var(--secondary-text-color); }
      .d-cond.bad ha-icon { color: var(--primary-text-color); }
      .d-offset { display: flex; align-items: center; justify-content: space-between; padding: 5px 0; }
      .d-o-label { font-size: .95rem; }
      .d-stepper { display: flex; align-items: center; gap: 10px; }
      .d-stepper button {
        width: 32px; height: 32px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0);
        background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);
        font-size: 1.15rem; line-height: 1; cursor: pointer;
      }
      .d-stepper button:hover { background: var(--secondary-background-color, #f0f0f0); }
      .d-stepper span { min-width: 2.6ch; text-align: center; font-weight: var(--ha-font-weight-medium, 500); }
    `;
  }

  _build() {
    const c = this._config;
    const hasZone = !!this._ids.target;
    const statusMode = c.status_display || "full";
    const compact = statusMode === "compact";
    const showGrid = statusMode === "full";
    const showCondIcon = compact && !!this._ids.conditions;
    // In compact mode the current temperature moves up into the header status
    // area and humidity is dropped, so the separate "Now" bar isn't shown.
    const showHeadTemp = compact && !!this._ids.currentTemp;
    const showNow = !compact && (this._ids.currentTemp || this._ids.humidity);
    // From-target deviation bar: opt-out via the editor, shown in both full and
    // compact modes, and only when a room temperature sensor exists.
    const showDeviation = c.show_deviation !== false && !!this._ids.currentTemp;

    if (!hasZone) {
      this.shadowRoot.innerHTML = `
        <style>${this._styles()}</style>
        <ha-card>
          <div class="empty">
            <ha-icon icon="mdi:home-thermometer-outline"></ha-icon>
            <div>Pick a SmarterZones <b>zone device</b> in the card settings.</div>
          </div>
        </ha-card>`;
      this._built = true;
      return;
    }

    const statusItem = (cls, label) =>
      `<div class="s-item ${cls}"><span class="s-label">${label}</span><span class="s-val" data-${cls}>–</span></div>`;

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="header">
          <div class="title" data-name>${this._deviceName}</div>
          <div class="head-right">
            ${showHeadTemp ? `
            <button class="head-temp" data-temp-mi aria-label="Current temperature" title="Current temperature">
              <ha-icon icon="mdi:thermometer"></ha-icon>
              <span data-head-temp>–</span>
            </button>` : ""}
            ${showCondIcon ? `
            <button class="cond-icon" id="cond-icon" aria-label="Conditions" title="Conditions">
              <ha-icon icon="mdi:check-circle-outline"></ha-icon>
            </button>` : ""}
            <button class="info-btn" id="details-btn" aria-label="Zone details" title="Zone details">
              <ha-icon icon="mdi:tune-variant"></ha-icon>
            </button>
          </div>
        </div>

        <div class="content">
          <div class="field">
            <span class="f-label">Managed</span>
            <div class="ctrl-switch" data-switch="smart" role="switch" tabindex="0"
                 aria-checked="false" aria-label="Managed">
              <div class="cs-bg"></div>
              <div class="cs-thumb"><ha-icon class="cs-icon" icon="mdi:power-off"></ha-icon></div>
            </div>
          </div>

          ${this._ids.openZone ? `
          <div class="field">
            <span class="f-label">Zone</span>
            <div class="ctrl-switch" data-switch="zone" role="switch" tabindex="0"
                 aria-checked="false" aria-label="Zone open">
              <div class="cs-bg"></div>
              <div class="cs-thumb"><ha-icon class="cs-icon" icon="mdi:air-filter"></ha-icon></div>
            </div>
          </div>` : ""}

          ${showNow ? `
          <div class="field">
            <span class="f-label" data-now-label>Now</span>
            <div class="now-panel">
              <div class="now-items">
                ${this._ids.currentTemp ? `
                <div class="now-item" data-temp-mi>
                  <ha-icon icon="mdi:thermometer"></ha-icon>
                  <span data-current-temp>–</span>
                </div>` : ""}
                ${this._ids.humidity ? `
                <div class="now-item" data-hum-mi>
                  <ha-icon icon="mdi:water-percent"></ha-icon>
                  <span data-humidity>–</span>
                </div>` : ""}
              </div>
            </div>
          </div>` : ""}

          <div class="field">
            <span class="f-label">Target</span>
            <div class="target">
              <div class="t-ctrl">
                <button class="t-btn dec" id="t-dec" aria-label="Lower target">−</button>
                <div class="t-readout" data-target-mi>
                  <span class="t-val" data-target>–</span>
                  <span class="t-unit" data-unit>°C</span>
                </div>
                <button class="t-btn inc" id="t-inc" aria-label="Raise target">+</button>
              </div>
            </div>
          </div>

          ${showGrid ? `
          <div class="status">
            ${statusItem("projected", "Projected")}
            ${this._ids.conditions ? statusItem("conditions", "Conditions") : ""}
            ${this._ids.coolRange ? statusItem("cool", "Cooling band") : ""}
            ${this._ids.heatRange ? statusItem("heat", "Heating band") : ""}
          </div>` : ""}

          ${showDeviation ? `
          <div class="field">
            <span class="f-label">From target</span>
            <div class="dev">
              <div class="dev-track">
                <div class="dev-fill" data-dev-fill></div>
                <div class="dev-ctr"></div>
              </div>
              <span class="dev-delta" data-dev-delta>–</span>
            </div>
          </div>` : ""}
        </div>
      </ha-card>`;

    const root = this.shadowRoot;
    // HA-style control switches (smart control + manual zone open/close). Click
    // toggles; keyboard mirrors ha-control-switch (Enter/Space toggle, arrows/Home/
    // End set a definite side).
    root.querySelectorAll(".ctrl-switch").forEach((el) => {
      const which = el.getAttribute("data-switch");
      el.addEventListener("click", () => this._toggleSwitch(which));
      el.addEventListener("keydown", (ev) => {
        switch (ev.key) {
          case "Enter":
          case " ":
            ev.preventDefault(); this._toggleSwitch(which); break;
          case "ArrowLeft":
          case "ArrowDown":
          case "Home":
            ev.preventDefault(); this._setSwitch(which, false); break;
          case "ArrowRight":
          case "ArrowUp":
          case "End":
            ev.preventDefault(); this._setSwitch(which, true); break;
        }
      });
    });
    // Target steppers
    const tdec = root.getElementById("t-dec");
    const tinc = root.getElementById("t-inc");
    if (tdec) tdec.addEventListener("click", () => this._stepNumber(this._ids.target, -1));
    if (tinc) tinc.addEventListener("click", () => this._stepNumber(this._ids.target, 1));
    const tmi = root.querySelector("[data-target-mi]");
    if (tmi) tmi.addEventListener("click", () => this._moreInfo(this._ids.target));
    const tempMi = root.querySelector("[data-temp-mi]");
    if (tempMi) tempMi.addEventListener("click", () => this._moreInfo(this._ids.currentTemp));
    const humMi = root.querySelector("[data-hum-mi]");
    if (humMi) humMi.addEventListener("click", () => this._moreInfo(this._ids.humidity));
    // Status more-info
    root.querySelectorAll(".s-item").forEach((el) => {
      el.addEventListener("click", () => {
        if (el.classList.contains("projected")) this._moreInfo(this._ids.projected);
        else if (el.classList.contains("conditions")) this._moreInfo(this._ids.conditions);
        else if (el.classList.contains("cool")) this._moreInfo(this._ids.coolRange);
        else if (el.classList.contains("heat")) this._moreInfo(this._ids.heatRange);
      });
    });
    const detailsBtn = root.getElementById("details-btn");
    if (detailsBtn) detailsBtn.addEventListener("click", () => this._openDetails());
    const condIcon = root.getElementById("cond-icon");
    if (condIcon) condIcon.addEventListener("click", () => this._moreInfo(this._ids.conditions));

    this._built = true;
  }

  _update() {
    const root = this.shadowRoot;
    const hass = this._hass;
    if (!root || !hass || !this._ids || !this._ids.target) return;
    const states = hass.states;
    const txt = (sel, val) => { const el = root.querySelector(sel); if (el) el.textContent = val; };

    const nameEl = root.querySelector("[data-name]");
    if (nameEl) nameEl.textContent = this._config.name || this._deviceName;

    // Target
    const tSt = states[this._ids.target];
    if (tSt) {
      const v = Number(tSt.state);
      txt("[data-target]", isNaN(v) ? tSt.state : v.toFixed(1));
      txt("[data-unit]", (tSt.attributes && tSt.attributes.unit_of_measurement) || "°C");
    }

    // Current temperature / humidity
    const nowLabelEl = root.querySelector("[data-now-label]");
    if (nowLabelEl) nowLabelEl.textContent = this._config.current_label || "Now";
    if (this._ids.currentTemp) {
      const ct = states[this._ids.currentTemp];
      if (ct) {
        const v = Number(ct.state);
        const unit = (ct.attributes && ct.attributes.unit_of_measurement) || "°C";
        const display = isNaN(v) ? "–" : `${v.toFixed(1)} ${unit}`;
        // Now bar (full/hidden modes) and the header chip (compact mode) are
        // mutually exclusive, so only one of these selectors exists at a time.
        txt("[data-current-temp]", display);
        txt("[data-head-temp]", display);
      }
    }
    if (this._ids.humidity) {
      const h = states[this._ids.humidity];
      if (h) {
        const v = Number(h.state);
        txt("[data-humidity]", isNaN(v) ? "–" : `${Math.round(v)}%`);
      }
    }

    // Deviation-from-target bar: centre = target, fill grows right (warmer) or
    // left (cooler). The inner edge (at the target) is square; only the outer end
    // is rounded. Scale: ±DEV_SCALE° maps to the half-width.
    const devFill = root.querySelector("[data-dev-fill]");
    if (devFill) {
      const DEV_SCALE = 3;
      const cur = this._ids.currentTemp ? Number((states[this._ids.currentTemp] || {}).state) : NaN;
      const tgt = tSt ? Number(tSt.state) : NaN;
      const deltaEl = root.querySelector("[data-dev-delta]");
      if (!isNaN(cur) && !isNaN(tgt)) {
        const dev = cur - tgt;
        const w = (Math.min(Math.abs(dev) / DEV_SCALE, 1) * 50).toFixed(1) + "%";
        const warm = dev >= 0;
        devFill.style.width = w;
        devFill.style.left = warm ? "50%" : "auto";
        devFill.style.right = warm ? "auto" : "50%";
        devFill.classList.toggle("warm", warm);
        devFill.classList.toggle("cool", !warm);
        if (deltaEl) {
          const r = Math.round(dev * 10) / 10;
          deltaEl.textContent =
            (r > 0 ? "+" + r.toFixed(1) : r < 0 ? "−" + Math.abs(r).toFixed(1) : r.toFixed(1)) + "°";
        }
      } else {
        devFill.style.width = "0%";
        if (deltaEl) deltaEl.textContent = "–";
      }
    }

    // HA-style control switches: checked state, availability, icon and ARIA.
    const updateSwitch = (which, id, onIcon, offIcon) => {
      const el = root.querySelector(`.ctrl-switch[data-switch="${which}"]`);
      if (!el) return;
      const st = id && states[id];
      const avail = !!st && st.state !== "unavailable" && st.state !== "unknown";
      const on = avail && st.state === "on";
      el.classList.toggle("checked", on);
      el.classList.toggle("disabled", !avail);
      el.setAttribute("aria-checked", on ? "true" : "false");
      el.setAttribute("aria-disabled", avail ? "false" : "true");
      const icon = el.querySelector(".cs-icon");
      if (icon) icon.setAttribute("icon", on ? onIcon : offIcon);
    };
    updateSwitch("smart", this._ids.smart, "mdi:power", "mdi:power-off");
    if (this._ids.openZone) {
      updateSwitch("zone", this._ids.openZone, "mdi:air-filter", "mdi:air-filter");
    }

    // Conditions icon (compact mode, in the header)
    const condIcon = root.getElementById("cond-icon");
    if (condIcon) {
      const c = this._ids.conditions && states[this._ids.conditions];
      const met = c && c.state === "on";
      const haIcon = condIcon.querySelector("ha-icon");
      if (haIcon) haIcon.setAttribute("icon", met ? "mdi:check-circle-outline" : "mdi:alert-circle-outline");
      condIcon.style.color = met ? "var(--secondary-text-color)" : "var(--primary-text-color)";
      condIcon.title = met ? "Conditions met" : "Conditions not met";
    }

    // Status section
    if (this._config.status_display === "full") {
      const proj = this._ids.projected && states[this._ids.projected];
      txt("[data-projected]", proj ? proj.state : "–");
      const projEl = root.querySelector("[data-projected]");
      if (projEl && proj) {
        projEl.classList.toggle("good", proj.state === "Open");
        projEl.classList.toggle("muted", proj.state !== "Open");
      }
      if (this._ids.conditions) {
        const con = states[this._ids.conditions];
        const met = con && con.state === "on";
        txt("[data-conditions]", con ? (met ? "Met" : "Not met") : "–");
        const conEl = root.querySelector("[data-conditions]");
        if (conEl) { conEl.classList.toggle("good", !!met); conEl.classList.toggle("bad", con && !met); }
      }
      if (this._ids.coolRange) {
        const cr = states[this._ids.coolRange];
        txt("[data-cool]", cr ? cr.state : "–");
      }
      if (this._ids.heatRange) {
        const hr = states[this._ids.heatRange];
        txt("[data-heat]", hr ? hr.state : "–");
      }
    }

    // Keep the details popup in sync while it's open.
    if (this._detailsHost) this._updateDetails();
  }

  _styles() {
    return `
      ha-card {
        overflow: hidden;
        /* Match Home Assistant's standard typography (tile-card look). */
        font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, "Helvetica Neue", Arial, sans-serif));
      }
      /* Lighter, more delicate Material-style icons throughout the card. */
      ha-icon { opacity: .85; }
      .header {
        display: flex; align-items: center; justify-content: space-between;
        padding: 16px 16px 0 16px; gap: 12px;
      }
      .title { font-size: 1.25rem; font-weight: var(--ha-font-weight-medium, 500); color: var(--primary-text-color); }
      .head-right { display: flex; align-items: center; gap: 8px; }
      .head-temp {
        display: inline-flex; align-items: center; gap: 4px; border: none; cursor: pointer;
        background: transparent; padding: 0 2px;
        color: var(--primary-text-color); font-size: .95rem; font-weight: var(--ha-font-weight-medium, 500);
      }
      .head-temp ha-icon {
        --mdc-icon-size: 18px; color: var(--state-icon-color, var(--secondary-text-color));
      }
      .info-btn {
        display: inline-flex; align-items: center; justify-content: center;
        width: 32px; height: 32px; border-radius: 50%; border: none; cursor: pointer;
        background: transparent; color: var(--secondary-text-color);
      }
      .info-btn:hover { background: var(--secondary-background-color); color: var(--primary-text-color); }
      .info-btn ha-icon { --mdc-icon-size: 20px; }
      .cond-icon {
        display: inline-flex; align-items: center; justify-content: center;
        width: 32px; height: 32px; border-radius: 50%; border: none; cursor: pointer;
        background: transparent; color: var(--secondary-text-color); padding: 0;
      }
      .cond-icon:hover { background: var(--secondary-background-color); }
      .cond-icon ha-icon { --mdc-icon-size: 20px; opacity: .75; }
      .content { padding: 12px 16px 16px 16px; display: flex; flex-direction: column; gap: 14px; }

      /* Faithful replica of HA's ha-control-switch (the tile-card toggle): a track
         with a subtle state-tinted background layer and a rounded thumb that slides
         between off (left) and on (right). Same tokens, structure and 180ms
         ease-in-out transitions as the real component. */
      .ctrl-switch {
        --control-switch-on-color: var(--primary-color);
        --control-switch-off-color: var(--disabled-color);
        --control-switch-background-opacity: 0.2;
        --control-switch-thickness: 42px;
        --control-switch-border-radius: 12px;
        /* 0 padding so the thumb fills the whole channel (height + active edge);
           the thumb keeps its own border-radius so corners still round. */
        --control-switch-padding: 0px;
        position: relative; box-sizing: border-box; flex: 1 1 auto;
        height: var(--control-switch-thickness);
        border-radius: var(--control-switch-border-radius);
        padding: var(--control-switch-padding);
        display: flex; overflow: hidden; cursor: pointer; outline: none;
        user-select: none; -webkit-tap-highlight-color: transparent;
      }
      .ctrl-switch:focus-visible { box-shadow: 0 0 0 2px var(--control-switch-on-color); }
      .ctrl-switch .cs-bg {
        position: absolute; top: 0; left: 0; height: 100%; width: 100%;
        background-color: var(--control-switch-off-color);
        opacity: var(--control-switch-background-opacity);
        transition: background-color 180ms ease-in-out;
      }
      /* Thumb is 40% of the bar and slides edge-to-edge: travel = (100% - 40%) of the
         bar = 60%, which is 1.5 of the thumb's own width, hence translateX(150%). */
      .ctrl-switch .cs-thumb {
        position: relative; z-index: 1; width: 40%; height: 100%; box-sizing: border-box;
        border-radius: calc(var(--control-switch-border-radius) - var(--control-switch-padding));
        background-color: var(--control-switch-off-color);
        transform: translateX(0);
        transition: transform 180ms ease-in-out, background-color 180ms ease-in-out;
        display: flex; align-items: center; justify-content: center;
        color: var(--text-primary-color, #fff);
      }
      .ctrl-switch .cs-icon { --mdc-icon-size: 20px; }
      .ctrl-switch.checked .cs-bg { background-color: var(--control-switch-on-color); }
      .ctrl-switch.checked .cs-thumb {
        transform: translateX(150%); background-color: var(--control-switch-on-color);
      }
      .ctrl-switch.disabled { opacity: .5; cursor: default; }

      .field { display: flex; flex-direction: row; align-items: center; gap: 12px; }
      .f-label { flex: 0 0 auto; width: 64px; white-space: nowrap;
                 color: var(--secondary-text-color); font-size: .9rem; }
      .now-panel, .target {
        flex: 1 1 auto;
        background: var(--secondary-background-color);
        border-radius: 14px; padding: 8px 14px; min-height: 46px; box-sizing: border-box;
        display: flex; align-items: center; gap: 16px;
      }
      .now-panel { justify-content: center; }
      .now-items { display: flex; align-items: center; gap: 16px; }
      .now-item { display: flex; align-items: center; gap: 6px; cursor: pointer;
                  color: var(--primary-text-color); font-size: 1.1rem; font-weight: var(--ha-font-weight-medium, 500); }
      .now-item ha-icon { --mdc-icon-size: 18px; color: var(--state-icon-color, var(--secondary-text-color)); }
      .t-ctrl { width: 100%; display: flex; align-items: center; justify-content: space-between; }
      .t-readout {
        display: inline-flex; align-items: baseline; gap: 2px;
        line-height: 1; cursor: pointer; min-width: 4.2ch; justify-content: center;
      }
      .t-val { font-size: 1.1rem; font-weight: var(--ha-font-weight-medium, 500); color: var(--primary-text-color); }
      .t-unit { font-size: 1.1rem; font-weight: var(--ha-font-weight-normal, 400); color: var(--primary-text-color); }
      .t-btn {
        flex: 0 0 auto; width: 32px; height: 32px; border-radius: 50%; border: none; cursor: pointer;
        background: rgba(127, 127, 127, .16); color: var(--primary-text-color);
        font-size: 1.25rem; line-height: 1;
        display: flex; align-items: center; justify-content: center;
        transition: background-color .15s ease;
      }
      .t-btn:hover { background-color: rgba(127, 127, 127, .28); }
      .t-btn:active { background-color: rgba(127, 127, 127, .4); }

      /* Deviation-from-target bar: target in the centre, fill grows out to the
         current reading. The end at the target is square; only the outer end is
         rounded (set per-direction in JS via .warm / .cool). */
      .dev { flex: 1 1 auto; display: flex; align-items: center; gap: 12px; }
      .dev-track {
        position: relative; flex: 1 1 auto; height: 10px; border-radius: 5px;
        background: var(--secondary-background-color);
      }
      .dev-fill {
        position: absolute; top: 0; bottom: 0; background: var(--primary-color);
        border-radius: 0; transition: width .2s ease;
      }
      .dev-fill.warm { border-radius: 0 5px 5px 0; }
      .dev-fill.cool { border-radius: 5px 0 0 5px; }
      .dev-ctr {
        position: absolute; left: 50%; top: -3px; bottom: -3px; width: 2px; z-index: 1;
        background: var(--secondary-text-color); transform: translateX(-1px); border-radius: 1px;
      }
      .dev-delta {
        flex: 0 0 auto; min-width: 4ch; text-align: right; font-size: .95rem;
        font-weight: var(--ha-font-weight-medium, 500); color: var(--primary-text-color);
      }

      .status {
        display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px;
        border-top: 1px solid var(--divider-color); padding-top: 12px;
      }
      .s-item { display: flex; align-items: center; justify-content: space-between; cursor: pointer; }
      .s-label { color: var(--secondary-text-color); font-size: .9rem; }
      .s-val { color: var(--primary-text-color); font-weight: var(--ha-font-weight-medium, 500); font-size: .9rem; }
      .s-val.good { color: var(--primary-text-color); }
      .s-val.bad { color: var(--secondary-text-color); }
      .s-val.muted { color: var(--secondary-text-color); font-weight: var(--ha-font-weight-medium, 500); }

      .empty { padding: 24px 16px; display: flex; flex-direction: column; align-items: center; gap: 10px;
               color: var(--secondary-text-color); text-align: center; }
      .empty ha-icon { --mdc-icon-size: 40px; }
    `;
  }
}

if (!customElements.get("smarterzones-zone-card")) {
  customElements.define("smarterzones-zone-card", SmarterZonesZoneCard);
}

/* ----------------------------- config editor ----------------------------- */

const EDITOR_SCHEMA = [
  { name: "device", required: true, selector: { device: { integration: "smarterzones" } } },
  { name: "name", selector: { text: {} } },
  { name: "current_label", selector: { text: {} } },
  {
    name: "status_display",
    selector: {
      select: {
        mode: "dropdown",
        options: [
          { value: "full", label: "Full status (projected, conditions, bands)" },
          { value: "compact", label: "Compact (temperature + conditions icon in header)" },
        ],
      },
    },
  },
  { name: "show_deviation", selector: { boolean: {} } },
];

class SmarterZonesZoneCardEditor extends HTMLElement {
  setConfig(config) {
    const cfg = Object.assign({}, config);
    // Migrate legacy options: show_status/compact_status booleans, the removed
    // "hidden" status_display value, and the removed show_details toggle.
    if (cfg.status_display === undefined) {
      cfg.status_display =
        cfg.show_status === false || cfg.compact_status ? "compact" : "full";
    }
    if (cfg.status_display === "hidden") cfg.status_display = "compact";
    if (cfg.show_deviation === undefined) cfg.show_deviation = true;
    delete cfg.show_status;
    delete cfg.compact_status;
    delete cfg.show_details;
    this._config = cfg;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _label(schema) {
    const labels = {
      device: "Zone device",
      name: "Name (optional, overrides the device name)",
      current_label: "Current readings label (e.g. \"Now\")",
      status_display: "Status display",
      show_deviation: "Show \"from target\" deviation bar",
    };
    return labels[schema.name] || schema.name;
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => this._label(s);
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        fireEvent(this, "config-changed", { config: ev.detail.value });
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = this._config;
  }
}

if (!customElements.get("smarterzones-zone-card-editor")) {
  customElements.define("smarterzones-zone-card-editor", SmarterZonesZoneCardEditor);
}

/* --------------------------- card registration --------------------------- */

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "smarterzones-zone-card")) {
  window.customCards.push({
    type: "smarterzones-zone-card",
    name: "SmarterZones Zone",
    description: "A complete SmarterZones zone: target, smart control, status and offsets.",
    preview: true,
    documentationURL: "https://github.com/",
  });
}

console.info(
  `%c SMARTERZONES-ZONE-CARD %c ${CARD_VERSION} `,
  "color:#fff;background:#0369A1;font-weight:700;border-radius:3px 0 0 3px;padding:2px 4px;",
  "color:#0369A1;background:#e0f2fe;font-weight:700;border-radius:0 3px 3px 0;padding:2px 4px;"
);
