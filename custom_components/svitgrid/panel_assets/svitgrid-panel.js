/* accent color is a placeholder pending brand review */

(function () {
  "use strict";

  const STYLE = `
    :host {
      display: block;
      font-family: var(--paper-font-body1_-_font-family, sans-serif);
      font-size: 14px;
      color: var(--primary-text-color, #212121);
      --accent: #1f6feb;
    }
    .panel-root {
      max-width: 960px;
      margin: 0 auto;
      padding: 16px;
    }
    .panel-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 20px;
    }
    .panel-header h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 600;
      color: var(--accent);
    }
    .live-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #888;
      flex-shrink: 0;
    }
    .live-dot.active { background: #22c55e; }
    .updated-label {
      margin-left: auto;
      font-size: 12px;
      color: var(--secondary-text-color, #888);
    }
    .section-title {
      font-size: 13px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--secondary-text-color, #888);
      margin: 20px 0 10px;
    }
    /* Live cards */
    .inverter-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 12px;
    }
    .inv-card {
      background: var(--card-background-color, #fff);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 12px;
      padding: 14px 16px;
    }
    .inv-card-title {
      font-size: 12px;
      font-weight: 600;
      color: var(--secondary-text-color, #888);
      margin-bottom: 10px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .inv-row {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      padding: 3px 0;
      font-size: 13px;
    }
    .inv-row-label { color: var(--secondary-text-color, #888); }
    .inv-row-value { font-weight: 500; }
    .inv-row-sub {
      font-size: 11px;
      color: var(--secondary-text-color, #888);
      margin-left: 4px;
    }
    /* Today tiles */
    .today-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 12px;
    }
    .today-tile {
      background: var(--card-background-color, #fff);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 12px;
      padding: 14px 16px;
      text-align: center;
    }
    .today-tile-value {
      font-size: 24px;
      font-weight: 700;
      color: var(--accent);
      line-height: 1.1;
    }
    .today-tile-unit {
      font-size: 12px;
      color: var(--secondary-text-color, #888);
      margin-top: 2px;
    }
    .today-tile-label {
      font-size: 12px;
      color: var(--secondary-text-color, #888);
      margin-top: 6px;
    }
    /* History bar chart */
    .history-chart {
      background: var(--card-background-color, #fff);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 12px;
      padding: 16px;
      overflow-x: auto;
    }
    .bar-chart {
      display: flex;
      align-items: flex-end;
      gap: 3px;
      height: 120px;
    }
    .bar-col {
      display: flex;
      flex-direction: column;
      align-items: center;
      flex: 1;
      min-width: 12px;
    }
    .bar {
      width: 100%;
      background: var(--accent);
      border-radius: 2px 2px 0 0;
      min-height: 2px;
    }
    .bar-label {
      font-size: 9px;
      color: var(--secondary-text-color, #888);
      margin-top: 3px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: clip;
      max-width: 32px;
      text-align: center;
    }
    .history-empty {
      color: var(--secondary-text-color, #888);
      text-align: center;
      padding: 20px;
      font-size: 13px;
    }
    /* Sync footer */
    .sync-footer {
      margin-top: 20px;
      font-size: 12px;
      color: var(--secondary-text-color, #888);
      padding: 10px 14px;
      background: var(--card-background-color, #fff);
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 10px;
    }
    .sync-footer.amber {
      border-color: #f59e0b;
      background: #fffbeb;
      color: #92400e;
    }
    /* Error/loading states */
    .section-error {
      font-size: 12px;
      color: #ef4444;
      padding: 6px 0;
    }
    .section-loading {
      font-size: 12px;
      color: var(--secondary-text-color, #888);
      padding: 6px 0;
    }
  `;

  class SvitgridPanel extends HTMLElement {
    constructor() {
      super();
      this._hass = null;
      this._built = false;
      this._timer = null;
      this._lastLiveInverterId = null;
      // Shadow DOM refs
      this._liveSec = null;
      this._todaySec = null;
      this._historySec = null;
      this._syncFooter = null;
      this._updatedLabel = null;
      this._liveDot = null;

      this.attachShadow({ mode: "open" });
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._built) {
        this._buildShell();
      }
      if (!this._timer) {
        this._startPolling();
      }
    }

    connectedCallback() {
      if (!this._built) {
        this._buildShell();
      }
      if (this._hass && !this._timer) {
        this._startPolling();
      }
    }

    disconnectedCallback() {
      if (this._timer) {
        clearInterval(this._timer);
        this._timer = null;
      }
    }

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
      h1.textContent = "Svitgrid";
      header.appendChild(h1);

      const dot = document.createElement("div");
      dot.className = "live-dot";
      this._liveDot = dot;
      header.appendChild(dot);

      const updated = document.createElement("span");
      updated.className = "updated-label";
      updated.textContent = "–";
      this._updatedLabel = updated;
      header.appendChild(updated);

      root.appendChild(header);

      // Live section
      const liveTitle = document.createElement("div");
      liveTitle.className = "section-title";
      liveTitle.textContent = "Live";
      root.appendChild(liveTitle);

      const liveSec = document.createElement("div");
      liveSec.className = "section-loading";
      liveSec.textContent = "Loading…";
      this._liveSec = liveSec;
      root.appendChild(liveSec);

      // Today section
      const todayTitle = document.createElement("div");
      todayTitle.className = "section-title";
      todayTitle.textContent = "Today";
      root.appendChild(todayTitle);

      const todaySec = document.createElement("div");
      todaySec.className = "section-loading";
      todaySec.textContent = "Loading…";
      this._todaySec = todaySec;
      root.appendChild(todaySec);

      // History section
      const histTitle = document.createElement("div");
      histTitle.className = "section-title";
      histTitle.textContent = "Solar — last 30 days";
      root.appendChild(histTitle);

      const histSec = document.createElement("div");
      histSec.className = "section-loading";
      histSec.textContent = "Loading…";
      this._historySec = histSec;
      root.appendChild(histSec);

      // Sync footer
      const syncFooter = document.createElement("div");
      syncFooter.className = "sync-footer";
      syncFooter.textContent = "Loading sync status…";
      this._syncFooter = syncFooter;
      root.appendChild(syncFooter);

      shadow.appendChild(root);
    }

    _startPolling() {
      if (this._timer) return;
      this._refresh();
      this._timer = setInterval(() => this._refresh(), 10000);
    }

    async _refresh() {
      if (!this._hass) return;
      await Promise.all([
        this._loadLive(),
        this._loadToday(),
        this._loadSync(),
      ]);
      // History depends on _lastLiveInverterId populated by _loadLive
      await this._loadHistory();

      // Update header
      if (this._updatedLabel) {
        this._updatedLabel.textContent =
          "updated " + new Date().toLocaleTimeString();
      }
      if (this._liveDot) {
        this._liveDot.classList.add("active");
      }
    }

    _call(path) {
      return this._hass.callApi("GET", path);
    }

    // ------------------------------------------------------------------ //
    // Formatters
    // ------------------------------------------------------------------ //

    _w(v) {
      if (v == null || typeof v !== "number" || !isFinite(v)) return "—";
      return Math.round(v) + " W";
    }

    _kwh(v) {
      if (v == null || typeof v !== "number" || !isFinite(v)) return "—";
      return v.toFixed(1);
    }

    _pct(v) {
      if (v == null || typeof v !== "number" || !isFinite(v)) return "—";
      return Math.round(v) + "%";
    }

    _localDate(dateStr) {
      // dateStr is ISO or date-only; returns localized short date
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

    _localTime(isoStr) {
      if (!isoStr) return "—";
      try {
        return new Date(isoStr).toLocaleTimeString();
      } catch (_) {
        return isoStr;
      }
    }

    _dateStr(d) {
      // Returns YYYY-MM-DD for a Date object using local timezone
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}`;
    }

    // ------------------------------------------------------------------ //
    // Live
    // ------------------------------------------------------------------ //

    async _loadLive() {
      try {
        const data = await this._call("svitgrid/live");
        const inverters = (data && Array.isArray(data.inverters))
          ? data.inverters
          : [];

        // Track first inverter id for history
        if (inverters.length > 0) {
          this._lastLiveInverterId = inverters[0].inverterId || null;
        }

        if (!this._liveSec) return;

        if (inverters.length === 0) {
          this._liveSec.className = "section-loading";
          this._liveSec.innerHTML = "No inverters reporting live data.";
          return;
        }

        const grid = document.createElement("div");
        grid.className = "inverter-grid";

        for (const inv of inverters) {
          const p = (inv.payload && typeof inv.payload === "object")
            ? inv.payload
            : {};
          const battPower = typeof p.batteryPower === "number" ? p.batteryPower : null;
          const gridPower = typeof p.gridPower === "number" ? p.gridPower : null;

          const card = document.createElement("div");
          card.className = "inv-card";

          const title = document.createElement("div");
          title.className = "inv-card-title";
          title.textContent = inv.inverterId || "Inverter";
          card.appendChild(title);

          const rows = [
            {
              label: "☀ PV",
              value: this._w(p.pvPower),
              sub: "",
            },
            {
              label: "🔋 Battery",
              value:
                this._pct(p.batterySoc) +
                (battPower != null
                  ? " (" + this._w(Math.abs(battPower)) + ")"
                  : ""),
              sub:
                battPower != null
                  ? battPower >= 0
                    ? "charging"
                    : "discharging"
                  : "",
            },
            {
              label: "⚡ Grid",
              value: this._w(gridPower != null ? Math.abs(gridPower) : null),
              sub:
                gridPower != null
                  ? gridPower > 0
                    ? "importing"
                    : "exporting"
                  : "",
            },
            {
              label: "🏠 Load",
              value: this._w(p.loadPower),
              sub: "",
            },
          ];

          for (const r of rows) {
            const row = document.createElement("div");
            row.className = "inv-row";

            const lbl = document.createElement("span");
            lbl.className = "inv-row-label";
            lbl.textContent = r.label;

            const valWrap = document.createElement("span");

            const val = document.createElement("span");
            val.className = "inv-row-value";
            val.textContent = r.value;
            valWrap.appendChild(val);

            if (r.sub) {
              const sub = document.createElement("span");
              sub.className = "inv-row-sub";
              sub.textContent = r.sub;
              valWrap.appendChild(sub);
            }

            row.appendChild(lbl);
            row.appendChild(valWrap);
            card.appendChild(row);
          }

          grid.appendChild(card);
        }

        this._liveSec.className = "";
        this._liveSec.innerHTML = "";
        this._liveSec.appendChild(grid);
      } catch (err) {
        if (this._liveSec) {
          this._liveSec.className = "section-error";
          this._liveSec.textContent =
            "Live data unavailable: " + (err && err.message ? err.message : String(err));
        }
      }
    }

    // ------------------------------------------------------------------ //
    // Today
    // ------------------------------------------------------------------ //

    async _loadToday() {
      try {
        const data = await this._call("svitgrid/today");
        const inverters = (data && Array.isArray(data.inverters))
          ? data.inverters
          : [];

        const sum = (field) => {
          let total = null;
          for (const inv of inverters) {
            const v =
              inv.energy && typeof inv.energy[field] === "number"
                ? inv.energy[field]
                : null;
            if (v != null) {
              total = (total || 0) + v;
            }
          }
          return total;
        };

        const tiles = [
          { label: "Generated", value: this._kwh(sum("dailyPvEnergy")), unit: "kWh" },
          { label: "Consumed", value: this._kwh(sum("dailyLoadEnergy")), unit: "kWh" },
          { label: "Imported", value: this._kwh(sum("dailyGridImportEnergy")), unit: "kWh" },
          { label: "Exported", value: this._kwh(sum("dailyGridExportEnergy")), unit: "kWh" },
        ];

        if (!this._todaySec) return;

        const grid = document.createElement("div");
        grid.className = "today-grid";

        for (const t of tiles) {
          const tile = document.createElement("div");
          tile.className = "today-tile";

          const val = document.createElement("div");
          val.className = "today-tile-value";
          val.textContent = t.value;

          const unit = document.createElement("div");
          unit.className = "today-tile-unit";
          unit.textContent = t.unit;

          const lbl = document.createElement("div");
          lbl.className = "today-tile-label";
          lbl.textContent = t.label;

          tile.appendChild(val);
          tile.appendChild(unit);
          tile.appendChild(lbl);
          grid.appendChild(tile);
        }

        this._todaySec.className = "";
        this._todaySec.innerHTML = "";
        this._todaySec.appendChild(grid);
      } catch (err) {
        if (this._todaySec) {
          this._todaySec.className = "section-error";
          this._todaySec.textContent =
            "Today data unavailable: " + (err && err.message ? err.message : String(err));
        }
      }
    }

    // ------------------------------------------------------------------ //
    // History
    // ------------------------------------------------------------------ //

    async _loadHistory() {
      try {
        if (!this._historySec) return;

        if (!this._lastLiveInverterId) {
          this._historySec.className = "history-empty";
          this._historySec.innerHTML =
            '<div class="history-empty">No inverter available for history.</div>';
          return;
        }

        const today = new Date();
        const start30 = new Date(today);
        start30.setDate(today.getDate() - 30);

        const endStr = this._dateStr(today);
        const startStr = this._dateStr(start30);

        const data = await this._call(
          "svitgrid/history?inverter_id=" +
            encodeURIComponent(this._lastLiveInverterId) +
            "&start=" +
            startStr +
            "&end=" +
            endStr
        );

        const days = (data && Array.isArray(data.days)) ? data.days : [];

        if (days.length === 0) {
          this._historySec.className = "";
          this._historySec.innerHTML = "";
          const empty = document.createElement("div");
          empty.className = "history-empty";
          empty.textContent = "No history data for the last 30 days.";
          this._historySec.appendChild(empty);
          return;
        }

        // Find max for scaling
        let maxVal = 0;
        for (const d of days) {
          const v =
            d.energy && typeof d.energy.dailyPvEnergy === "number"
              ? d.energy.dailyPvEnergy
              : 0;
          if (v > maxVal) maxVal = v;
        }

        const chartWrap = document.createElement("div");
        chartWrap.className = "history-chart";

        const barChart = document.createElement("div");
        barChart.className = "bar-chart";

        for (let i = 0; i < days.length; i++) {
          const d = days[i];
          const v =
            d.energy && typeof d.energy.dailyPvEnergy === "number"
              ? d.energy.dailyPvEnergy
              : 0;
          const pct = maxVal > 0 ? (v / maxVal) * 100 : 0;

          const col = document.createElement("div");
          col.className = "bar-col";

          const bar = document.createElement("div");
          bar.className = "bar";
          bar.style.height = Math.max(pct, 2) + "%";
          bar.title = (d.day || "") + ": " + this._kwh(v) + " kWh";

          const lbl = document.createElement("div");
          lbl.className = "bar-label";
          // Label every 5th bar to avoid clutter
          if (i % 5 === 0 || i === days.length - 1) {
            lbl.textContent = this._localDate(d.day);
          } else {
            lbl.textContent = "";
          }

          col.appendChild(bar);
          col.appendChild(lbl);
          barChart.appendChild(col);
        }

        chartWrap.appendChild(barChart);

        this._historySec.className = "";
        this._historySec.innerHTML = "";
        this._historySec.appendChild(chartWrap);
      } catch (err) {
        if (this._historySec) {
          this._historySec.className = "section-error";
          this._historySec.textContent =
            "History unavailable: " + (err && err.message ? err.message : String(err));
        }
      }
    }

    // ------------------------------------------------------------------ //
    // Sync status
    // ------------------------------------------------------------------ //

    async _loadSync() {
      try {
        const data = await this._call("svitgrid/sync-status");
        const counts = (data && data.counts && typeof data.counts === "object")
          ? data.counts
          : {};
        const synced = counts.sent || 0;
        const pending = counts.pending || 0;
        const failed = counts.failed || 0;
        const skipped = counts.skipped || 0;
        const lastSent = data && data.last_sent_ts
          ? this._localTime(data.last_sent_ts)
          : "—";

        if (!this._syncFooter) return;

        const hasIssue = pending + failed > 0;
        this._syncFooter.className = "sync-footer" + (hasIssue ? " amber" : "");
        this._syncFooter.textContent =
          "Synced " +
          synced +
          " · Pending " +
          pending +
          " · Failed " +
          failed +
          " · Skipped " +
          skipped +
          " · Last sent " +
          lastSent;
      } catch (err) {
        if (this._syncFooter) {
          this._syncFooter.className = "sync-footer";
          this._syncFooter.textContent =
            "Sync status unavailable: " +
            (err && err.message ? err.message : String(err));
        }
      }
    }
  }

  if (!customElements.get("svitgrid-panel")) {
    customElements.define("svitgrid-panel", SvitgridPanel);
  }
})();
