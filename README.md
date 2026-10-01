# Go2 Sensor Simulator

A small FastAPI service that generates **synthetic sensor data** for the
sensor suite under evaluation for the Go2 EDU (RGB, thermal, multi-gas,
environmental, acoustic/ultrasonic, vibration, LiDAR, radiation). It exists
so the consuming application (dashboards, alerting logic, fusion pipelines,
data storage, etc.) can be built and validated **before physical sensors
are mounted and tested** on the robot.

Every reading is returned as an **evidence record** matching Section 4 of
the *Hazardous Area Inspection Application PoV* ("Data Model: What Each
Record Should Contain") — `timestamp` (UTC + local), `asset_id`,
`location`, `sensor_id`/`sensor_type`, `raw_value`/`file_uri`,
`unit`/`threshold`, `quality_flag`, and `inspection_run_id` — plus a
derived `checkpoint_status` (Pass/Warning/N/A) so downstream checkpoint
and risk-scoring logic (Sections 6–9 of the PoV) has something real to
consume, not just raw values.

This is a functional mock, not a physics simulator — it won't reproduce
exact sensor noise characteristics, but it gives realistic value ranges,
realistic payload shapes, and injectable anomalies/dropouts/quality issues
so you can exercise real application logic (thresholds, alarms, evidence
correlation, error handling).

## Install & run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Interactive API docs: `http://localhost:8000/docs`

## Two ways to consume data

**1. REST — pull a single reading on demand**
```
GET /sensors/{sensor_type}/sample?asset_id=Pump-P101     # one sensor
GET /sensors/all/sample                                  # snapshot across all sensors
```
`asset_id` is optional — omit it to get a reading from a randomly chosen
asset in the mock registry (`GET /assets`), or target a specific one.

**2. WebSocket — subscribe to a continuous stream**
(mirrors how a real sensor would publish onto a ROS 2 topic / WebRTC
data channel at a fixed rate)
```
WS /sensors/{sensor_type}/stream?asset_id=Pump-P101
WS /sensors/all/stream                # all sensors, each at its own rate
```

Valid `sensor_type` values: `rgb`, `thermal`, `gas`, `env`, `acoustic`,
`vibration`, `lidar`, `radiation`.

**3. Inspection run / asset registry**
```
GET  /assets                    # mock asset/zone registry (id, zone, x, y)
GET  /inspection/current        # current inspection_run_id
POST /inspection/start          # start a new run, get a new inspection_run_id
```
All readings are tagged with whatever `inspection_run_id` is currently
active — call `POST /inspection/start` at the beginning of a simulated
inspection route, the way a robot would when it starts a new run.

## Runtime configuration

```
GET  /config
POST /config
```

Body (all fields optional):
```json
{
  "locomotion_state": "walking",
  "anomaly_mode": true,
  "rate_hz": {"lidar": 5, "vibration": 100}
}
```

- **`locomotion_state`** (`idle` | `walking` | `running`) — injects extra
  vibration, acoustic noise, motion blur, and dropout probability, as a
  stand-in for the "temperature, humidity, dust and vibration exposure
  during locomotion" concern raised in the sensor review.
- **`anomaly_mode`** — raises the probability of hotspots/alarms/spikes
  (thermal hotspot, gas alarm, radiation spike) so alerting logic gets
  exercised, not just nominal values.
- **`rate_hz`** — per-sensor publish rate for streaming endpoints. Push
  several sensors to high rates simultaneously to approximate the
  "bandwidth and compute with multiple sensors streaming" concern.

## Example client

```bash
python client_example.py rest
python client_example.py stream vibration
python client_example.py stream all
```

## Evidence record schema (PoV Section 4)

Every reading, regardless of sensor, has this shape:

```json
{
  "record_id": "c8a9b358bc4a",
  "timestamp_utc": "2026-09-22T14:11:56.02Z",
  "timestamp_local": "2026-09-22T19:41:56.02+05:30",
  "inspection_run_id": "RUN-2026-09-22-141155",
  "asset_id": "Valve-V22",
  "location": {"x": 30.2, "y": 11.0, "zone": "Compressor Room"},
  "sensor_id": "go2_multigas_module",
  "sensor_type": "gas",
  "unit": "ppm / %LEL / %O2",
  "threshold": {"h2s_ppm": 10.0, "co_ppm": 35.0, "ch4_pct_lel": 10.0, "voc_ppm": 5.0, "o2_pct_min": 19.5, "o2_pct_max": 23.0},
  "quality_flag": "Good",
  "raw_value": {"h2s_ppm": 1.74, "co_ppm": 31.2, "ch4_pct_lel": 3.39, "voc_ppm": 1.86, "o2_pct": 22.97},
  "file_uri": null,
  "checkpoint_status": "Pass",
  "locomotion_state": "idle",
  "data": { "...": "full sensor-specific payload" }
}
```

- **`raw_value`** — the scalar/summary metric(s) used for threshold checks (matches the PoV's `raw_value` example, e.g. `CH4=2.1%`).
- **`file_uri`** — set only for heavy payloads (RGB, thermal, LiDAR), pointing to a mock `s3://` path. There's no real object store here, so the full payload still rides along in `data` for convenience — a real integration would fetch it from storage instead.
- **`quality_flag`** — `Good` / `Low confidence` / `Calibration overdue` / `Missing data`, randomly injected per the PoV's "treat data quality as a core feature" principle — independent of whether the value itself is safe.
- **`checkpoint_status`** — `Pass` / `Warning` derived from comparing `raw_value` to `threshold` (PoV Section 6/8), or `N/A` for sensors evaluated qualitatively (RGB defects via CV, environmental trend) or when the reading was dropped.
- **`location`** and **`asset_id`** come from a small mock asset/zone registry (`GET /assets`) so readings can be correlated by asset + time + location, per the PoV's key design principles.

## Mapping to the sensor list

| Sensor type | Generator | Payload shape |
|---|---|---|
| RGB camera | `generate_rgb_frame` | base64 JPEG frame, width/height/fps |
| Thermal (FLIR Lepton 3.5) | `generate_thermal_frame` | 160x120 temp matrix (°C), min/max/avg, hotspot flag |
| Multi-gas | `generate_gas_reading` | H2S, CO, CH4 %LEL, VOC, O2, alarm flag |
| Environmental (BME688) | `generate_env_reading` | temp, humidity, pressure, gas resistance |
| Acoustic / ultrasonic | `generate_acoustic_reading` | waveform preview, FFT spectrum preview, dominant freq, dB, ultrasonic intensity |
| Vibration (WISE-2410) | `generate_vibration_reading` | X/Y/Z RMS + peak, FFT spectrum |
| LiDAR (Livox Mid-360) | `generate_lidar_frame` | downsampled XYZI point cloud, range min/max |
| Radiation (GQ GMC-500) | `generate_radiation_reading` | dose rate (µSv/h), CPM, alarm flag |

## Extending toward real hardware later

When actual sensors come online, the cleanest migration path is to keep
this same REST/WebSocket contract and swap each `generate_*` function in
`app/sensors.py` for a real driver call (ROS 2 subscriber, serial read,
SDK call, etc.) — the consuming application shouldn't need to change.

## Out of scope for this mock (see PoV for full design)

- **MQTT transport** (Section 5) — this service exposes REST + WebSocket
  only. A real edge gateway would publish continuous telemetry over MQTT;
  bolting on an MQTT publisher (e.g. `paho-mqtt`) that republishes the
  same evidence records is a natural next step if that transport needs
  to be exercised specifically.
- **Compression before upload** (Section 5) — payloads are sent as plain
  JSON; heavy fields (thermal matrix, point cloud, RGB frame) would be
  compressed in a real pipeline.
- **Risk scoring aggregation** (Section 9) — this service emits
  per-reading `checkpoint_status`, which is the input a risk-scoring
  engine would consume; it does not itself compute a rolled-up
  per-asset/zone risk score across multiple readings and sensor types.
- **Correlated insights** (Section 7's "Insight" and "Action" levels,
  e.g. thermal rise + vibration anomaly → bearing issue) — each reading
  is generated independently; cross-sensor correlation is left to the
  consuming application, as intended.

## Known simplifications

- Point clouds and thermal matrices are sent as full JSON arrays for
  simplicity; for real bandwidth testing you'd want binary encoding
  (e.g. protobuf, or ROS 2's native serialization) instead.
- The "locomotion noise" model is a rough heuristic, not measured data
  from actual Go2 gait dynamics — treat it as a placeholder to be
  replaced once real mounted-sensor data is available.
- Single-process asyncio server; fine for functional testing, not a
  bandwidth/latency benchmark of the real Jetson Orin + ROS 2 stack.
