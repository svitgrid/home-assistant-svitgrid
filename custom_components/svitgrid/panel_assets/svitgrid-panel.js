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

  // Cadence-aware staleness: thresholds scale with the inverter's observed
  // reporting interval (intervalS from the live snapshot) rather than being
  // fixed.  Floors prevent false-stale on very fast cadences.
  const STALE_FACTOR = 2.5;           // stale once age > factor × interval
  const FRESH_FACTOR = 1.5;           // header dot green below factor × interval
  const STALE_FLOOR_MS = 120 * 1000;  // never flag stale before 2 min
  const FRESH_FLOOR_MS = 90 * 1000;   // dot green at least up to 90 s
  const DEFAULT_INTERVAL_MS = 300 * 1000; // fallback when intervalS unknown

  function thresholdsFor(intervalS) {
    const iv =
      typeof intervalS === "number" && isFinite(intervalS) && intervalS > 0
        ? intervalS * 1000
        : DEFAULT_INTERVAL_MS;
    return {
      staleAfterMs: Math.max(iv * STALE_FACTOR, STALE_FLOOR_MS),
      freshUnderMs: Math.max(iv * FRESH_FACTOR, FRESH_FLOOR_MS),
    };
  }

  // Future-i18n string table (English for v1). Keep all user copy here so a
  // later `uk` pass swaps one object.
  const STR = {
    title: "Svitgrid",
    live: "Live",
    today: "Today",
    history: "Energy — last 30 days",
    historyEmpty: "No data recorded in this period.",
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
    batteryCharged: "Battery charged",
    batteryDischarged: "Battery discharged",
    generator: "Generator",
    losses: "Losses",
    current: "Current",
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
    system: "System",
    // History controls
    histDays7: "7d",
    histDays30: "30d",
    histDays90: "90d",
    histDays365: "365d",
    histMetricGenerated: "Generated",
    histMetricConsumed: "Consumed",
    histMetricImported: "Imported",
    histMetricExported: "Exported",
    histMetricBattCharged: "Batt charged",
    histMetricBattDischarged: "Batt discharged",
    histMetricLosses: "Losses",
    histAriaRange: "Select history range",
    histAriaMetric: "Select energy metric",
    histAriaMode: "Select history mode",
    histModeEnergy: "Energy",
    histModeSources: "Sources",
    histModeTrends: "Trends",
    histSourcesPv: "Solar (PV)",
    histSourcesImport: "Grid import",
    histSourcesBattery: "Battery discharge",
    histSourcesTitle: "Energy sources",
    histSourcesTotal: "Total",
    histTrendsTitle: "Trends",
    histTrendsNoData: "No trend data for this metric in the selected period.",
    histAriaTrendMetric: "Select trend metric",
    histTrendMetricSoc: "Battery SOC",
    histTrendMetricInvTemp: "Inverter temp",
    histTrendMetricBattTemp: "Battery temp",
    histTrendMetricFreq: "Grid frequency",
    histTrendUnitPct: "%",
    histTrendUnitDegC: "°C",
    histTrendUnitHz: "Hz",
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

    /* History control bar (range + metric chips) */
    .hist-controls {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--sp-2) var(--sp-3);
      margin-bottom: var(--sp-3);
    }
    .hist-chip-group {
      display: flex;
      gap: var(--sp-1);
      flex-wrap: wrap;
    }
    .hist-chip {
      font-family: inherit;
      font-size: 12px;
      font-weight: 600;
      color: var(--sg-text-2);
      background: color-mix(in srgb, var(--sg-divider) 60%, transparent);
      border: 1px solid var(--sg-divider);
      border-radius: 16px;
      padding: 3px 10px;
      cursor: pointer;
      transition: background 0.12s ease, color 0.12s ease;
      line-height: 1.4;
    }
    .hist-chip:hover { background: color-mix(in srgb, var(--accent) 18%, var(--sg-card-bg)); }
    .hist-chip[aria-pressed="true"] {
      background: color-mix(in srgb, var(--accent) 22%, var(--sg-card-bg));
      border-color: var(--accent);
      color: var(--sg-text);
    }
    .hist-chip:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    @media (prefers-reduced-motion: reduce) {
      .hist-chip { transition: none; }
    }
    .hist-sep {
      width: 1px;
      height: 18px;
      background: var(--sg-divider);
      flex-shrink: 0;
      align-self: center;
    }

    /* Sources stacked-bar segments */
    .bar-seg-pv      { background: var(--accent); }
    .bar-seg-import  { background: var(--info-color, #1565C0); }
    .bar-seg-battery { background: var(--sg-ok); }
    .stacked-bar {
      width: 100%;
      display: flex;
      flex-direction: column-reverse;
      align-items: stretch;
      border-radius: 2px 2px 0 0;
      overflow: hidden;
      min-height: 2px;
      cursor: pointer;
    }
    .stacked-bar:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    /* Sources legend */
    .hist-legend {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sp-1) var(--sp-3);
      margin-top: var(--sp-3);
      font-size: 12px;
      color: var(--sg-text-2);
    }
    .hist-legend-item {
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .hist-legend-swatch {
      width: 10px;
      height: 10px;
      border-radius: 2px;
      flex-shrink: 0;
    }

    /* Trends line-chart SVG */
    .line-svg {
      display: block;
      width: 100%;
      height: 140px;
      overflow: visible;
    }
    .line-path {
      fill: none;
      stroke: var(--accent);
      stroke-width: 2;
      stroke-linejoin: round;
      stroke-linecap: round;
    }
    .line-gridline {
      stroke: var(--sg-divider);
      stroke-dasharray: 3 3;
      stroke-width: 1;
    }
    .line-dot {
      fill: var(--accent);
      stroke: var(--sg-card-bg);
      stroke-width: 2;
      cursor: pointer;
    }
    .line-dot:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
    @media (prefers-reduced-motion: reduce) {
      .line-path { stroke-dasharray: none; }
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

    /* Detail section */
    .detail-toggle {
      display: flex;
      align-items: center;
      gap: var(--sp-1);
      width: 100%;
      background: none;
      border: none;
      border-top: 1px solid var(--sg-divider);
      margin-top: var(--sp-2);
      padding: var(--sp-2) 0 0;
      cursor: pointer;
      font-family: inherit;
      font-size: 12px;
      color: var(--sg-text-2);
      text-align: left;
    }
    .detail-toggle:hover { color: var(--sg-text); }
    .detail-toggle:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
      border-radius: 2px;
    }
    .detail-chevron {
      display: inline-block;
      font-size: 10px;
      transition: transform 0.2s ease;
      flex-shrink: 0;
    }
    .detail-toggle[aria-expanded="true"] .detail-chevron {
      transform: rotate(90deg);
    }
    @media (prefers-reduced-motion: reduce) {
      .detail-chevron { transition: none; }
    }
    .detail-region {
      display: none;
      padding-top: var(--sp-2);
    }
    .detail-region.open {
      display: block;
    }
    .detail-group {
      margin-bottom: var(--sp-2);
    }
    .detail-group-label {
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: var(--sg-muted);
      margin-bottom: 3px;
    }
    .detail-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .detail-chip {
      font-size: 11px;
      color: var(--sg-text);
      background: color-mix(in srgb, var(--sg-divider) 50%, transparent);
      border-radius: 4px;
      padding: 1px 6px;
      font-variant-numeric: tabular-nums;
    }
    .detail-batt-val {
      font-size: 12px;
      color: var(--sg-text);
      font-variant-numeric: tabular-nums;
    }
    .detail-phase-table {
      display: grid;
      font-size: 11px;
      font-variant-numeric: tabular-nums;
      column-gap: var(--sp-2);
      row-gap: 2px;
    }
    .detail-phase-table.cols-1 { grid-template-columns: auto 1fr; }
    .detail-phase-table.cols-2 { grid-template-columns: auto repeat(2, 1fr); }
    .detail-phase-table.cols-3 { grid-template-columns: auto repeat(3, 1fr); }
    .detail-phase-hdr {
      color: var(--sg-muted);
      font-weight: 700;
    }
    .detail-phase-cell {
      color: var(--sg-text);
    }
    .detail-phase-row-label {
      color: var(--sg-text-2);
    }
    .detail-footnote {
      font-size: 10px;
      color: var(--sg-muted);
      margin-top: var(--sp-1);
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
  const NF2 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

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
      this._freshestAgeMs = null;    // age (ms) of the most-recent inverter reading
      this._freshestIntervalS = null; // intervalS of the freshest inverter (for header dot)
      this._todayRefs = null;      // cached DOM refs for today-tile values
      this._detailOpen = {};       // inverterId -> bool (open/closed state, survives refresh)

      // History controls (SP-C foundation — extended by Tasks 3-5)
      this._histRangeDays = 30;            // 7 | 30 | 90 | 365
      this._histMode = "energy";           // "energy" | "sources" | "trends"
      this._histMetric = "dailyPvEnergy";  // active energy field key
      this._trendMetric = "batterySoc";    // active trend metric key
      this._histKey = null;                // cache key: range|metric|data changes trigger re-render
      this._histSec = null;                // h2 section title node (for dynamic text update)

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
      this._histKey = null;
      this._histMode = "energy";
      this._histSec = null;

      this._liveSec = this._addSection(root, STR.live, "live-region");
      this._fillSkeleton(this._liveSec, "live");

      this._todaySec = this._addSection(root, STR.today, "today-region");
      this._fillSkeleton(this._todaySec, "today");

      const histResult = this._addSectionWithTitle(root, this._histSectionTitle(), "history-region");
      this._histSec = histResult.title;
      this._historySec = histResult.sec;
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

    // Like _addSection but returns both the h2 title node and the section div
    // so the caller can update the title text dynamically.
    _addSectionWithTitle(root, titleText, ariaLabel) {
      const h2 = document.createElement("h2");
      h2.className = "section-title";
      h2.textContent = titleText;
      root.appendChild(h2);

      const sec = document.createElement("div");
      sec.setAttribute("role", "group");
      sec.setAttribute("aria-label", ariaLabel);
      root.appendChild(sec);
      return { title: h2, sec };
    }

    // Compute the dynamic history section title from current range/metric state.
    _histSectionTitle() {
      if (this._histMode === "sources") {
        return STR.histSourcesTitle + " — last " + this._histRangeDays + " days";
      }
      if (this._histMode === "trends") {
        const trendLabels = {
          batterySoc:           STR.histTrendMetricSoc,
          inverterTemperature:  STR.histTrendMetricInvTemp,
          batteryTemperature:   STR.histTrendMetricBattTemp,
          gridFrequency:        STR.histTrendMetricFreq,
        };
        const label = trendLabels[this._trendMetric] || STR.histTrendsTitle;
        return label + " — last " + this._histRangeDays + " days";
      }
      const metricLabels = {
        dailyPvEnergy:              STR.histMetricGenerated,
        dailyLoadEnergy:            STR.histMetricConsumed,
        dailyGridImportEnergy:      STR.histMetricImported,
        dailyGridExportEnergy:      STR.histMetricExported,
        dailyBatteryChargeEnergy:   STR.histMetricBattCharged,
        dailyBatteryDischargeEnergy: STR.histMetricBattDischarged,
        dailyLossesEnergy:          STR.histMetricLosses,
      };
      const label = metricLabels[this._histMetric] || STR.histMetricGenerated;
      return label + " — last " + this._histRangeDays + " days";
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
      this._histSec = null;
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
      const { freshUnderMs } = thresholdsFor(this._freshestIntervalS);
      if (ageMs < freshUnderMs) {
        this._liveDot.classList.add("fresh");
      } else {
        this._liveDot.classList.add("aging");
      }
      const ageLabel = this._relAge(ageMs);
      this._liveDot.setAttribute(
        "aria-label",
        ageMs < freshUnderMs ? STR.liveDotLive : STR.dataAge + " " + ageLabel
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
          this._freshestIntervalS = null;
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
        let freshestIntervalS = null;
        for (const inv of inverters) {
          const id = inv.inverterId || "?";
          const node = this._invNodes[id];
          if (!node) continue;
          const ageMs = isNum(inv.ts) || (typeof inv.ts === "string")
            ? now - new Date(inv.ts).getTime()
            : null;
          if (isNum(ageMs) && ageMs < freshest) {
            freshest = ageMs;
            freshestIntervalS = (typeof inv.intervalS === "number" && isFinite(inv.intervalS))
              ? inv.intervalS : null;
          }
          this._updateInvCard(node, inv.payload, ageMs, inv.intervalS);
        }

        this._freshestAgeMs = isFinite(freshest) ? freshest : null;
        this._freshestIntervalS = freshestIntervalS;
        return true;
      } catch (err) {
        if (this._liveSec) {
          this._liveSec.className = "section-error";
          this._liveSec.textContent =
            "Live data unavailable: " +
            (err && err.message ? err.message : String(err));
        }
        this._freshestAgeMs = null;
        this._freshestIntervalS = null;
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

      // Detail section (built once, mutated in place on each poll)
      const detail = this._buildDetail(id);
      card.appendChild(detail.wrap);
      refs.detail = detail.refs;

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

    // ---------------------------------------------------------------- //
    // Detail section — build once, mutate in place
    // ---------------------------------------------------------------- //
    _buildDetail(inverterId) {
      const safeId = inverterId.replace(/[^a-zA-Z0-9_-]/g, "_");
      const regionId = "detail-" + safeId;
      const isOpen = !!this._detailOpen[inverterId];

      const wrap = document.createElement("div");

      // Toggle button
      const toggle = document.createElement("button");
      toggle.className = "detail-toggle";
      toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
      toggle.setAttribute("aria-controls", regionId);

      const chevron = document.createElement("span");
      chevron.className = "detail-chevron";
      chevron.textContent = "▸";
      chevron.setAttribute("aria-hidden", "true");
      toggle.appendChild(chevron);

      const toggleLabel = document.createElement("span");
      toggleLabel.textContent = "Details";
      toggle.appendChild(toggleLabel);

      toggle.addEventListener("click", () => {
        const opening = toggle.getAttribute("aria-expanded") !== "true";
        this._detailOpen[inverterId] = opening;
        toggle.setAttribute("aria-expanded", opening ? "true" : "false");
        region.classList.toggle("open", opening);
      });

      wrap.appendChild(toggle);

      // Detail region
      const region = document.createElement("div");
      region.className = "detail-region" + (isOpen ? " open" : "");
      region.setAttribute("role", "region");
      region.setAttribute("aria-label", "Inverter details");
      region.id = regionId;

      // --- Solar strings group ---
      const pvGroup = document.createElement("div");
      pvGroup.className = "detail-group";
      const pvLabel = document.createElement("div");
      pvLabel.className = "detail-group-label";
      pvLabel.textContent = "Solar strings";
      const pvChips = document.createElement("div");
      pvChips.className = "detail-chips";
      pvGroup.appendChild(pvLabel);
      pvGroup.appendChild(pvChips);
      region.appendChild(pvGroup);

      // --- Battery group (voltage + temperature + current) ---
      const battGroup = document.createElement("div");
      battGroup.className = "detail-group";
      const battLabel = document.createElement("div");
      battLabel.className = "detail-group-label";
      battLabel.textContent = "Battery";
      const battVal = document.createElement("div");
      battVal.className = "detail-batt-val";
      const battTemp = document.createElement("div");
      battTemp.className = "detail-batt-val";
      const battCurrent = document.createElement("div");
      battCurrent.className = "detail-batt-val";
      battGroup.appendChild(battLabel);
      battGroup.appendChild(battVal);
      battGroup.appendChild(battTemp);
      battGroup.appendChild(battCurrent);
      region.appendChild(battGroup);

      // --- System group (inverter temperature + grid frequency) ---
      const sysGroup = document.createElement("div");
      sysGroup.className = "detail-group";
      const sysLabel = document.createElement("div");
      sysLabel.className = "detail-group-label";
      sysLabel.textContent = STR.system;
      const sysInvTemp = document.createElement("div");
      sysInvTemp.className = "detail-batt-val";
      const sysGridFreq = document.createElement("div");
      sysGridFreq.className = "detail-batt-val";
      const sysLoadFreq = document.createElement("div");
      sysLoadFreq.className = "detail-batt-val";
      sysGroup.appendChild(sysLabel);
      sysGroup.appendChild(sysInvTemp);
      sysGroup.appendChild(sysGridFreq);
      sysGroup.appendChild(sysLoadFreq);
      region.appendChild(sysGroup);

      // --- Grid per-phase group ---
      const gridGroup = document.createElement("div");
      gridGroup.className = "detail-group";
      const gridLabel = document.createElement("div");
      gridLabel.className = "detail-group-label";
      gridLabel.textContent = "Grid (per phase)";
      const gridTable = document.createElement("div");
      gridTable.className = "detail-phase-table";
      gridGroup.appendChild(gridLabel);
      gridGroup.appendChild(gridTable);
      region.appendChild(gridGroup);

      // --- Load per-phase group ---
      const loadGroup = document.createElement("div");
      loadGroup.className = "detail-group";
      const loadLabel = document.createElement("div");
      loadLabel.className = "detail-group-label";
      loadLabel.textContent = "Load (per phase)";
      const loadTable = document.createElement("div");
      loadTable.className = "detail-phase-table";
      loadGroup.appendChild(loadLabel);
      loadGroup.appendChild(loadTable);
      region.appendChild(loadGroup);

      // --- Reading quality footnote ---
      const footnote = document.createElement("div");
      footnote.className = "detail-footnote";
      region.appendChild(footnote);

      wrap.appendChild(region);

      const refs = {
        toggle, region,
        pvGroup, pvChips,
        battGroup, battVal, battTemp, battCurrent,
        sysGroup, sysInvTemp, sysGridFreq, sysLoadFreq,
        gridGroup, gridTable,
        loadGroup, loadTable,
        footnote,
      };

      return { wrap, refs };
    }

    _updateDetail(refs, payload) {
      const p = payload && typeof payload === "object" ? payload : {};

      // Field name arrays — kept as literals so grepping / smoke-tests find them.
      // pvPower1..pvPower8 (extendable); gridPowerL1/gridPowerL2/gridPowerL3;
      // gridVoltageL1/gridVoltageL2/gridVoltageL3; loadPowerL1/loadPowerL2/loadPowerL3.
      const PV_FIELDS = [
        "pvPower1", "pvPower2", "pvPower3", "pvPower4",
        "pvPower5", "pvPower6", "pvPower7", "pvPower8",
      ];
      const GRID_POWER_FIELDS   = ["gridPowerL1", "gridPowerL2", "gridPowerL3"];
      const GRID_VOLT_FIELDS    = ["gridVoltageL1", "gridVoltageL2", "gridVoltageL3"];
      const GRID_CURRENT_FIELDS = ["gridCurrentL1", "gridCurrentL2", "gridCurrentL3"];
      const LOAD_POWER_FIELDS   = ["loadPowerL1", "loadPowerL2", "loadPowerL3"];
      const LOAD_CURRENT_FIELDS = ["loadCurrentL1", "loadCurrentL2", "loadCurrentL3"];

      // --- Solar strings ---
      const pvValues = [];
      for (let k = 0; k < PV_FIELDS.length; k++) {
        const v = p[PV_FIELDS[k]];
        if (isNum(v)) pvValues.push({ label: "PV" + (k + 1), v });
      }
      if (pvValues.length > 0) {
        refs.pvChips.textContent = "";
        for (const { label, v } of pvValues) {
          const chip = document.createElement("span");
          chip.className = "detail-chip";
          chip.textContent = label + " " + NF0.format(v) + " W";
          refs.pvChips.appendChild(chip);
        }
        refs.pvGroup.style.display = "";
      } else {
        refs.pvGroup.style.display = "none";
      }

      // --- Battery voltage + temperature + current ---
      const bv = p.batteryVoltage;
      const bt = p.batteryTemperature;
      const bc = p.batteryCurrent;
      if (isNum(bv)) {
        refs.battVal.textContent = NF1.format(bv) + " V";
        refs.battVal.style.display = "";
      } else {
        refs.battVal.style.display = "none";
      }
      if (isNum(bt)) {
        refs.battTemp.textContent = NF1.format(bt) + " °C";
        refs.battTemp.style.display = "";
      } else {
        refs.battTemp.style.display = "none";
      }
      if (isNum(bc)) {
        refs.battCurrent.textContent = NF1.format(bc) + " A";
        refs.battCurrent.style.display = "";
      } else {
        refs.battCurrent.style.display = "none";
      }
      refs.battGroup.style.display = (isNum(bv) || isNum(bt) || isNum(bc)) ? "" : "none";

      // --- System (inverter temperature + grid frequency + load frequency) ---
      const it = p.inverterTemperature;
      const gf = p.gridFrequency;
      const lf = p.loadFrequency;
      if (isNum(it)) {
        refs.sysInvTemp.textContent = NF1.format(it) + " °C";
        refs.sysInvTemp.style.display = "";
      } else {
        refs.sysInvTemp.style.display = "none";
      }
      if (isNum(gf)) {
        refs.sysGridFreq.textContent = NF2.format(gf) + " Hz";
        refs.sysGridFreq.style.display = "";
      } else {
        refs.sysGridFreq.style.display = "none";
      }
      if (isNum(lf)) {
        refs.sysLoadFreq.textContent = NF2.format(lf) + " Hz";
        refs.sysLoadFreq.style.display = "";
      } else {
        refs.sysLoadFreq.style.display = "none";
      }
      refs.sysGroup.style.display = (isNum(it) || isNum(gf) || isNum(lf)) ? "" : "none";

      // --- Grid per-phase mini-table ---
      // Determine which phases (0=L1,1=L2,2=L3) have any data.
      const gridActivePh = [];
      for (let i = 0; i < 3; i++) {
        const hasP = isNum(p[GRID_POWER_FIELDS[i]]);
        const hasV = isNum(p[GRID_VOLT_FIELDS[i]]);
        const hasC = isNum(p[GRID_CURRENT_FIELDS[i]]);
        if (hasP || hasV || hasC) gridActivePh.push({ i, label: "L" + (i + 1), hasP, hasV, hasC });
      }
      if (gridActivePh.length > 0) {
        const hasPowerRow   = gridActivePh.some((ph) => ph.hasP);
        const hasVoltRow    = gridActivePh.some((ph) => ph.hasV);
        const hasCurrentRow = gridActivePh.some((ph) => ph.hasC);

        refs.gridTable.textContent = "";
        refs.gridTable.className = "detail-phase-table cols-" + gridActivePh.length;

        // Header row: blank + phase labels
        const hdrBlank = document.createElement("div");
        hdrBlank.className = "detail-phase-hdr";
        refs.gridTable.appendChild(hdrBlank);
        for (const ph of gridActivePh) {
          const hdr = document.createElement("div");
          hdr.className = "detail-phase-hdr";
          hdr.textContent = ph.label;
          refs.gridTable.appendChild(hdr);
        }

        if (hasPowerRow) {
          const rowLbl = document.createElement("div");
          rowLbl.className = "detail-phase-row-label";
          rowLbl.textContent = "Power (W)";
          refs.gridTable.appendChild(rowLbl);
          for (const ph of gridActivePh) {
            const cell = document.createElement("div");
            cell.className = "detail-phase-cell";
            const v = p[GRID_POWER_FIELDS[ph.i]];
            cell.textContent = isNum(v) ? NF0.format(v) : "";
            refs.gridTable.appendChild(cell);
          }
        }

        if (hasVoltRow) {
          const rowLbl = document.createElement("div");
          rowLbl.className = "detail-phase-row-label";
          rowLbl.textContent = "Voltage (V)";
          refs.gridTable.appendChild(rowLbl);
          for (const ph of gridActivePh) {
            const cell = document.createElement("div");
            cell.className = "detail-phase-cell";
            const v = p[GRID_VOLT_FIELDS[ph.i]];
            cell.textContent = isNum(v) ? NF1.format(v) : "";
            refs.gridTable.appendChild(cell);
          }
        }

        if (hasCurrentRow) {
          const rowLbl = document.createElement("div");
          rowLbl.className = "detail-phase-row-label";
          rowLbl.textContent = STR.current + " (A)";
          refs.gridTable.appendChild(rowLbl);
          for (const ph of gridActivePh) {
            const cell = document.createElement("div");
            cell.className = "detail-phase-cell";
            const v = p[GRID_CURRENT_FIELDS[ph.i]];
            cell.textContent = isNum(v) ? NF1.format(v) : "";
            refs.gridTable.appendChild(cell);
          }
        }

        refs.gridGroup.style.display = "";
      } else {
        refs.gridGroup.style.display = "none";
      }

      // --- Load per-phase mini-table ---
      const loadActivePh = [];
      for (let i = 0; i < 3; i++) {
        const hasP = isNum(p[LOAD_POWER_FIELDS[i]]);
        const hasC = isNum(p[LOAD_CURRENT_FIELDS[i]]);
        if (hasP || hasC) loadActivePh.push({ i, label: "L" + (i + 1), hasP, hasC });
      }
      if (loadActivePh.length > 0) {
        const hasLoadPowerRow   = loadActivePh.some((ph) => ph.hasP);
        const hasLoadCurrentRow = loadActivePh.some((ph) => ph.hasC);

        refs.loadTable.textContent = "";
        refs.loadTable.className = "detail-phase-table cols-" + loadActivePh.length;

        // Header row
        const hdrBlank = document.createElement("div");
        hdrBlank.className = "detail-phase-hdr";
        refs.loadTable.appendChild(hdrBlank);
        for (const ph of loadActivePh) {
          const hdr = document.createElement("div");
          hdr.className = "detail-phase-hdr";
          hdr.textContent = ph.label;
          refs.loadTable.appendChild(hdr);
        }

        // Power row
        if (hasLoadPowerRow) {
          const rowLbl = document.createElement("div");
          rowLbl.className = "detail-phase-row-label";
          rowLbl.textContent = "Power (W)";
          refs.loadTable.appendChild(rowLbl);
          for (const ph of loadActivePh) {
            const cell = document.createElement("div");
            cell.className = "detail-phase-cell";
            const v = p[LOAD_POWER_FIELDS[ph.i]];
            cell.textContent = isNum(v) ? NF0.format(v) : "";
            refs.loadTable.appendChild(cell);
          }
        }

        // Current row
        if (hasLoadCurrentRow) {
          const rowLbl = document.createElement("div");
          rowLbl.className = "detail-phase-row-label";
          rowLbl.textContent = STR.current + " (A)";
          refs.loadTable.appendChild(rowLbl);
          for (const ph of loadActivePh) {
            const cell = document.createElement("div");
            cell.className = "detail-phase-cell";
            const v = p[LOAD_CURRENT_FIELDS[ph.i]];
            cell.textContent = isNum(v) ? NF1.format(v) : "";
            refs.loadTable.appendChild(cell);
          }
        }

        refs.loadGroup.style.display = "";
      } else {
        refs.loadGroup.style.display = "none";
      }

      // --- Reading quality footnote ---
      const sc = p.sampleCount;
      const ps = p.periodSec;
      if (isNum(sc) && isNum(ps)) {
        refs.footnote.textContent =
          sc + " samples over " + Math.round(ps / 60) + " min";
        refs.footnote.style.display = "";
      } else {
        refs.footnote.style.display = "none";
      }
    }

    _updateInvCard(node, payload, ageMs, intervalS) {
      const p = payload && typeof payload === "object" ? payload : {};

      // Staleness — threshold scales with observed reporting cadence.
      const { staleAfterMs } = thresholdsFor(intervalS);
      const stale = isNum(ageMs) && ageMs > staleAfterMs;
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

      // Detail section — update in place every poll
      if (node.detail) {
        this._updateDetail(node.detail, p);
      }
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

        // Present-only helper: returns {value, present} where present=true when
        // at least one inverter reported a finite value for the field.
        const sumPresent = (field) => {
          let total = null;
          for (const inv of inverters) {
            const v =
              inv.energy && isNum(inv.energy[field]) ? inv.energy[field] : null;
            if (v != null) total = (total || 0) + v;
          }
          return { value: total, present: total != null };
        };

        const battCharged   = sumPresent("dailyBatteryChargeEnergy");
        const battDischarged = sumPresent("dailyBatteryDischargeEnergy");
        const generator     = sumPresent("dailyGeneratorEnergy");
        const losses        = sumPresent("dailyLossesEnergy");

        const tiles = [
          { label: STR.generated,        value: this._kwh(sum("dailyPvEnergy")),          presentOnly: false, present: true },
          { label: STR.consumed,         value: this._kwh(sum("dailyLoadEnergy")),         presentOnly: false, present: true },
          { label: STR.imported,         value: this._kwh(sum("dailyGridImportEnergy")),   presentOnly: false, present: true },
          { label: STR.exported,         value: this._kwh(sum("dailyGridExportEnergy")),   presentOnly: false, present: true },
          { label: STR.batteryCharged,   value: this._kwh(battCharged.value),              presentOnly: true,  present: battCharged.present },
          { label: STR.batteryDischarged, value: this._kwh(battDischarged.value),          presentOnly: true,  present: battDischarged.present },
          { label: STR.generator,        value: this._kwh(generator.value),                presentOnly: true,  present: generator.present },
          { label: STR.losses,           value: this._kwh(losses.value),                   presentOnly: true,  present: losses.present },
        ];

        if (!this._todaySec) return true;

        // Build once (8 tiles always in DOM), then mutate values + visibility.
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
            if (t.presentOnly && !t.present) tile.style.display = "none";
            grid.appendChild(tile);
            this._todayRefs.push({ val, tile, presentOnly: t.presentOnly });
          }
          this._todaySec.appendChild(grid);
        } else {
          for (let i = 0; i < tiles.length; i++) {
            const ref = this._todayRefs[i];
            ref.val.textContent = tiles[i].value;
            if (ref.presentOnly) {
              ref.tile.style.display = tiles[i].present ? "" : "none";
            }
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
    // History — driven by _histRangeDays and _histMetric (SP-C foundation)
    // ---------------------------------------------------------------- //
    async _loadHistory() {
      this._histReq = (this._histReq || 0) + 1;
      const req = this._histReq;
      try {
        if (!this._historySec) return true;

        if (!this._lastLiveInverterId) {
          this._histKey = null;
          this._historySec.className = "";
          this._historySec.innerHTML = "";
          const empty = document.createElement("div");
          empty.className = "history-empty";
          empty.textContent = STR.historyEmpty;
          this._historySec.appendChild(empty);
          return true;
        }

        const today = new Date();
        const startDate = new Date(today);
        startDate.setDate(today.getDate() - (this._histRangeDays - 1));
        const endStr = this._dateStr(today);
        const startStr = this._dateStr(startDate);

        // The history endpoint is per-inverter; we sum the chosen metric across
        // all live inverters per day for parity with the Today tiles.
        const ids = Object.keys(this._invNodes || {});
        const idList = ids.length ? ids : [this._lastLiveInverterId];
        const metric = this._histMetric;
        const isSources = this._histMode === "sources";
        const isTrends = this._histMode === "trends";
        const trendMetric = this._trendMetric;

        // byDay maps:
        //   energy mode  -> day -> summed kWh (number)
        //   sources mode -> day -> {pv, imp, batt} sums
        //   trends mode  -> day -> [{value, count}] per-inverter accumulator for weighted mean
        const byDay = new Map();
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
            if (typeof day !== "string") continue;
            if (isSources) {
              const prev = byDay.get(day) || { pv: 0, imp: 0, batt: 0 };
              const e = d.energy || {};
              prev.pv   += isNum(e.dailyPvEnergy)                ? e.dailyPvEnergy                : 0;
              prev.imp  += isNum(e.dailyGridImportEnergy)        ? e.dailyGridImportEnergy        : 0;
              prev.batt += isNum(e.dailyBatteryDischargeEnergy)  ? e.dailyBatteryDischargeEnergy  : 0;
              byDay.set(day, prev);
            } else if (isTrends) {
              // Accumulate per-inverter {value, count} for weighted-mean calculation.
              // d.avgs holds daily averages keyed by metric name; count comes from d.sample_count.
              const avgs = d.avgs || {};
              const rawVal = avgs[trendMetric];
              const rawCount = isNum(d.sample_count) ? d.sample_count : 0;
              if (isNum(rawVal) && rawCount > 0) {
                const prev = byDay.get(day) || [];
                prev.push({ value: rawVal, count: rawCount });
                byDay.set(day, prev);
              } else if (!byDay.has(day)) {
                // Mark the day as seen with no data so gaps are explicit.
                byDay.set(day, []);
              }
            } else {
              const v = d.energy && isNum(d.energy[metric]) ? d.energy[metric] : 0;
              byDay.set(day, (byDay.get(day) || 0) + v);
            }
          }
        }

        // Drop stale responses — a newer request has already been dispatched.
        if (req !== this._histReq) return true;

        if (isSources) {
          const sourceSeries = Array.from(byDay.entries())
            .map(([day, v]) => ({ day, pv: v.pv, imp: v.imp, batt: v.batt }))
            .sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));
          const dataFingerprint = sourceSeries.map((s) => s.day + ":" + s.pv + ":" + s.imp + ":" + s.batt).join(",");
          const newKey = this._histRangeDays + "|sources|" + dataFingerprint;
          if (newKey !== this._histKey) {
            this._histKey = newKey;
            this._renderHistorySources(sourceSeries);
          }
        } else if (isTrends) {
          // Compute weighted mean per day; days with no data → null (gap).
          const trendSeries = Array.from(byDay.entries())
            .map(([day, items]) => ({ day, value: this.weightedMean(items) }))
            .sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));
          const dataFingerprint = trendSeries.map((s) => s.day + ":" + (s.value === null ? "null" : s.value.toFixed(2))).join(",");
          const newKey = this._histRangeDays + "|trends|" + trendMetric + "|" + dataFingerprint;
          if (newKey !== this._histKey) {
            this._histKey = newKey;
            this._renderHistoryTrends(trendSeries, trendMetric);
          }
        } else {
          const series = Array.from(byDay.entries())
            .map(([day, kwh]) => ({ day, kwh }))
            .sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));
          // Build a cache key from range + metric + data fingerprint.
          const dataFingerprint = series.map((s) => s.day + ":" + s.kwh).join(",");
          const newKey = this._histRangeDays + "|" + metric + "|" + dataFingerprint;
          if (newKey !== this._histKey) {
            this._histKey = newKey;
            this._renderHistory(series);
          }
        }
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

    // Shared history controls bar: mode switcher + range chips + metric chips (energy only).
    // Appended to `container` in-place.
    _appendHistControls(container) {
      const controls = document.createElement("div");
      controls.className = "hist-controls";

      // Mode switcher: Energy | Sources | Trends
      const modeGroup = document.createElement("div");
      modeGroup.className = "hist-chip-group";
      modeGroup.setAttribute("role", "group");
      modeGroup.setAttribute("aria-label", STR.histAriaMode);
      const modeOptions = [
        { key: "energy",  label: STR.histModeEnergy },
        { key: "sources", label: STR.histModeSources },
        { key: "trends",  label: STR.histModeTrends },
      ];
      for (const mo of modeOptions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "hist-chip";
        btn.textContent = mo.label;
        btn.setAttribute("aria-pressed", mo.key === this._histMode ? "true" : "false");
        btn.addEventListener("click", () => {
          if (this._histMode === mo.key) return;
          this._histMode = mo.key;
          this._histKey = null;
          this._lastHistoryFetch = 0;
          if (this._histSec) this._histSec.textContent = this._histSectionTitle();
          this._loadHistory();
        });
        modeGroup.appendChild(btn);
      }
      controls.appendChild(modeGroup);

      // Separator
      const modeSep = document.createElement("div");
      modeSep.className = "hist-sep";
      modeSep.setAttribute("aria-hidden", "true");
      controls.appendChild(modeSep);

      // Range chips: 7 / 30 / 90 / 365
      const rangeGroup = document.createElement("div");
      rangeGroup.className = "hist-chip-group";
      rangeGroup.setAttribute("role", "group");
      rangeGroup.setAttribute("aria-label", STR.histAriaRange);
      const rangeDays = [7, 30, 90, 365];
      const rangeLabels = [STR.histDays7, STR.histDays30, STR.histDays90, STR.histDays365];
      for (let ri = 0; ri < rangeDays.length; ri++) {
        const d = rangeDays[ri];
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "hist-chip";
        btn.textContent = rangeLabels[ri];
        btn.setAttribute("aria-pressed", d === this._histRangeDays ? "true" : "false");
        btn.addEventListener("click", () => {
          if (this._histRangeDays === d) return;
          this._histRangeDays = d;
          this._histKey = null;
          this._lastHistoryFetch = 0;
          if (this._histSec) this._histSec.textContent = this._histSectionTitle();
          this._loadHistory();
        });
        rangeGroup.appendChild(btn);
      }
      controls.appendChild(rangeGroup);

      // Separator + metric chips: only visible in Energy mode
      if (this._histMode === "energy") {
        const sep = document.createElement("div");
        sep.className = "hist-sep";
        sep.setAttribute("aria-hidden", "true");
        controls.appendChild(sep);

        const metricGroup = document.createElement("div");
        metricGroup.className = "hist-chip-group";
        metricGroup.setAttribute("role", "group");
        metricGroup.setAttribute("aria-label", STR.histAriaMetric);
        const metricOptions = [
          { key: "dailyPvEnergy",              label: STR.histMetricGenerated },
          { key: "dailyLoadEnergy",            label: STR.histMetricConsumed },
          { key: "dailyGridImportEnergy",      label: STR.histMetricImported },
          { key: "dailyGridExportEnergy",      label: STR.histMetricExported },
          { key: "dailyBatteryChargeEnergy",   label: STR.histMetricBattCharged },
          { key: "dailyBatteryDischargeEnergy", label: STR.histMetricBattDischarged },
          { key: "dailyLossesEnergy",          label: STR.histMetricLosses },
        ];
        for (const opt of metricOptions) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "hist-chip";
          btn.textContent = opt.label;
          btn.setAttribute("aria-pressed", opt.key === this._histMetric ? "true" : "false");
          btn.addEventListener("click", () => {
            if (this._histMetric === opt.key) return;
            this._histMetric = opt.key;
            this._histKey = null;
            this._lastHistoryFetch = 0;
            if (this._histSec) this._histSec.textContent = this._histSectionTitle();
            this._loadHistory();
          });
          metricGroup.appendChild(btn);
        }
        controls.appendChild(metricGroup);
      }

      // Separator + trend-metric chips: only visible in Trends mode
      if (this._histMode === "trends") {
        const trendSep = document.createElement("div");
        trendSep.className = "hist-sep";
        trendSep.setAttribute("aria-hidden", "true");
        controls.appendChild(trendSep);

        const trendMetricGroup = document.createElement("div");
        trendMetricGroup.className = "hist-chip-group";
        trendMetricGroup.setAttribute("role", "group");
        trendMetricGroup.setAttribute("aria-label", STR.histAriaTrendMetric);
        const trendMetricOptions = [
          { key: "batterySoc",          label: STR.histTrendMetricSoc },
          { key: "inverterTemperature", label: STR.histTrendMetricInvTemp },
          { key: "batteryTemperature",  label: STR.histTrendMetricBattTemp },
          { key: "gridFrequency",       label: STR.histTrendMetricFreq },
        ];
        for (const opt of trendMetricOptions) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "hist-chip";
          btn.textContent = opt.label;
          btn.setAttribute("aria-pressed", opt.key === this._trendMetric ? "true" : "false");
          btn.addEventListener("click", () => {
            if (this._trendMetric === opt.key) return;
            this._trendMetric = opt.key;
            this._histKey = null;
            this._lastHistoryFetch = 0;
            if (this._histSec) this._histSec.textContent = this._histSectionTitle();
            this._loadHistory();
          });
          trendMetricGroup.appendChild(btn);
        }
        controls.appendChild(trendMetricGroup);
      }

      container.appendChild(controls);
    }

    _renderHistory(series) {
      if (!this._historySec) return;
      this._historySec.className = "";
      this._historySec.innerHTML = "";

      // Update the section title h2 to reflect current range + metric.
      if (this._histSec) {
        this._histSec.textContent = this._histSectionTitle();
      }

      this._appendHistControls(this._historySec);

      // ---- Empty state ----
      const allZero = series.length === 0 || series.every((s) => s.kwh === 0);
      if (allZero) {
        const empty = document.createElement("div");
        empty.className = "history-empty";
        empty.textContent = STR.historyEmpty;
        this._historySec.appendChild(empty);
        return;
      }

      let maxVal = 0;
      for (const s of series) if (s.kwh > maxVal) maxVal = s.kwh;

      const wrap = document.createElement("div");
      wrap.className = "history-chart";

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
    // History — Sources (stacked) renderer
    // ---------------------------------------------------------------- //
    _renderHistorySources(sourceSeries) {
      if (!this._historySec) return;
      this._historySec.className = "";
      this._historySec.innerHTML = "";

      // Update section title.
      if (this._histSec) {
        this._histSec.textContent = this._histSectionTitle();
      }

      this._appendHistControls(this._historySec);

      // ---- Empty state ----
      const allZero = sourceSeries.length === 0 ||
        sourceSeries.every((s) => s.pv === 0 && s.imp === 0 && s.batt === 0);
      if (allZero) {
        const empty = document.createElement("div");
        empty.className = "history-empty";
        empty.textContent = STR.historyEmpty;
        this._historySec.appendChild(empty);
        return;
      }

      // Max total per day determines chart scale.
      let maxVal = 0;
      for (const s of sourceSeries) {
        const total = s.pv + s.imp + s.batt;
        if (total > maxVal) maxVal = total;
      }

      const wrap = document.createElement("div");
      wrap.className = "history-chart";

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

      const lastIdx = sourceSeries.length - 1;

      for (let i = 0; i < sourceSeries.length; i++) {
        const s = sourceSeries[i];
        const total = s.pv + s.imp + s.batt;
        const pct = maxVal > 0 ? (total / maxVal) * 100 : 0;

        const col = document.createElement("div");
        col.className = "bar-col";

        // Value cap on most-recent bar
        if (i === lastIdx && total > 0) {
          const cap = document.createElement("div");
          cap.className = "bar-cap";
          cap.textContent = this._kwh(total);
          col.appendChild(cap);
        }

        // Stacked bar: segments stacked bottom-up (flex-direction: column-reverse)
        // Order in DOM: pv (top segment visually at top), imp, batt (bottom)
        // With column-reverse, first child = bottom of stack.
        const stackBar = document.createElement("div");
        stackBar.className = "stacked-bar";
        stackBar.style.height = Math.max(pct, 2) + "%";
        const tipLabel =
          this._localDate(s.day) + ": " +
          STR.histSourcesPv + " " + this._kwh(s.pv) + " · " +
          STR.histSourcesImport + " " + this._kwh(s.imp) + " · " +
          STR.histSourcesBattery + " " + this._kwh(s.batt) + " · " +
          STR.histSourcesTotal + " " + this._kwh(total);
        stackBar.setAttribute("role", "img");
        stackBar.setAttribute("aria-label", tipLabel);
        stackBar.setAttribute("tabindex", "0");

        // Segments: batt (bottom in column-reverse = first child), imp, pv (top = last child)
        const segs = [
          { val: s.batt, cls: "bar-seg-battery" },
          { val: s.imp,  cls: "bar-seg-import"  },
          { val: s.pv,   cls: "bar-seg-pv"      },
        ];
        for (const seg of segs) {
          if (seg.val <= 0) continue;
          const el = document.createElement("div");
          el.className = seg.cls;
          // Height as % of stacked-bar's own height
          el.style.height = (total > 0 ? (seg.val / total) * 100 : 0) + "%";
          stackBar.appendChild(el);
        }

        const showTip = (clientX) => {
          tooltip.textContent = tipLabel;
          tooltip.classList.add("show");
          const plotRect = plot.getBoundingClientRect();
          const barRect = stackBar.getBoundingClientRect();
          const x = (clientX != null ? clientX : barRect.left + barRect.width / 2) - plotRect.left + plot.scrollLeft;
          tooltip.style.left = x + "px";
          tooltip.style.top = (barRect.top - plotRect.top) + "px";
        };
        const hideTip = () => tooltip.classList.remove("show");
        stackBar.addEventListener("click", (e) => showTip(e.clientX));
        stackBar.addEventListener("mouseenter", (e) => showTip(e.clientX));
        stackBar.addEventListener("mouseleave", hideTip);
        stackBar.addEventListener("focus", () => showTip(null));
        stackBar.addEventListener("blur", hideTip);

        col.appendChild(stackBar);
        chart.appendChild(col);
      }

      plot.appendChild(chart);
      plot.appendChild(tooltip);
      area.appendChild(plot);
      wrap.appendChild(area);

      // X axis labels (every ~5th + last) aligned under bars
      const axis = document.createElement("div");
      axis.className = "bar-axis";
      for (let i = 0; i < sourceSeries.length; i++) {
        const lbl = document.createElement("div");
        lbl.className = "bar-label";
        if (i % 5 === 0 || i === lastIdx) {
          lbl.textContent = this._localDate(sourceSeries[i].day);
        }
        axis.appendChild(lbl);
      }
      wrap.appendChild(axis);

      // Legend
      const legend = document.createElement("div");
      legend.className = "hist-legend";
      legend.setAttribute("aria-label", "Chart legend");
      const legendItems = [
        { cls: "bar-seg-pv",      label: STR.histSourcesPv },
        { cls: "bar-seg-import",  label: STR.histSourcesImport },
        { cls: "bar-seg-battery", label: STR.histSourcesBattery },
      ];
      for (const li of legendItems) {
        const item = document.createElement("div");
        item.className = "hist-legend-item";
        const swatch = document.createElement("div");
        swatch.className = "hist-legend-swatch " + li.cls;
        swatch.setAttribute("aria-hidden", "true");
        const lbl = document.createElement("span");
        lbl.textContent = li.label;
        item.appendChild(swatch);
        item.appendChild(lbl);
        legend.appendChild(item);
      }
      wrap.appendChild(legend);

      this._historySec.appendChild(wrap);
    }

    // ---------------------------------------------------------------- //
    // History — Trends (line) helpers + renderer
    // ---------------------------------------------------------------- //

    // weightedMean(items, metric) — pure helper.
    // items: Array<{value: number, count: number}> — each entry is one inverter's daily average
    // and its sample count. Returns the sample_count-weighted mean, or null if items is empty
    // or total weight is zero (→ gap in the chart, not zero).
    weightedMean(items) {
      if (!Array.isArray(items) || items.length === 0) return null;
      let weightedSum = 0;
      let totalWeight = 0;
      for (const item of items) {
        if (isNum(item.value) && isNum(item.count) && item.count > 0) {
          weightedSum += item.value * item.count;
          totalWeight += item.count;
        }
      }
      return totalWeight > 0 ? weightedSum / totalWeight : null;
    }

    _renderHistoryTrends(trendSeries, trendMetric) {
      if (!this._historySec) return;
      this._historySec.className = "";
      this._historySec.innerHTML = "";

      // Update section title.
      if (this._histSec) {
        this._histSec.textContent = this._histSectionTitle();
      }

      this._appendHistControls(this._historySec);

      // Unit label per metric.
      const metricUnits = {
        batterySoc:           STR.histTrendUnitPct,
        inverterTemperature:  STR.histTrendUnitDegC,
        batteryTemperature:   STR.histTrendUnitDegC,
        gridFrequency:        STR.histTrendUnitHz,
      };
      const unit = metricUnits[trendMetric] || "";

      // ---- Empty state: no point has a value ----
      const hasData = trendSeries.some((s) => s.value !== null);
      if (!hasData) {
        const empty = document.createElement("div");
        empty.className = "history-empty";
        empty.textContent = STR.histTrendsNoData;
        this._historySec.appendChild(empty);
        return;
      }

      // Compute min/max from non-null values for y-axis auto-scale.
      let minVal = Infinity;
      let maxVal = -Infinity;
      for (const s of trendSeries) {
        if (s.value !== null) {
          if (s.value < minVal) minVal = s.value;
          if (s.value > maxVal) maxVal = s.value;
        }
      }
      // Give a small margin so extreme points don't touch the edge.
      const range = maxVal - minVal;
      const pad = range > 0 ? range * 0.08 : Math.max(Math.abs(maxVal) * 0.05, 1);
      const yLo = minVal - pad;
      const yHi = maxVal + pad;
      const yRange = yHi - yLo;

      const SVG_W = 600; // viewBox width (scales with element)
      const SVG_H = 140;

      const wrap = document.createElement("div");
      wrap.className = "history-chart";

      const area = document.createElement("div");
      area.className = "chart-area";

      // Y axis labels: max, mid, min
      const yAxis = document.createElement("div");
      yAxis.className = "y-axis";
      const fmt = (v) => {
        if (unit === STR.histTrendUnitPct) return Math.round(v) + "%";
        if (unit === STR.histTrendUnitHz)  return v.toFixed(2) + " Hz";
        return Math.round(v) + unit;
      };
      const yTop = document.createElement("span");
      yTop.textContent = fmt(yHi);
      const yMid = document.createElement("span");
      yMid.textContent = fmt((yHi + yLo) / 2);
      const yBot = document.createElement("span");
      yBot.textContent = fmt(yLo);
      yAxis.appendChild(yTop);
      yAxis.appendChild(yMid);
      yAxis.appendChild(yBot);
      area.appendChild(yAxis);

      const plot = document.createElement("div");
      plot.className = "chart-plot";
      plot.style.position = "relative";

      // Tooltip (reused pattern from bar chart)
      const tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      this._tooltip = tooltip;

      // SVG element
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns, "svg");
      svg.setAttribute("viewBox", "0 0 " + SVG_W + " " + SVG_H);
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("aria-hidden", "true");
      svg.classList.add("line-svg");

      // 50% gridline (visual only)
      const glLine = document.createElementNS(ns, "line");
      glLine.setAttribute("x1", "0");
      glLine.setAttribute("y1", SVG_H / 2);
      glLine.setAttribute("x2", SVG_W);
      glLine.setAttribute("y2", SVG_H / 2);
      glLine.classList.add("line-gridline");
      svg.appendChild(glLine);

      const n = trendSeries.length;
      // Map day index → SVG x coordinate (centered columns)
      const xOf = (i) => n > 1 ? (i / (n - 1)) * SVG_W : SVG_W / 2;
      // Map value → SVG y (0 = top)
      const yOf = (v) => SVG_H - ((v - yLo) / yRange) * SVG_H;

      // Build path segments: consecutive non-null points form a segment.
      // A null point breaks the line (gap).
      let pathD = "";
      let inSegment = false;
      for (let i = 0; i < n; i++) {
        const s = trendSeries[i];
        if (s.value === null) {
          inSegment = false;
          continue;
        }
        const x = xOf(i).toFixed(2);
        const y = yOf(s.value).toFixed(2);
        if (!inSegment) {
          pathD += "M" + x + "," + y;
          inSegment = true;
        } else {
          pathD += " L" + x + "," + y;
        }
      }
      if (pathD) {
        const path = document.createElementNS(ns, "path");
        path.setAttribute("d", pathD);
        path.classList.add("line-path");
        svg.appendChild(path);
      }

      // Dots for each non-null point (interactive)
      for (let i = 0; i < n; i++) {
        const s = trendSeries[i];
        if (s.value === null) continue;
        const cx = xOf(i);
        const cy = yOf(s.value);
        const circle = document.createElementNS(ns, "circle");
        circle.setAttribute("cx", cx.toFixed(2));
        circle.setAttribute("cy", cy.toFixed(2));
        circle.setAttribute("r", n > 60 ? "2" : "3.5");
        circle.classList.add("line-dot");
        const tipLabel = this._localDate(s.day) + ": " + fmt(s.value);
        circle.setAttribute("role", "img");
        circle.setAttribute("aria-label", tipLabel);
        circle.setAttribute("tabindex", "0");
        const showTip = (clientX) => {
          tooltip.textContent = tipLabel;
          tooltip.classList.add("show");
          const plotRect = plot.getBoundingClientRect();
          const svgRect = svg.getBoundingClientRect();
          // Map SVG x percentage to plot pixel offset.
          const xPx = (cx / SVG_W) * svgRect.width + (svgRect.left - plotRect.left) + plot.scrollLeft;
          const yPx = (cy / SVG_H) * svgRect.height + (svgRect.top - plotRect.top);
          tooltip.style.left = (clientX != null ? (clientX - plotRect.left + plot.scrollLeft) : xPx) + "px";
          tooltip.style.top = yPx + "px";
        };
        const hideTip = () => tooltip.classList.remove("show");
        circle.addEventListener("mouseenter", (e) => showTip(e.clientX));
        circle.addEventListener("mouseleave", hideTip);
        circle.addEventListener("click", (e) => showTip(e.clientX));
        circle.addEventListener("focus", () => showTip(null));
        circle.addEventListener("blur", hideTip);
        svg.appendChild(circle);
      }

      plot.appendChild(svg);
      plot.appendChild(tooltip);
      area.appendChild(plot);
      wrap.appendChild(area);

      // X axis labels (every ~5th + last) consistent with bar chart
      const lastIdx = trendSeries.length - 1;
      const axis = document.createElement("div");
      axis.className = "bar-axis";
      for (let i = 0; i < trendSeries.length; i++) {
        const lbl = document.createElement("div");
        lbl.className = "bar-label";
        if (i % 5 === 0 || i === lastIdx) {
          lbl.textContent = this._localDate(trendSeries[i].day);
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
