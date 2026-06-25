/* Svitgrid HA sidebar panel — dependency-free ES module, shadow DOM.
 * UX revamp 2026-06-25: theme-native colors, honest staleness, readable
 * 30-day chart, mini energy-flow row, a11y, in-place refresh.
 *
 * Data contract (UNCHANGED):
 *   this._hass.callApi("GET", "svitgrid/live|today|history|sync-status")
 *   live:    { inverters: [{ inverterId, ts, payload:{ pvPower, batterySoc,
 *                            batteryPower, gridPower, loadPower } }] }
 *   today:   { inverters: [{ energy:{ dailyPvEnergy, dailyLoadEnergy,
 *                            dailyGridImportEnergy, dailyGridExportEnergy } }] }
 *   history: { days: [{ day, energy:{ dailyPvEnergy } }] }
 *   sync:    { counts:{ sent, pending, failed, skipped }, last_sent_ts }
 *
 * Sign conventions (CORRECT — do not change, only display):
 *   batteryPower < 0 = discharging; >= 0 = charging.
 *   gridPower    > 0 = importing;   <= 0 = exporting.
 */

(function () {
  "use strict";

  // ------------------------------------------------------------------ //
  // Constants
  // ------------------------------------------------------------------ //
  const POLL_MS = 10000;          // live / today / sync cadence
  const HISTORY_MS = 5 * 60 * 1000; // history refetch cadence (5 min)
  const STALE_MS = 90 * 1000;      // card considered stale past this age
  const FRESH_MS = 60 * 1000;      // header dot green below this age

  // Future-i18n string table (English for v1). Keep all user copy here so a
  // later `uk` pass swaps one object.
  const STR = {
    title: "Svitgrid",
    live: "Live",
    today: "Today",
    history: "Solar — last 30 days",
    historyTitle: "Solar generated — last 30 days (kWh)",
    historyEmpty: "No solar generation recorded in this period.",
    noInverters: "No inverters reporting live data.",
    pv: "Solar",
    battery: "Battery",
    grid: "Grid",
    load: "House",
    charging: "charging",
    discharging: "discharging",
    importing: "importing",
    exporting: "exporting",
    generated: "Generated",
    consumed: "Consumed",
    imported: "Imported",
    exported: "Exported",
    kwh: "kWh",
    stale: "Stale",
    syncedAll: "All readings synced",
    lastSent: "last sent",
    pending: "pending",
    failed: "failed",
    skipped: "skipped",
    never: "never",
    dataAge: "data",
    waiting: "Waiting for data…",
    liveDotLive: "Live data updating",
    liveDotIdle: "Live data idle",
    // Takeover screen
    takeoverDeprovisioned: "Device no longer provisioned",
    takeoverPaused: "Svitgrid paused",
    takeoverDeprovisionedNext: "To reconnect, remove the Svitgrid integration (Settings → Devices & Services → Svitgrid → Delete) and pair it again.",
    takeoverPausedNext: "Paused by the operator. It will resume automatically when re-enabled.",
    takeoverSince: "since",
  };

  // ------------------------------------------------------------------ //
  // Styles — theme-native; accent gold, semantics via warning/error.
  // ------------------------------------------------------------------ //
  const STYLE = `
    :host {
      --svitgrid-accent: #F5A623;
      --accent: var(--svitgrid-accent, var(--primary-color, #F5A623));
      --sp-1: 4px;
      --sp-2: 8px;
      --sp-3: 12px;
      --sp-4: 16px;
      --sp-5: 20px;
      --sp-6: 24px;
      --sg-radius: var(--ha-card-border-radius, 12px);
      --sg-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.12));
      --sg-card-bg: var(--ha-card-background, var(--card-background-color, #fff));
      --sg-text: var(--primary-text-color, #212121);
      --sg-text-2: var(--secondary-text-color, #6f6f6f);
      --sg-divider: var(--divider-color, #e0e0e0);
      --sg-ok: var(--success-color, #2e7d32);
      --sg-warn: var(--warning-color, #ff9800);
      --sg-err: var(--error-color, #d32f2f);
      --sg-muted: var(--disabled-text-color, #9e9e9e);
      display: block;
      font-family: var(--mdc-typography-font-family, var(--paper-font-body1_-_font-family, sans-serif));
      font-size: 14px;
      color: var(--sg-text);
    }
    @media (prefers-color-scheme: dark) {
      :host { --svitgrid-accent: #FFC04D; }
    }

    .panel-root {
      max-width: 1100px;
      margin: 0 auto;
      padding: var(--sp-4);
      box-sizing: border-box;
    }

    /* Header */
    .panel-header {
      display: flex;
      align-items: center;
      gap: var(--sp-3);
      margin-bottom: var(--sp-5);
    }
    .panel-header h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      color: var(--accent);
      letter-spacing: 0.01em;
    }
    .live-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--sg-muted);
      flex-shrink: 0;
      position: relative;
    }
    .live-dot.fresh { background: var(--sg-ok); }
    .live-dot.aging { background: var(--sg-warn); }
    .live-dot.idle  { background: var(--sg-muted); }
    .live-dot.fresh::after {
      content: "";
      position: absolute;
      inset: -4px;
      border-radius: 50%;
      border: 2px solid var(--sg-ok);
      opacity: 0;
      animation: sg-pulse 2s ease-out infinite;
    }
    @keyframes sg-pulse {
      0%   { transform: scale(0.6); opacity: 0.7; }
      100% { transform: scale(1.6); opacity: 0; }
    }
    @media (prefers-reduced-motion: reduce) {
      .live-dot.fresh::after { animation: none; }
    }
    .updated-label {
      margin-left: auto;
      font-size: 12px;
      color: var(--sg-text-2);
      white-space: nowrap;
    }

    /* Section titles */
    h2.section-title {
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--sg-text-2);
      margin: var(--sp-6) 0 var(--sp-3);
    }

    /* Live cards (heaviest visual weight) */
    .inverter-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: var(--sp-4);
    }
    .inv-card {
      background: var(--sg-card-bg);
      border-radius: var(--sg-radius);
      box-shadow: var(--sg-shadow);
      padding: var(--sp-5);
      transition: opacity 0.3s ease;
    }
    .inv-card.stale { opacity: 0.55; }
    .inv-card-head {
      display: flex;
      align-items: center;
      gap: var(--sp-2);
      margin-bottom: var(--sp-3);
    }
    .inv-card-title {
      font-size: 13px;
      font-weight: 700;
      color: var(--sg-text);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
    }
    .stale-badge {
      font-size: 11px;
      font-weight: 600;
      color: var(--sg-warn);
      white-space: nowrap;
      flex-shrink: 0;
    }
    /* Mini energy-flow row */
    .flow-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--sp-1) var(--sp-2);
      padding: var(--sp-2) 0 var(--sp-3);
      margin-bottom: var(--sp-2);
      border-bottom: 1px solid var(--sg-divider);
      font-size: 12px;
      color: var(--sg-text-2);
    }
    .flow-seg {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      white-space: nowrap;
    }
    .flow-seg svg { width: 16px; height: 16px; flex-shrink: 0; }
    .flow-seg .flow-val { font-weight: 600; color: var(--sg-text); }
    .flow-seg.ok   .flow-val,
    .flow-seg.ok   svg { color: var(--sg-ok); }
    .flow-seg.warn .flow-val,
    .flow-seg.warn svg { color: var(--sg-warn); }
    .flow-arrow { color: var(--sg-text-2); }
    .flow-sep { color: var(--sg-divider); }

    .inv-row {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      padding: var(--sp-1) 0;
    }
    .inv-row-label {
      color: var(--sg-text-2);
      font-size: 13px;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .inv-row-label svg { width: 16px; height: 16px; opacity: 0.85; }
    .inv-row-value {
      font-weight: 600;
      font-variant-numeric: tabular-nums;
    }
    .inv-row.headline .inv-row-value { font-size: 19px; }
    .inv-row-sub {
      font-size: 11px;
      color: var(--sg-text-2);
      margin-left: 5px;
      font-weight: 500;
    }

    /* Today tiles (lighter than live) */
    .today-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: var(--sp-3);
    }
    .today-tile {
      background: var(--sg-card-bg);
      border: 1px solid var(--sg-divider);
      border-radius: var(--sg-radius);
      padding: var(--sp-3) var(--sp-4);
      text-align: center;
    }
    .today-tile-value {
      font-size: 22px;
      font-weight: 700;
      color: var(--sg-text);
      line-height: 1.1;
      font-variant-numeric: tabular-nums;
    }
    .today-tile-unit {
      font-size: 11px;
      color: var(--sg-text-2);
      margin-top: 2px;
    }
    .today-tile-label {
      font-size: 12px;
      color: var(--sg-text-2);
      margin-top: var(--sp-2);
    }

    /* History bar chart */
    .history-chart {
      background: var(--sg-card-bg);
      border: 1px solid var(--sg-divider);
      border-radius: var(--sg-radius);
      padding: var(--sp-4);
      position: relative;
    }
    .chart-area {
      display: flex;
      gap: var(--sp-3);
    }
    .y-axis {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      align-items: flex-end;
      height: 140px;
      font-size: 10px;
      color: var(--sg-text-2);
      flex-shrink: 0;
      min-width: 34px;
      font-variant-numeric: tabular-nums;
    }
    .chart-plot {
      position: relative;
      flex: 1;
      min-width: 0;
      overflow-x: auto;
    }
    .gridline {
      position: absolute;
      left: 0;
      right: 0;
      border-top: 1px dashed var(--sg-divider);
      pointer-events: none;
    }
    .bar-chart {
      display: flex;
      align-items: flex-end;
      gap: 3px;
      height: 140px;
      position: relative;
      min-width: 100%;
    }
    .bar-col {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-end;
      flex: 1;
      min-width: 7px;
      height: 100%;
      position: relative;
    }
    .bar-cap {
      font-size: 9px;
      color: var(--sg-text);
      font-weight: 600;
      margin-bottom: 2px;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    .bar {
      width: 100%;
      background: var(--accent);
      border-radius: 2px 2px 0 0;
      min-height: 2px;
      cursor: pointer;
    }
    .bar-axis {
      display: flex;
      gap: 3px;
      margin: var(--sp-1) 0 0 calc(34px + var(--sp-3));
    }
    .bar-label {
      flex: 1;
      min-width: 7px;
      font-size: 9px;
      color: var(--sg-text-2);
      text-align: center;
      white-space: nowrap;
      overflow: hidden;
    }
    .chart-tooltip {
      position: absolute;
      pointer-events: none;
      background: var(--sg-text);
      color: var(--sg-card-bg);
      font-size: 11px;
      padding: 4px 8px;
      border-radius: 6px;
      white-space: nowrap;
      transform: translate(-50%, -100%);
      opacity: 0;
      transition: opacity 0.12s ease;
      z-index: 5;
    }
    .chart-tooltip.show { opacity: 1; }
    @media (prefers-reduced-motion: reduce) {
      .chart-tooltip { transition: none; }
    }
    .history-empty {
      color: var(--sg-text-2);
      text-align: center;
      padding: var(--sp-6) var(--sp-4);
      font-size: 13px;
    }

    /* Sync footer */
    .sync-footer {
      margin-top: var(--sp-5);
      font-size: 12px;
      color: var(--sg-text);
      padding: var(--sp-3) var(--sp-4);
      background: var(--sg-card-bg);
      border: 1px solid var(--sg-divider);
      border-radius: var(--sg-radius);
      display: flex;
      align-items: center;
      gap: var(--sp-2);
    }
    .sync-footer.issue {
      background: color-mix(in srgb, var(--sg-warn) 12%, var(--sg-card-bg));
      border-color: color-mix(in srgb, var(--sg-warn) 40%, var(--sg-divider));
      color: var(--sg-text);
    }
    .sync-lead { font-weight: 700; }
    .sync-footer.ok .sync-lead { color: var(--sg-ok); }
    .sync-footer.issue .sync-lead { color: var(--sg-warn); }
    .sync-detail { color: var(--sg-text-2); }

    /* States */
    .section-error {
      font-size: 12px;
      color: var(--sg-err);
      padding: var(--sp-2) 0;
    }

    /* Takeover screen (deprovisioned / paused) */
    .takeover-body {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      text-align: center;
      padding: 48px var(--sp-4);
      gap: var(--sp-4);
    }
    .takeover-icon {
      width: 56px;
      height: 56px;
      opacity: 0.75;
    }
    .takeover-headline {
      font-size: 20px;
      font-weight: 700;
      color: var(--sg-text);
      margin: 0;
    }
    .takeover-reason {
      font-size: 14px;
      color: var(--sg-text-2);
      max-width: 480px;
      margin: 0;
    }
    .takeover-since {
      font-size: 12px;
      color: var(--sg-muted);
      margin: 0;
    }
    .takeover-next {
      font-size: 13px;
      font-weight: 600;
      max-width: 480px;
      margin: 0;
      line-height: 1.5;
    }
    .takeover-next.deprovisioned { color: var(--error-color, var(--sg-err)); }
    .takeover-next.paused { color: var(--warning-color, var(--sg-warn)); }

    /* Skeletons */
    .skel-grid { display: grid; gap: var(--sp-4); }
    .skel-card {
      background: var(--sg-card-bg);
      border: 1px solid var(--sg-divider);
      border-radius: var(--sg-radius);
      padding: var(--sp-5);
    }
    .skel-line {
      height: 12px;
      border-radius: 4px;
      background: linear-gradient(90deg,
        var(--sg-divider) 25%,
        color-mix(in srgb, var(--sg-divider) 40%, transparent) 50%,
        var(--sg-divider) 75%);
      background-size: 200% 100%;
      animation: sg-shimmer 1.4s ease-in-out infinite;
      margin: var(--sp-2) 0;
    }
    .skel-line.w60 { width: 60%; }
    .skel-line.w40 { width: 40%; }
    .skel-line.w80 { width: 80%; }
    @keyframes sg-shimmer {
      0% { background-position: 200% 0; }
      100% { background-position: -200% 0; }
    }
    @media (prefers-reduced-motion: reduce) {
      .skel-line { animation: none; }
    }

    /* Responsive */
    @media (max-width: 600px) {
      .panel-root { padding: var(--sp-3); }
      .panel-header h1 { font-size: 18px; }
      .inverter-grid { grid-template-columns: 1fr; }
      .today-grid { grid-template-columns: repeat(2, 1fr); }
      .today-tile-value { font-size: 20px; }
      .inv-row.headline .inv-row-value { font-size: 18px; }
    }
  `;

  // ------------------------------------------------------------------ //
  // Inline mdi-style glyphs (24x24 path data; theme-colored via currentColor)
  // ------------------------------------------------------------------ //
  const ICON = {
    // mdi:white-balance-sunny
    solar:
      "M3.55 18.54l1.41 1.41 1.79-1.8-1.41-1.41-1.79 1.8M11 22.45h2V19.5h-2v2.95M4 10.5H1v2h3v-2M13 .55h-2V3.5h2V.55m7.45 9.95v2H23v-2h-2.55m-4.99-3.95l1.79-1.8-1.41-1.41-1.79 1.8 1.41 1.41M17.24 18.16l1.79 1.8 1.41-1.41-1.8-1.79-1.4 1.4M12 6.5a5.5 5.5 0 00-5.5 5.5 5.5 5.5 0 005.5 5.5 5.5 5.5 0 005.5-5.5A5.5 5.5 0 0012 6.5z",
    // mdi:home
    home:
      "M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8h5z",
    // mdi:battery
    battery:
      "M16.67 4H15V2H9v2H7.33C6.6 4 6 4.6 6 5.33v15.34C6 21.4 6.6 22 7.33 22h9.34c.74 0 1.34-.6 1.34-1.33V5.33C18 4.6 17.4 4 16.67 4z",
    // mdi:transmission-tower
    grid:
      "M8.28 5.45L6.5 4.55L7.76 2H16.23L17.5 4.55L15.72 5.44L15 4H9L8.28 5.45M18.62 8L17.5 5.74L15.72 6.63L16.5 8H13V6H11V8H7.5L8.28 6.63L6.5 5.74L5.38 8H2V10H5.13L4.4 12H3V14H4.9L3.79 19H5.84L6.18 17H17.82L18.16 19H20.21L19.1 14H21V12H19.6L18.87 10H22V8H18.62M7.21 12L7.94 10H16.06L16.79 12H7.21M16.5 15H7.5L8 13H16L16.5 15Z",
  };

  function svgIcon(name) {
    const path = ICON[name] || "";
    return (
      '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
      '<path d="' + path + '"/></svg>'
    );
  }

  // ------------------------------------------------------------------ //
  // Number formatting
  // ------------------------------------------------------------------ //
  const NF1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const NF0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

  function isNum(v) {
    return typeof v === "number" && isFinite(v);
  }

  class SvitgridPanel extends HTMLElement {
    constructor() {
      super();
      this._hass = null;
      this._built = false;
      this._timer = null;
      this._lastHistoryFetch = 0;
      this._lastLiveInverterId = null;
      this._invIdsKey = null;      // tracks the set of inverter ids currently rendered
      this._invNodes = {};         // inverterId -> { card, refs... }
      this._freshestAgeMs = null;  // age (ms) of the most-recent inverter reading
      this._todayRefs = null;      // cached DOM refs for today-tile values

      // Shadow refs
      this._liveSec = null;
      this._todaySec = null;
      this._historySec = null;
      this._syncFooter = null;
      this._updatedLabel = null;
      this._liveDot = null;
      this._tooltip = null;

      // Lifecycle / health
      this._lifecycle = { state: "active" };
      this._takeoverShown = false;
      this._panelBody = null; // the panel-root div (for takeover replacement)

      this.attachShadow({ mode: "open" });
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._built) this._buildShell();
      if (!this._timer) this._startPolling();
    }

    connectedCallback() {
      if (!this._built) this._buildShell();
      if (this._hass && !this._timer) this._startPolling();
    }

    disconnectedCallback() {
      if (this._timer) {
        clearInterval(this._timer);
        this._timer = null;
      }
    }

    // ---------------------------------------------------------------- //
    // Shell
    // ---------------------------------------------------------------- //
    _buildShell() {
      this._built = true;
      const shadow = this.shadowRoot;
      shadow.innerHTML = "";

      const style = document.createElement("style");
      style.textContent = STYLE;
      shadow.appendChild(style);

      const root = document.createElement("div");
      root.className = "panel-root";

      // Header
      const header = document.createElement("div");
      header.className = "panel-header";

      const h1 = document.createElement("h1");
      h1.textContent = STR.title;
      header.appendChild(h1);

      const dot = document.createElement("div");
      dot.className = "live-dot idle";
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label", STR.liveDotIdle);
      this._liveDot = dot;
      header.appendChild(dot);

      const updated = document.createElement("span");
      updated.className = "updated-label";
      updated.textContent = STR.waiting;
      this._updatedLabel = updated;
      header.appendChild(updated);

      root.appendChild(header);

      this._panelBody = root;

      // Build normal sections (reusable; also called when returning from takeover).
      this._buildNormalSections();

      shadow.appendChild(root);
    }

    // Rebuild the normal sections inside an existing panel-root.
    // Called after a takeover screen clears back to "active".
    _buildNormalSections() {
      const root = this._panelBody;
      if (!root) return;
      // Remove everything after the header (first child).
      while (root.childNodes.length > 1) {
        root.removeChild(root.lastChild);
      }
      // Reset refs that are body-scoped.
      this._invIdsKey = null;
      this._invNodes = {};
      this._todayRefs = null;
      this._lastHistoryFetch = 0;
      this._tooltip = null;

      this._liveSec = this._addSection(root, STR.live, "live-region");
      this._fillSkeleton(this._liveSec, "live");

      this._todaySec = this._addSection(root, STR.today, "today-region");
      this._fillSkeleton(this._todaySec, "today");

      this._historySec = this._addSection(root, STR.history, "history-region");
      this._fillSkeleton(this._historySec, "history");

      const syncFooter = document.createElement("div");
      syncFooter.className = "sync-footer";
      syncFooter.setAttribute("role", "status");
      syncFooter.setAttribute("aria-live", "polite");
      const skLead = document.createElement("span");
      skLead.className = "skel-line w40";
      skLead.style.height = "12px";
      skLead.style.width = "180px";
      syncFooter.appendChild(skLead);
      this._syncFooter = syncFooter;
      root.appendChild(syncFooter);
    }

    _addSection(root, titleText, ariaLabel) {
      const h2 = document.createElement("h2");
      h2.className = "section-title";
      h2.textContent = titleText;
      root.appendChild(h2);

      const sec = document.createElement("div");
      sec.setAttribute("role", "group");
      sec.setAttribute("aria-label", ariaLabel);
      root.appendChild(sec);
      return sec;
    }

    _fillSkeleton(sec, kind) {
      sec.className = "";
      sec.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "skel-grid";
      if (kind === "today") {
        grid.style.gridTemplateColumns = "repeat(auto-fill, minmax(150px, 1fr))";
      } else {
        grid.style.gridTemplateColumns =
          "repeat(auto-fill, minmax(260px, 1fr))";
      }
      const count = kind === "today" ? 4 : kind === "history" ? 1 : 2;
      for (let i = 0; i < count; i++) {
        const card = document.createElement("div");
        card.className = "skel-card";
        const widths = ["w60", "w80", "w40", "w80"];
        for (const w of widths) {
          const line = document.createElement("div");
          line.className = "skel-line " + w;
          card.appendChild(line);
        }
        grid.appendChild(card);
      }
      sec.appendChild(grid);
    }

    // ---------------------------------------------------------------- //
    // Polling
    // ---------------------------------------------------------------- //
    _startPolling() {
      if (this._timer) return;
      this._refresh();
      this._timer = setInterval(() => this._refresh(), POLL_MS);
    }

    async _refresh() {
      if (!this._hass) return;

      // Health check comes first; on non-active state we skip all data loaders.
      await this._loadHealth();

      if (this._lifecycle.state !== "active") {
        // Show takeover and stop; keep header dot idle.
        this._renderTakeover();
        this._updateHeaderDot(false);
        return;
      }

      // Returning from takeover: rebuild normal sections.
      if (this._takeoverShown) {
        this._takeoverShown = false;
        this._buildNormalSections();
      }

      const results = await Promise.allSettled([
        this._loadLive(),
        this._loadToday(),
        this._loadSync(),
      ]);

      // History on a slower cadence.
      const now = Date.now();
      if (now - this._lastHistoryFetch >= HISTORY_MS || this._lastHistoryFetch === 0) {
        this._lastHistoryFetch = now;
        await this._loadHistory();
      }

      const anyOk = results.some((r) => r.status === "fulfilled" && r.value !== false);
      this._updateHeaderDot(anyOk);
    }

    async _loadHealth() {
      try {
        const h = await this._call("svitgrid/health");
        this._lifecycle = (h && h.state) ? h : { state: "active" };
      } catch (_) {
        // Leave previous lifecycle or default to active (fail-open).
        this._lifecycle = this._lifecycle || { state: "active" };
      }
    }

    _call(path) {
      return this._hass.callApi("GET", path);
    }

    // ---------------------------------------------------------------- //
    // Takeover screen
    // ---------------------------------------------------------------- //
    _renderTakeover() {
      const root = this._panelBody;
      if (!root) return;

      if (this._takeoverShown) {
        // Already showing takeover — update text in place without a full rebuild.
        // Find existing nodes via class names and update them.
        const headlineEl = root.querySelector(".takeover-headline");
        const reasonEl = root.querySelector(".takeover-reason");
        const sinceEl = root.querySelector(".takeover-since");
        const nextEl = root.querySelector(".takeover-next");
        const lc = this._lifecycle;
        if (headlineEl) {
          headlineEl.textContent =
            lc.state === "deprovisioned"
              ? STR.takeoverDeprovisioned
              : STR.takeoverPaused;
        }
        if (reasonEl) {
          if (lc.reason) {
            reasonEl.textContent = lc.reason;
            reasonEl.style.display = "";
          } else {
            reasonEl.textContent = "";
            reasonEl.style.display = "none";
          }
        }
        if (sinceEl) {
          if (lc.since) {
            const sinceMs = Date.now() - new Date(lc.since).getTime();
            sinceEl.textContent = STR.takeoverSince + " " + this._relAge(sinceMs);
            sinceEl.style.display = "";
          } else {
            sinceEl.textContent = "";
            sinceEl.style.display = "none";
          }
        }
        if (nextEl) {
          nextEl.className =
            "takeover-next " +
            (lc.state === "deprovisioned" ? "deprovisioned" : "paused");
          nextEl.textContent =
            lc.state === "deprovisioned"
              ? STR.takeoverDeprovisionedNext
              : STR.takeoverPausedNext;
        }
        return;
      }

      // First-time build: clear body content (keep header = first child).
      while (root.childNodes.length > 1) {
        root.removeChild(root.lastChild);
      }
      this._takeoverShown = true;

      // Null out section refs so they don't receive stale updates.
      this._liveSec = null;
      this._todaySec = null;
      this._historySec = null;
      this._syncFooter = null;
      this._invNodes = {};
      this._todayRefs = null;

      const lc = this._lifecycle;
      const isDeprovisioned = lc.state === "deprovisioned";

      const body = document.createElement("div");
      body.className = "takeover-body";

      // Icon — static inline SVG (not user data; XSS safe)
      const iconWrap = document.createElement("div");
      iconWrap.className = "takeover-icon";
      iconWrap.style.color = isDeprovisioned
        ? "var(--error-color, var(--sg-err))"
        : "var(--warning-color, var(--sg-warn))";
      // mdi:link-off for deprovisioned; mdi:pause-circle-outline for paused
      if (isDeprovisioned) {
        iconWrap.innerHTML =
          '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
          '<path d="M2 5.27L3.28 4 20 20.72 18.73 22l-3.06-3.06L13 21.5c-1.65 1.65-4.37 ' +
          '1.65-6.02 0L3.47 18C1.82 16.35 1.82 13.63 3.47 12L5.5 10 7.5 12 5.47 14c-.68.' +
          '68-.68 1.79 0 2.47l3.51 3.51c.68.68 1.79.68 2.47 0l2.14-2.14L2 5.27m10.5-2.36' +
          'l-1.63 1.63 1.42 1.42 1.63-1.63c.68-.68 1.79-.68 2.47 0l3.51 3.51c.68.68.68 1.' +
          '79 0 2.47L17.5 12l2 2 1.53-1.53c1.65-1.65 1.65-4.37 0-6.02l-3.51-3.51c-1.65-1.' +
          '65-4.37-1.65-6.02 0z"/></svg>';
      } else {
        iconWrap.innerHTML =
          '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
          '<path d="M13 16h2V8h-2m-4 8h2V8H9m3-6C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.' +
          '48 10-10S17.52 2 12 2m0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 ' +
          '8z"/></svg>';
      }
      body.appendChild(iconWrap);

      // Headline
      const headline = document.createElement("p");
      headline.className = "takeover-headline";
      headline.textContent = isDeprovisioned
        ? STR.takeoverDeprovisioned
        : STR.takeoverPaused;
      body.appendChild(headline);

      // Reason (textContent only — user/API supplied)
      const reasonEl = document.createElement("p");
      reasonEl.className = "takeover-reason";
      if (lc.reason) {
        reasonEl.textContent = lc.reason;
      } else {
        reasonEl.style.display = "none";
      }
      body.appendChild(reasonEl);

      // Since (relative time)
      const sinceEl = document.createElement("p");
      sinceEl.className = "takeover-since";
      if (lc.since) {
        const sinceMs = Date.now() - new Date(lc.since).getTime();
        sinceEl.textContent = STR.takeoverSince + " " + this._relAge(sinceMs);
      } else {
        sinceEl.style.display = "none";
      }
      body.appendChild(sinceEl);

      // Next-step text (static copy — not API data)
      const nextEl = document.createElement("p");
      nextEl.className =
        "takeover-next " + (isDeprovisioned ? "deprovisioned" : "paused");
      nextEl.textContent = isDeprovisioned
        ? STR.takeoverDeprovisionedNext
        : STR.takeoverPausedNext;
      body.appendChild(nextEl);

      root.appendChild(body);
    }

    // ---------------------------------------------------------------- //
    // Header dot / freshness
    // ---------------------------------------------------------------- //
    _updateHeaderDot(anyOk) {
      if (!this._liveDot) return;
      this._liveDot.classList.remove("fresh", "aging", "idle");

      if (!anyOk || this._freshestAgeMs == null) {
        this._liveDot.classList.add("idle");
        this._liveDot.setAttribute("aria-label", STR.liveDotIdle);
        if (this._updatedLabel && !anyOk) {
          this._updatedLabel.textContent = STR.liveDotIdle;
        }
        return;
      }

      const ageMs = this._freshestAgeMs;
      if (ageMs < FRESH_MS) {
        this._liveDot.classList.add("fresh");
      } else {
        this._liveDot.classList.add("aging");
      }
      const ageLabel = this._relAge(ageMs);
      this._liveDot.setAttribute(
        "aria-label",
        ageMs < FRESH_MS ? STR.liveDotLive : STR.dataAge + " " + ageLabel
      );
      if (this._updatedLabel) {
        this._updatedLabel.textContent = STR.dataAge + " " + ageLabel;
      }
    }

    // human relative age from a duration in ms
    _relAge(ms) {
      if (!isNum(ms) || ms < 0) return STR.never;
      const sec = Math.round(ms / 1000);
      if (sec < 60) return sec + "s ago";
      const min = Math.round(sec / 60);
      if (min < 60) return min + " min ago";
      const hr = Math.round(min / 60);
      if (hr < 24) return hr + "h ago";
      const d = Math.round(hr / 24);
      return d + "d ago";
    }

    // ---------------------------------------------------------------- //
    // Formatters
    // ---------------------------------------------------------------- //
    _kw(v) {
      // watts -> "1.2 kW"
      if (!isNum(v)) return "—";
      return NF1.format(v / 1000) + " kW";
    }
    _kwAbs(v) {
      if (!isNum(v)) return "—";
      return NF1.format(Math.abs(v) / 1000) + " kW";
    }
    _kwShort(v) {
      // compact for flow row, no unit suffix gap: "3.2kW"
      if (!isNum(v)) return "—";
      return NF1.format(Math.abs(v) / 1000) + "kW";
    }
    _kwh(v) {
      if (!isNum(v)) return "—";
      return NF1.format(v);
    }
    _pct(v) {
      if (!isNum(v)) return "—";
      return NF0.format(v) + "%";
    }

    _prettyId(id) {
      if (!id || typeof id !== "string") return "Inverter";
      // Prettify a model/id string: split on - _ and Title-Case tokens.
      const cleaned = id.replace(/[-_]+/g, " ").trim();
      if (!cleaned) return id;
      return cleaned
        .split(" ")
        .map((t) => (t.length <= 3 ? t.toUpperCase() : t.charAt(0).toUpperCase() + t.slice(1)))
        .join(" ");
    }

    // ---------------------------------------------------------------- //
    // Live
    // ---------------------------------------------------------------- //
    async _loadLive() {
      try {
        const data = await this._call("svitgrid/live");
        const inverters =
          data && Array.isArray(data.inverters) ? data.inverters : [];

        if (inverters.length > 0) {
          this._lastLiveInverterId = inverters[0].inverterId || null;
        }

        if (!this._liveSec) return true;

        if (inverters.length === 0) {
          this._invIdsKey = null;
          this._invNodes = {};
          this._freshestAgeMs = null;
          this._liveSec.className = "";
          this._liveSec.innerHTML = "";
          const empty = document.createElement("div");
          empty.className = "history-empty";
          empty.textContent = STR.noInverters;
          this._liveSec.appendChild(empty);
          return false;
        }

        const now = Date.now();
        let freshest = Infinity;

        // Rebuild grid only when the SET of ids changes (sort so order doesn't matter).
        const idsKey = inverters.map((i) => i.inverterId || "?").sort().join("|");
        if (idsKey !== this._invIdsKey) {
          this._invIdsKey = idsKey;
          this._invNodes = {};
          this._liveSec.className = "";
          this._liveSec.innerHTML = "";
          const grid = document.createElement("div");
          grid.className = "inverter-grid";
          for (const inv of inverters) {
            const id = inv.inverterId || "?";
            const node = this._buildInvCard(id);
            this._invNodes[id] = node;
            grid.appendChild(node.card);
          }
          this._liveSec.appendChild(grid);
        }

        // Mutate values in place.
        for (const inv of inverters) {
          const id = inv.inverterId || "?";
          const node = this._invNodes[id];
          if (!node) continue;
          const ageMs = isNum(inv.ts) || (typeof inv.ts === "string")
            ? now - new Date(inv.ts).getTime()
            : null;
          if (isNum(ageMs) && ageMs < freshest) freshest = ageMs;
          this._updateInvCard(node, inv.payload, ageMs);
        }

        this._freshestAgeMs = isFinite(freshest) ? freshest : null;
        return true;
      } catch (err) {
        if (this._liveSec) {
          this._liveSec.className = "section-error";
          this._liveSec.textContent =
            "Live data unavailable: " +
            (err && err.message ? err.message : String(err));
        }
        this._freshestAgeMs = null;
        return false;
      }
    }

    _buildInvCard(id) {
      const card = document.createElement("div");
      card.className = "inv-card";

      const head = document.createElement("div");
      head.className = "inv-card-head";
      const title = document.createElement("div");
      title.className = "inv-card-title";
      title.textContent = this._prettyId(id);
      title.title = id; // hover shows raw id
      const badge = document.createElement("div");
      badge.className = "stale-badge";
      badge.style.display = "none";
      head.appendChild(title);
      head.appendChild(badge);
      card.appendChild(head);

      // Flow row
      const flow = document.createElement("div");
      flow.className = "flow-row";
      flow.setAttribute("aria-label", "Energy flow summary");
      card.appendChild(flow);

      // Metric rows: PV (headline), Load (headline), Battery, Grid
      const refs = { card, badge, flow };
      refs.pv = this._buildMetricRow(card, "solar", STR.pv, true);
      refs.load = this._buildMetricRow(card, "home", STR.load, true);
      refs.battery = this._buildMetricRow(card, "battery", STR.battery, false);
      refs.grid = this._buildMetricRow(card, "grid", STR.grid, false);
      return refs;
    }

    _buildMetricRow(card, icon, label, headline) {
      const row = document.createElement("div");
      row.className = "inv-row" + (headline ? " headline" : "");
      const lbl = document.createElement("span");
      lbl.className = "inv-row-label";
      lbl.innerHTML = svgIcon(icon); // static glyph markup only
      const lblText = document.createElement("span");
      lblText.textContent = label;
      lbl.appendChild(lblText);
      const valWrap = document.createElement("span");
      const val = document.createElement("span");
      val.className = "inv-row-value";
      val.textContent = "—";
      const sub = document.createElement("span");
      sub.className = "inv-row-sub";
      valWrap.appendChild(val);
      valWrap.appendChild(sub);
      row.appendChild(lbl);
      row.appendChild(valWrap);
      card.appendChild(row);
      return { val, sub };
    }

    _updateInvCard(node, payload, ageMs) {
      const p = payload && typeof payload === "object" ? payload : {};

      // Staleness
      const stale = isNum(ageMs) && ageMs > STALE_MS;
      node.card.classList.toggle("stale", stale);
      if (stale) {
        node.badge.style.display = "";
        node.badge.textContent = "⚠ " + STR.stale + " · " + this._relAge(ageMs);
      } else {
        node.badge.style.display = "none";
        node.badge.textContent = "";
      }

      const pv = isNum(p.pvPower) ? p.pvPower : null;
      const load = isNum(p.loadPower) ? p.loadPower : null;
      const soc = isNum(p.batterySoc) ? p.batterySoc : null;
      const batt = isNum(p.batteryPower) ? p.batteryPower : null;
      const grid = isNum(p.gridPower) ? p.gridPower : null;

      // PV
      node.pv.val.textContent = this._kw(pv);
      node.pv.sub.textContent = "";

      // Load
      node.load.val.textContent = this._kw(load);
      node.load.sub.textContent = "";

      // Battery: SOC + signed power + word
      if (soc != null) {
        node.battery.val.textContent = this._pct(soc);
      } else {
        node.battery.val.textContent = "—";
      }
      if (batt != null) {
        const charging = batt >= 0;
        const sign = charging ? "+" : "−";
        node.battery.sub.textContent =
          sign + this._kwAbs(batt) + " " + (charging ? STR.charging : STR.discharging);
      } else {
        node.battery.sub.textContent = "";
      }

      // Grid: magnitude + arrow + word
      if (grid != null) {
        const importing = grid > 0;
        node.grid.val.textContent = this._kwAbs(grid);
        node.grid.sub.textContent =
          (importing ? "↓ " : "↑ ") + (importing ? STR.importing : STR.exporting);
      } else {
        node.grid.val.textContent = "—";
        node.grid.sub.textContent = "";
      }

      this._updateFlowRow(node.flow, { pv, load, batt, grid });
    }

    _updateFlowRow(flow, v) {
      flow.innerHTML = ""; // rebuild glyph segments (static markup + textContent vals)
      const segs = [];

      // ☀ PV
      if (v.pv != null) {
        segs.push(this._flowSeg("solar", STR.pv, this._kwShort(v.pv), "", v.pv > 50 ? "ok" : ""));
      }
      // → House
      if (v.load != null) {
        segs.push(this._flowSeg("home", STR.load, this._kwShort(v.load), "→", ""));
      }
      // Battery (charge = ok/green; discharge = neutral)
      if (v.batt != null) {
        const charging = v.batt >= 0;
        const arrow = charging ? "↑" : "↓";
        segs.push(
          this._flowSeg(
            "battery",
            "",
            arrow + this._kwShort(v.batt),
            "│",
            charging ? "ok" : ""
          )
        );
      }
      // Grid (export = ok/green; import = warn)
      if (v.grid != null) {
        const importing = v.grid > 0;
        const arrow = importing ? "↓" : "↑";
        segs.push(
          this._flowSeg(
            "grid",
            "",
            arrow + this._kwShort(v.grid),
            "│",
            importing ? "warn" : "ok"
          )
        );
      }

      if (segs.length === 0) {
        flow.style.display = "none";
        return;
      }
      flow.style.display = "";
      for (const s of segs) flow.appendChild(s);
    }

    _flowSeg(icon, label, valText, sepGlyph, tone) {
      const wrap = document.createElement("span");
      wrap.className = "flow-seg" + (tone ? " " + tone : "");

      if (sepGlyph === "→" || sepGlyph === "│") {
        const sep = document.createElement("span");
        sep.className = sepGlyph === "→" ? "flow-arrow" : "flow-sep";
        sep.textContent = sepGlyph;
        // separators render as their own inline element before the segment
        const holder = document.createDocumentFragment();
        holder.appendChild(sep);
        holder.appendChild(wrap);
        // fill wrap below, return holder via a wrapper span
        this._fillSeg(wrap, icon, label, valText);
        const outer = document.createElement("span");
        outer.style.display = "inline-flex";
        outer.style.alignItems = "center";
        outer.style.gap = "4px";
        outer.appendChild(sep);
        outer.appendChild(wrap);
        return outer;
      }

      this._fillSeg(wrap, icon, label, valText);
      return wrap;
    }

    _fillSeg(wrap, icon, label, valText) {
      if (icon) {
        const ic = document.createElement("span");
        ic.style.display = "inline-flex";
        ic.innerHTML = svgIcon(icon); // static
        wrap.appendChild(ic);
      }
      if (label) {
        const l = document.createElement("span");
        l.textContent = label;
        wrap.appendChild(l);
      }
      const val = document.createElement("span");
      val.className = "flow-val";
      val.textContent = valText;
      wrap.appendChild(val);
    }

    // ---------------------------------------------------------------- //
    // Today
    // ---------------------------------------------------------------- //
    async _loadToday() {
      try {
        const data = await this._call("svitgrid/today");
        const inverters =
          data && Array.isArray(data.inverters) ? data.inverters : [];

        const sum = (field) => {
          let total = null;
          for (const inv of inverters) {
            const v =
              inv.energy && isNum(inv.energy[field]) ? inv.energy[field] : null;
            if (v != null) total = (total || 0) + v;
          }
          return total;
        };

        const tiles = [
          { label: STR.generated, value: this._kwh(sum("dailyPvEnergy")) },
          { label: STR.consumed, value: this._kwh(sum("dailyLoadEnergy")) },
          { label: STR.imported, value: this._kwh(sum("dailyGridImportEnergy")) },
          { label: STR.exported, value: this._kwh(sum("dailyGridExportEnergy")) },
        ];

        if (!this._todaySec) return true;

        // Build once, then mutate values.
        if (!this._todayRefs || this._todayRefs.length !== tiles.length) {
          this._todaySec.className = "";
          this._todaySec.innerHTML = "";
          const grid = document.createElement("div");
          grid.className = "today-grid";
          this._todayRefs = [];
          for (const t of tiles) {
            const tile = document.createElement("div");
            tile.className = "today-tile";
            const val = document.createElement("div");
            val.className = "today-tile-value";
            val.textContent = t.value;
            const unit = document.createElement("div");
            unit.className = "today-tile-unit";
            unit.textContent = STR.kwh;
            const lbl = document.createElement("div");
            lbl.className = "today-tile-label";
            lbl.textContent = t.label;
            tile.appendChild(val);
            tile.appendChild(unit);
            tile.appendChild(lbl);
            grid.appendChild(tile);
            this._todayRefs.push(val);
          }
          this._todaySec.appendChild(grid);
        } else {
          for (let i = 0; i < tiles.length; i++) {
            this._todayRefs[i].textContent = tiles[i].value;
          }
        }
        return true;
      } catch (err) {
        this._todayRefs = null;
        if (this._todaySec) {
          this._todaySec.className = "section-error";
          this._todaySec.textContent =
            "Today data unavailable: " +
            (err && err.message ? err.message : String(err));
        }
        return false;
      }
    }

    // ---------------------------------------------------------------- //
    // History (sum dailyPvEnergy across all inverters per day)
    // ---------------------------------------------------------------- //
    async _loadHistory() {
      try {
        if (!this._historySec) return true;

        if (!this._lastLiveInverterId) {
          this._historyKey = null;
          this._historySec.className = "";
          this._historySec.innerHTML = "";
          const empty = document.createElement("div");
          empty.className = "history-empty";
          empty.textContent = STR.historyEmpty;
          this._historySec.appendChild(empty);
          return true;
        }

        const today = new Date();
        const start30 = new Date(today);
        start30.setDate(today.getDate() - 30);
        const endStr = this._dateStr(today);
        const startStr = this._dateStr(start30);

        // The history endpoint is per-inverter; we sum dailyPvEnergy across
        // all live inverters per day for parity with the Today tiles.
        const ids = Object.keys(this._invNodes || {});
        const idList = ids.length ? ids : [this._lastLiveInverterId];

        const byDay = new Map(); // day -> summed kWh
        for (const id of idList) {
          let data;
          try {
            data = await this._call(
              "svitgrid/history?inverter_id=" +
                encodeURIComponent(id) +
                "&start=" +
                startStr +
                "&end=" +
                endStr
            );
          } catch (_) {
            continue; // one inverter failing shouldn't blank the chart
          }
          const days = data && Array.isArray(data.days) ? data.days : [];
          for (const d of days) {
            const day = d.day;
            const v =
              d.energy && isNum(d.energy.dailyPvEnergy)
                ? d.energy.dailyPvEnergy
                : 0;
            if (typeof day !== "string") continue;
            byDay.set(day, (byDay.get(day) || 0) + v);
          }
        }

        const series = Array.from(byDay.entries())
          .map(([day, kwh]) => ({ day, kwh }))
          .sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));

        this._renderHistory(series);
        return true;
      } catch (err) {
        if (this._historySec) {
          this._historySec.className = "section-error";
          this._historySec.textContent =
            "History unavailable: " +
            (err && err.message ? err.message : String(err));
        }
        return false;
      }
    }

    _renderHistory(series) {
      if (!this._historySec) return;
      this._historySec.className = "";
      this._historySec.innerHTML = "";

      if (series.length === 0) {
        const empty = document.createElement("div");
        empty.className = "history-empty";
        empty.textContent = STR.historyEmpty;
        this._historySec.appendChild(empty);
        return;
      }

      let maxVal = 0;
      for (const s of series) if (s.kwh > maxVal) maxVal = s.kwh;

      if (maxVal === 0) {
        const empty = document.createElement("div");
        empty.className = "history-empty";
        empty.textContent = STR.historyEmpty;
        this._historySec.appendChild(empty);
        return;
      }

      const wrap = document.createElement("div");
      wrap.className = "history-chart";

      const title = document.createElement("div");
      title.style.fontSize = "12px";
      title.style.fontWeight = "600";
      title.style.color = "var(--sg-text-2)";
      title.style.marginBottom = "12px";
      title.textContent = STR.historyTitle;
      wrap.appendChild(title);

      const area = document.createElement("div");
      area.className = "chart-area";

      // Y axis: max, 50%, 0
      const yAxis = document.createElement("div");
      yAxis.className = "y-axis";
      const yMax = document.createElement("span");
      yMax.textContent = this._kwh(maxVal);
      const yMid = document.createElement("span");
      yMid.textContent = this._kwh(maxVal / 2);
      const yZero = document.createElement("span");
      yZero.textContent = "0";
      yAxis.appendChild(yMax);
      yAxis.appendChild(yMid);
      yAxis.appendChild(yZero);
      area.appendChild(yAxis);

      const plot = document.createElement("div");
      plot.className = "chart-plot";

      const chart = document.createElement("div");
      chart.className = "bar-chart";

      // 50% gridline
      const gl = document.createElement("div");
      gl.className = "gridline";
      gl.style.top = "50%";
      chart.appendChild(gl);

      // Tooltip
      const tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      this._tooltip = tooltip;

      const lastIdx = series.length - 1;
      let tallestIdx = 0;
      for (let i = 1; i < series.length; i++) {
        if (series[i].kwh > series[tallestIdx].kwh) tallestIdx = i;
      }

      for (let i = 0; i < series.length; i++) {
        const s = series[i];
        const pct = maxVal > 0 ? (s.kwh / maxVal) * 100 : 0;

        const col = document.createElement("div");
        col.className = "bar-col";

        // Value caps on tallest + most-recent
        if (i === tallestIdx || i === lastIdx) {
          const cap = document.createElement("div");
          cap.className = "bar-cap";
          cap.textContent = this._kwh(s.kwh);
          col.appendChild(cap);
        }

        const bar = document.createElement("div");
        bar.className = "bar";
        bar.style.height = Math.max(pct, 2) + "%";
        const label = this._localDate(s.day) + ": " + this._kwh(s.kwh) + " kWh";
        bar.setAttribute("role", "img");
        bar.setAttribute("aria-label", label);
        bar.setAttribute("tabindex", "0");
        const showTip = (clientX) => {
          tooltip.textContent = label;
          tooltip.classList.add("show");
          const plotRect = plot.getBoundingClientRect();
          const barRect = bar.getBoundingClientRect();
          const x = (clientX != null ? clientX : barRect.left + barRect.width / 2) - plotRect.left + plot.scrollLeft;
          tooltip.style.left = x + "px";
          tooltip.style.top = (barRect.top - plotRect.top) + "px";
        };
        const hideTip = () => tooltip.classList.remove("show");
        bar.addEventListener("click", (e) => showTip(e.clientX));
        bar.addEventListener("mouseenter", (e) => showTip(e.clientX));
        bar.addEventListener("mouseleave", hideTip);
        bar.addEventListener("focus", () => showTip(null));
        bar.addEventListener("blur", hideTip);

        col.appendChild(bar);
        chart.appendChild(col);
      }

      plot.appendChild(chart);
      plot.appendChild(tooltip);
      area.appendChild(plot);
      wrap.appendChild(area);

      // X axis labels (every ~5th + last) aligned under bars
      const axis = document.createElement("div");
      axis.className = "bar-axis";
      for (let i = 0; i < series.length; i++) {
        const lbl = document.createElement("div");
        lbl.className = "bar-label";
        if (i % 5 === 0 || i === lastIdx) {
          lbl.textContent = this._localDate(series[i].day);
        }
        axis.appendChild(lbl);
      }
      wrap.appendChild(axis);

      this._historySec.appendChild(wrap);
    }

    // ---------------------------------------------------------------- //
    // Sync status
    // ---------------------------------------------------------------- //
    async _loadSync() {
      try {
        const data = await this._call("svitgrid/sync-status");
        const counts =
          data && data.counts && typeof data.counts === "object"
            ? data.counts
            : {};
        const sent = isNum(counts.sent) ? counts.sent : 0;
        const pending = isNum(counts.pending) ? counts.pending : 0;
        const failed = isNum(counts.failed) ? counts.failed : 0;
        const skipped = isNum(counts.skipped) ? counts.skipped : 0;

        let lastAge = STR.never;
        if (data && data.last_sent_ts) {
          const t = new Date(data.last_sent_ts).getTime();
          if (isNum(t)) lastAge = this._relAge(Date.now() - t);
        }

        if (!this._syncFooter) return true;

        const hasIssue = pending + failed > 0;
        this._syncFooter.className = "sync-footer " + (hasIssue ? "issue" : "ok");
        this._syncFooter.innerHTML = "";

        const lead = document.createElement("span");
        lead.className = "sync-lead";
        const detail = document.createElement("span");
        detail.className = "sync-detail";

        if (hasIssue) {
          lead.textContent =
            "⚠ " + pending + " " + STR.pending + " · " + failed + " " + STR.failed;
          // skipped kept secondary (tooltip only)
          detail.textContent = sent + " synced · " + STR.lastSent + " " + lastAge;
          if (skipped > 0) detail.title = skipped + " " + STR.skipped;
        } else {
          lead.textContent = "✓ " + STR.syncedAll;
          detail.textContent = "· " + STR.lastSent + " " + lastAge;
          if (skipped > 0) detail.title = skipped + " " + STR.skipped;
        }

        this._syncFooter.appendChild(lead);
        this._syncFooter.appendChild(detail);
        return true;
      } catch (err) {
        if (this._syncFooter) {
          this._syncFooter.className = "sync-footer";
          this._syncFooter.innerHTML = "";
          const span = document.createElement("span");
          span.className = "sync-detail";
          span.textContent =
            "Sync status unavailable: " +
            (err && err.message ? err.message : String(err));
          this._syncFooter.appendChild(span);
        }
        return false;
      }
    }

    // ---------------------------------------------------------------- //
    // Date helpers
    // ---------------------------------------------------------------- //
    _localDate(dateStr) {
      if (!dateStr) return "—";
      try {
        return new Date(dateStr).toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
        });
      } catch (_) {
        return dateStr;
      }
    }

    _dateStr(d) {
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return y + "-" + m + "-" + day;
    }
  }

  if (!customElements.get("svitgrid-panel")) {
    customElements.define("svitgrid-panel", SvitgridPanel);
  }
})();
