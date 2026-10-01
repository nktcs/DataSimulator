"""
Go2 Sensor Simulator
---------------------
Mock data source standing in for real sensors on the Go2 EDU, so a
consuming application can be exercised end-to-end before hardware is
mounted/validated.

Run:
    uvicorn app.main:app --reload --port 8000

Two ways to consume it, matching how real sensors would typically be
integrated (REST poll vs a continuous ROS2/WebRTC-style stream):

  REST (pull one reading):
    GET /sensors/{sensor_type}/sample
    GET /sensors/all/sample

  WebSocket (continuous stream, like a topic subscription):
    WS /sensors/{sensor_type}/stream
    WS /sensors/all/stream

  Alerts (fires once a sensor's checkpoint_status has been continuously
  "Warning" for at least alert_sustain_seconds; optionally POSTed to
  alert_webhook_url too, see POST /config):
    GET /alerts
    WS  /alerts/stream

  Runtime config (locomotion state, rates, anomaly mode, alerting):
    POST /config
    GET  /config
"""
import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .config import DEFAULT_RATES_HZ, ASSETS
from .schemas import ConfigUpdate
from .sensors import build_reading, GENERATORS, _pick_asset

# How long a stream "dwells" on one asset before the simulated robot moves
# to the next one, instead of picking a fresh random asset every message.
DWELL_SECONDS = (10.0, 30.0)

ALERTS_MAX = 200
ALERTS: list = []
# Per-sensor_type tracking of how long checkpoint_status has been
# continuously "Warning", so a single noisy reading doesn't fire an alert.
_breach_state: dict = {}


def _new_run_id() -> str:
    return f"RUN-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S')}"

app = FastAPI(
    title="Go2 Sensor Simulator",
    description="Synthetic data source for Go2 EDU sensor suite (RGB, thermal, gas, "
                 "env, acoustic, vibration, LiDAR, radiation) for functional validation "
                 "without physical hardware.",
    version="1.0.0",
)

STATE = {
    "locomotion_state": "idle",   # idle | walking | running
    "anomaly_mode": False,
    "rate_hz": dict(DEFAULT_RATES_HZ),
    "inspection_run_id": _new_run_id(),
    "alert_sustain_seconds": 10.0,  # continuous "Warning" duration before an alert fires
    "alert_webhook_url": None,      # POSTed with the alert payload when set
}

VALID_SENSORS = list(GENERATORS.keys())


@app.get("/", response_class=HTMLResponse)
def root():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Go2 Sensor Simulator</title>
        <style>
            :root {
                --bg: #ffffff;
                --text: #1a1a1a;
                --muted: #6b7280;
                --line: #e2e2e2;
                --row-alt: #fafafa;
                --pass: #15803d;
                --pass-bg: #ecfdf3;
                --warning: #b45309;
                --warning-bg: #fffbeb;
                --na: #6b7280;
                --na-bg: #f3f4f6;
            }
            * { box-sizing: border-box; }
            body {
                margin: 0;
                padding: 20px;
                font-family: "Segoe UI", system-ui, sans-serif;
                background: var(--bg);
                color: var(--text);
            }
            header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 12px;
                margin-bottom: 16px;
            }
            h1 { font-size: 1.25rem; margin: 0; }
            .sub { color: var(--muted); font-size: 0.85rem; margin-top: 2px; }
            .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
            select, button {
                font: inherit;
                padding: 6px 12px;
                border: 1px solid var(--line);
                border-radius: 6px;
                background: white;
                cursor: pointer;
            }
            button.primary { background: #1a1a1a; color: white; border-color: #1a1a1a; }
            .meta-line { color: var(--muted); font-size: 0.85rem; margin-bottom: 12px; }
            table { width: 100%; border-collapse: collapse; font-size: 0.92rem; table-layout: fixed; }
            th, td {
                text-align: left;
                padding: 8px 10px;
                border-bottom: 1px solid var(--line);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                height: 36px;
            }
            th { color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }
            tbody tr:nth-child(even) { background: var(--row-alt); }
            .pill {
                display: inline-block;
                padding: 2px 8px;
                border-radius: 999px;
                font-size: 0.78rem;
                font-weight: 600;
            }
            .pill.pass { color: var(--pass); background: var(--pass-bg); }
            .pill.warning { color: var(--warning); background: var(--warning-bg); }
            .pill.na { color: var(--na); background: var(--na-bg); }
            .unit { color: var(--muted); }
            section { margin-top: 28px; }
            section h2 { font-size: 1rem; margin: 0 0 4px; }
            .settings-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
            .settings-row label { font-size: 0.85rem; color: var(--muted); }
            input[type="number"], input[type="text"] {
                font: inherit;
                padding: 6px 10px;
                border: 1px solid var(--line);
                border-radius: 6px;
            }
            input[type="number"] { width: 70px; }
            input[type="text"] { width: 260px; }
            #alerts-empty { color: var(--muted); font-size: 0.88rem; padding: 8px 0; }
            @media (max-width: 700px) {
                table, thead, tbody, tr { display: block; }
                thead { display: none; }
                tr { border-bottom: 1px solid var(--line); padding: 8px 0; }
                td { border: none; padding: 2px 0; white-space: normal; overflow: visible; text-overflow: clip; height: auto; }
                td::before { content: attr(data-label) ": "; color: var(--muted); }
            }
        </style>
    </head>
    <body>
        <header>
            <div>
                <h1>Go2 Sensor Simulator</h1>
                <div class="sub">Synthetic sensor feed — updates every ~2s</div>
            </div>
            <div class="controls">
                <span id="run-id" class="sub">Run: --</span>
                <select id="locomotion-state" aria-label="Locomotion state">
                    <option value="idle">Idle</option>
                    <option value="walking">Walking</option>
                    <option value="running">Running</option>
                </select>
                <button id="anomaly-toggle" title="Anomaly mode boosts how often simulated faults occur (gas alarms, thermal hotspots, vibration faults, obstructions, radiation spikes), so you can reliably trigger Warning/alert behavior without waiting for a rare random event.">Anomaly: Off</button>
                <button id="new-run" class="primary">Start inspection</button>
            </div>
        </header>

        <table>
            <colgroup>
                <col style="width:12%"><col style="width:10%"><col style="width:14%">
                <col style="width:16%"><col style="width:14%"><col style="width:34%">
            </colgroup>
            <thead>
                <tr>
                    <th>Sensor</th>
                    <th>Status</th>
                    <th title="The piece of equipment or location this reading is attributed to (e.g. a pump, valve, or storage zone) — simulates the robot inspecting a specific asset.">Asset</th>
                    <th>Zone</th>
                    <th>Quality</th>
                    <th>Value</th>
                </tr>
            </thead>
            <tbody id="sensor-body"></tbody>
        </table>

        <section>
            <h2>Alerts</h2>
            <div class="settings-row">
                <label for="alert-sustain">Sustained for (s)</label>
                <input type="number" id="alert-sustain" min="1" step="1" value="10" />
                <label for="alert-webhook">Webhook URL (optional)</label>
                <input type="text" id="alert-webhook" placeholder="https://your-app/webhook" />
                <button id="alert-settings-save">Save</button>
            </div>
            <table>
                <colgroup>
                    <col style="width:14%"><col style="width:12%"><col style="width:14%">
                    <col style="width:16%"><col style="width:34%"><col style="width:10%">
                </colgroup>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Sensor</th>
                        <th>Asset</th>
                        <th>Zone</th>
                        <th>Value</th>
                        <th>Sustained</th>
                    </tr>
                </thead>
                <tbody id="alerts-body"></tbody>
            </table>
            <div id="alerts-empty">No alerts yet — fires once a sensor stays in "Warning" for the configured duration.</div>
        </section>

        <script>
            const SENSOR_ORDER = ["rgb", "thermal", "gas", "env", "acoustic", "vibration", "lidar", "radiation"];
            const SENSOR_LABEL = {
                rgb: "RGB", thermal: "Thermal", gas: "Gas", env: "Environment",
                acoustic: "Acoustic", vibration: "Vibration", lidar: "LiDAR", radiation: "Radiation",
            };
            // Streams can push at up to 50 Hz; buffer incoming readings here and
            // only paint the table on a fixed interval below, or the DOM churns
            // faster than anything is readable.
            const latest = {};
            const alerts = [];
            let runId = "--";
            let locomotionState = "idle";
            let anomalyMode = false;

            // Scalar sensors: one raw_value number, unit comes from the reading itself.
            const SCALAR_UNIT = { thermal: "°C", acoustic: "dB", vibration: "mm/s", lidar: "m", radiation: "µSv/h" };
            // Composite sensors (gas, env): raw_value is a dict, each key has its own unit.
            const FIELD_UNIT = {
                h2s_ppm: "ppm", co_ppm: "ppm", ch4_pct_lel: "%LEL", voc_ppm: "ppm", o2_pct: "%",
                ambient_temp_c: "°C", humidity_pct: "%RH", pressure_hpa: "hPa",
            };

            function fmtNumber(n) {
                if (typeof n !== "number") return String(n);
                return Math.abs(n) >= 100 ? n.toFixed(1) : n.toFixed(3).replace(/0+$/, "").replace(/\\.$/, "");
            }

            function fmtValue(type, raw) {
                if (raw === null || raw === undefined) return "--";
                if (typeof raw === "object") {
                    return Object.entries(raw)
                        .slice(0, 4)
                        .map(([k, v]) => {
                            const val = typeof v === "number" ? fmtNumber(v) : v;
                            const unit = FIELD_UNIT[k];
                            return `${k}: ${val}${unit ? ` <span class="unit">${unit}</span>` : ""}`;
                        })
                        .join("  ·  ");
                }
                const val = typeof raw === "number" ? fmtNumber(raw) : String(raw);
                const unit = SCALAR_UNIT[type];
                return unit ? `${val} <span class="unit">${unit}</span>` : val;
            }

            // Plain-text version (no HTML) for the title attribute, so the full
            // value is still available on hover once the cell truncates it.
            function plainValue(type, raw) {
                if (raw === null || raw === undefined) return "--";
                if (typeof raw === "object") {
                    return Object.entries(raw)
                        .slice(0, 4)
                        .map(([k, v]) => `${k}: ${typeof v === "number" ? fmtNumber(v) : v}${FIELD_UNIT[k] ? " " + FIELD_UNIT[k] : ""}`)
                        .join("  ·  ");
                }
                const val = typeof raw === "number" ? fmtNumber(raw) : String(raw);
                return SCALAR_UNIT[type] ? `${val} ${SCALAR_UNIT[type]}` : val;
            }

            function statusPillClass(status) {
                const s = String(status || "").toLowerCase();
                if (s === "pass") return "pass";
                if (s === "warning") return "warning";
                return "na";
            }

            function render() {
                const body = document.getElementById("sensor-body");
                body.innerHTML = SENSOR_ORDER.map((type) => {
                    const r = latest[type];
                    if (!r) {
                        return `<tr><td data-label="Sensor">${SENSOR_LABEL[type]}</td><td colspan="5">Waiting for data…</td></tr>`;
                    }
                    const cls = statusPillClass(r.checkpoint_status);
                    return `
                        <tr>
                            <td data-label="Sensor">${SENSOR_LABEL[type]}</td>
                            <td data-label="Status"><span class="pill ${cls}">${r.checkpoint_status || "N/A"}</span></td>
                            <td data-label="Asset">${r.asset_id || "--"}</td>
                            <td data-label="Zone">${r.location?.zone || "--"}</td>
                            <td data-label="Quality">${r.quality_flag || "--"}</td>
                            <td data-label="Value" class="value" title="${plainValue(type, r.raw_value)}">${fmtValue(type, r.raw_value)}</td>
                        </tr>
                    `;
                }).join("");
                document.getElementById("run-id").textContent = `Run: ${runId}`;
            }
            // Slowed down from a per-message repaint (streams push up to 50/sec)
            // to a fixed 2s cadence so values are readable instead of flickering.
            setInterval(render, 2000);

            function renderAlerts() {
                document.getElementById("alerts-empty").style.display = alerts.length ? "none" : "block";
                document.getElementById("alerts-body").innerHTML = alerts.slice(0, 25).map((a) => `
                    <tr>
                        <td data-label="Time">${new Date(a.triggered_at_utc).toLocaleTimeString()}</td>
                        <td data-label="Sensor">${SENSOR_LABEL[a.sensor_type] || a.sensor_type}</td>
                        <td data-label="Asset">${a.asset_id}</td>
                        <td data-label="Zone">${a.zone || "--"}</td>
                        <td data-label="Value" class="value" title="${plainValue(a.sensor_type, a.raw_value)}">${fmtValue(a.sensor_type, a.raw_value)}</td>
                        <td data-label="Sustained">${a.sustained_seconds}s</td>
                    </tr>
                `).join("");
            }

            async function fetchJson(url, options = {}) {
                const response = await fetch(url, options);
                if (!response.ok) throw new Error(`Request failed for ${url}`);
                return response.json();
            }

            async function loadConfig() {
                const config = await fetchJson("/config");
                locomotionState = config.locomotion_state || "idle";
                anomalyMode = !!config.anomaly_mode;
                runId = config.inspection_run_id || "--";
                document.getElementById("locomotion-state").value = locomotionState;
                document.getElementById("anomaly-toggle").textContent = `Anomaly: ${anomalyMode ? "On" : "Off"}`;
                document.getElementById("anomaly-toggle").classList.toggle("primary", anomalyMode);
                document.getElementById("alert-sustain").value = config.alert_sustain_seconds ?? 10;
                document.getElementById("alert-webhook").value = config.alert_webhook_url || "";
            }

            async function loadAlerts() {
                const payload = await fetchJson("/alerts?limit=25");
                alerts.length = 0;
                alerts.push(...payload.alerts);
                renderAlerts();
            }

            async function saveAlertSettings() {
                await fetchJson("/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        alert_sustain_seconds: Number(document.getElementById("alert-sustain").value) || 10,
                        alert_webhook_url: document.getElementById("alert-webhook").value.trim(),
                    }),
                });
                await loadConfig();
            }

            function openAlertsStream() {
                const scheme = window.location.protocol === "https:" ? "wss" : "ws";
                const ws = new WebSocket(`${scheme}://${window.location.host}/alerts/stream`);
                ws.onmessage = (event) => {
                    alerts.unshift(JSON.parse(event.data));
                    renderAlerts();
                };
                ws.onclose = () => setTimeout(openAlertsStream, 1500);
                ws.onerror = () => ws.close();
            }

            async function startInspection() {
                const payload = await fetchJson("/inspection/start", { method: "POST" });
                runId = payload.inspection_run_id;
            }

            async function updateLocomotion(value) {
                await fetchJson("/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ locomotion_state: value }),
                });
                await loadConfig();
            }

            async function toggleAnomaly() {
                await fetchJson("/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ anomaly_mode: !anomalyMode }),
                });
                await loadConfig();
            }

            function openStream() {
                const scheme = window.location.protocol === "https:" ? "wss" : "ws";
                const ws = new WebSocket(`${scheme}://${window.location.host}/sensors/all/stream`);
                ws.onmessage = (event) => {
                    const reading = JSON.parse(event.data);
                    latest[reading.sensor_type] = reading;
                };
                ws.onclose = () => setTimeout(openStream, 1500);
                ws.onerror = () => ws.close();
            }

            document.getElementById("new-run").addEventListener("click", startInspection);
            document.getElementById("locomotion-state").addEventListener("change", (e) => updateLocomotion(e.target.value));
            document.getElementById("anomaly-toggle").addEventListener("click", toggleAnomaly);
            document.getElementById("alert-settings-save").addEventListener("click", saveAlertSettings);

            loadConfig().catch((err) => console.error(err));
            loadAlerts().catch((err) => console.error(err));
            openStream();
            openAlertsStream();
        </script>
    </body>
    </html>
    """


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return root()


@app.get("/health")
def health():
    return {"status": "ok", "sensors": VALID_SENSORS, "state": STATE}


@app.get("/config")
def get_config():
    return STATE


@app.get("/alerts")
def list_alerts(limit: int = 50):
    """Most recent alerts first. An alert is only recorded once a sensor's
    checkpoint_status has been continuously 'Warning' for at least
    alert_sustain_seconds (see /config) - not on every breached reading."""
    return {"alerts": ALERTS[-limit:][::-1]}


@app.websocket("/alerts/stream")
async def alerts_stream(ws: WebSocket):
    await ws.accept()
    last_sent = len(ALERTS)
    try:
        while True:
            if len(ALERTS) > last_sent:
                for alert in ALERTS[last_sent:]:
                    await ws.send_text(json.dumps(alert))
                last_sent = len(ALERTS)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


@app.get("/assets")
def list_assets():
    """Mock asset/zone registry, matching the PoV's asset_id + location
    fields, so readings can be correlated by equipment context."""
    return {"assets": ASSETS}


@app.get("/inspection/current")
def current_inspection_run():
    return {"inspection_run_id": STATE["inspection_run_id"]}


@app.post("/inspection/start")
def start_inspection_run():
    """Start a new inspection_run_id, as if a robot were beginning a new
    inspection route/session (PoV Section 4)."""
    STATE["inspection_run_id"] = _new_run_id()
    return {"inspection_run_id": STATE["inspection_run_id"]}


@app.post("/config")
def update_config(update: ConfigUpdate):
    if update.locomotion_state is not None:
        if update.locomotion_state not in ("idle", "walking", "running"):
            raise HTTPException(400, "locomotion_state must be idle|walking|running")
        STATE["locomotion_state"] = update.locomotion_state
    if update.anomaly_mode is not None:
        STATE["anomaly_mode"] = update.anomaly_mode
    if update.rate_hz:
        for k, v in update.rate_hz.items():
            if k not in VALID_SENSORS:
                raise HTTPException(400, f"Unknown sensor '{k}'")
            STATE["rate_hz"][k] = float(v)
    if update.alert_sustain_seconds is not None:
        if update.alert_sustain_seconds <= 0:
            raise HTTPException(400, "alert_sustain_seconds must be > 0")
        STATE["alert_sustain_seconds"] = float(update.alert_sustain_seconds)
    if update.alert_webhook_url is not None:
        STATE["alert_webhook_url"] = update.alert_webhook_url or None  # "" clears it
    return STATE


# --------------------------------------------------------------- REST ----
@app.get("/sensors")
def list_sensors():
    return {"sensors": VALID_SENSORS}


@app.get("/sensors/{sensor_type}/sample")
def get_sample(sensor_type: str, asset_id: str = None):
    """asset_id is optional — omit it to get a reading from a randomly
    chosen asset in the mock registry (see GET /assets), or pass a
    specific asset_id (e.g. 'Pump-P101') to target one."""
    def _one(s, aid):
        return build_reading(
            s, asset_id=aid, inspection_run_id=STATE["inspection_run_id"],
            locomotion_state=STATE["locomotion_state"], anomaly_mode=STATE["anomaly_mode"],
        )

    if sensor_type == "all":
        # one shared asset for the whole snapshot, so a single "all" sample
        # reads like one robot at one location, not 8 sensors scattered
        # across 8 different assets.
        shared_asset_id = _pick_asset(asset_id)["asset_id"]
        return {s: _one(s, shared_asset_id) for s in VALID_SENSORS}
    if sensor_type not in VALID_SENSORS:
        raise HTTPException(404, f"Unknown sensor_type '{sensor_type}'. Valid: {VALID_SENSORS}")
    return _one(sensor_type, asset_id)


# ---------------------------------------------------------- WEBSOCKET ----
def _build(sensor_type: str, asset_id: str = None):
    return build_reading(
        sensor_type, asset_id=asset_id, inspection_run_id=STATE["inspection_run_id"],
        locomotion_state=STATE["locomotion_state"], anomaly_mode=STATE["anomaly_mode"],
    )


def _send_webhook(url: str, payload: dict):
    try:
        requests.post(url, json=payload, timeout=3)
    except requests.RequestException as exc:
        print(f"[alert webhook] POST to {url} failed: {exc}")


def _fire_alert(reading: dict, sustained_seconds: float):
    alert = {
        "alert_id": uuid.uuid4().hex[:12],
        "triggered_at_utc": datetime.now(timezone.utc).isoformat(),
        "sensor_type": reading["sensor_type"],
        "sensor_id": reading["sensor_id"],
        "asset_id": reading["asset_id"],
        "zone": reading["location"].get("zone"),
        "inspection_run_id": reading["inspection_run_id"],
        "raw_value": reading["raw_value"],
        "unit": reading["unit"],
        "threshold": reading["threshold"],
        "sustained_seconds": round(sustained_seconds, 1),
    }
    ALERTS.append(alert)
    del ALERTS[:-ALERTS_MAX]

    webhook_url = STATE.get("alert_webhook_url")
    if webhook_url:
        # fire-and-forget: don't let a slow/broken webhook stall the sensor stream
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _send_webhook, webhook_url, alert)


def _check_alert(reading: dict):
    """Track how long a sensor has been continuously in 'Warning' and fire
    an alert once it's been sustained for alert_sustain_seconds, rather
    than alerting on every single breached reading."""
    sensor_type = reading["sensor_type"]
    st = _breach_state.setdefault(sensor_type, {"since": None, "alerted": False, "asset_id": None})

    if reading["checkpoint_status"] != "Warning":
        st["since"] = None
        st["alerted"] = False
        st["asset_id"] = None
        return

    now = time.time()
    if st["since"] is None or st["asset_id"] != reading["asset_id"]:
        st["since"] = now
        st["alerted"] = False
        st["asset_id"] = reading["asset_id"]
        return

    sustained = now - st["since"]
    if not st["alerted"] and sustained >= STATE["alert_sustain_seconds"]:
        st["alerted"] = True
        _fire_alert(reading, sustained)


class _AssetWalker:
    """Tracks which asset a stream is currently 'at'. If asset_id is
    pinned by the caller it never changes; otherwise it dwells on one
    randomly-picked asset for a while before moving to the next, so a
    stream reads like a robot visiting locations, not teleporting on
    every message."""

    def __init__(self, asset_id: str = None):
        self._pinned = asset_id is not None
        self._asset_id = _pick_asset(asset_id)["asset_id"]
        self._next_switch = time.time() + random.uniform(*DWELL_SECONDS)

    @property
    def asset_id(self) -> str:
        if not self._pinned and time.time() >= self._next_switch:
            self._asset_id = _pick_asset(None)["asset_id"]
            self._next_switch = time.time() + random.uniform(*DWELL_SECONDS)
        return self._asset_id


async def _stream_single(ws: WebSocket, sensor_type: str, asset_id: str = None):
    await ws.accept()
    walker = _AssetWalker(asset_id)
    try:
        while True:
            reading = _build(sensor_type, walker.asset_id)
            _check_alert(reading)
            await ws.send_text(json.dumps(reading))
            rate = max(STATE["rate_hz"].get(sensor_type, 1.0), 0.1)
            await asyncio.sleep(1.0 / rate)
    except WebSocketDisconnect:
        pass


@app.websocket("/sensors/{sensor_type}/stream")
async def stream_sensor(ws: WebSocket, sensor_type: str, asset_id: str = None):
    """Optional ?asset_id=Pump-P101 query param pins the stream to one
    asset; omit it and the stream dwells on one randomly-picked asset for
    a while before moving to the next, simulating a robot visiting
    locations during a route (see _AssetWalker)."""
    if sensor_type != "all" and sensor_type not in VALID_SENSORS:
        await ws.close(code=1008)
        return

    if sensor_type == "all":
        await ws.accept()
        walker = _AssetWalker(asset_id)  # shared across sensors: one robot, one location at a time
        try:
            # simplest approach: round-robin all sensors at their own rates
            # by running independent send loops concurrently on one socket.
            async def sender(s):
                while True:
                    reading = _build(s, walker.asset_id)
                    _check_alert(reading)
                    await ws.send_text(json.dumps(reading))
                    rate = max(STATE["rate_hz"].get(s, 1.0), 0.1)
                    await asyncio.sleep(1.0 / rate)

            await asyncio.gather(*(sender(s) for s in VALID_SENSORS))
        except WebSocketDisconnect:
            pass
        return

    await _stream_single(ws, sensor_type, asset_id)
