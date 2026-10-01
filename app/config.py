"""
Central config: streaming rates, value ranges, and noise profiles.
Tune these to match datasheets more closely, or to stress-test the
consuming application (e.g. push rates up to test bandwidth/compute limits).
"""

# Default publish rate (Hz) per sensor if not overridden per-request.
DEFAULT_RATES_HZ = {
    "rgb": 15.0,
    "thermal": 9.0,          # FLIR Lepton 3.5 native ~8.7 Hz
    "gas": 1.0,
    "env": 1.0,              # BME688
    "acoustic": 20.0,        # summarized frames, not raw audio rate
    "vibration": 50.0,       # WISE-2410 style
    "lidar": 10.0,           # Livox Mid-360
    "radiation": 0.5,        # GQ GMC-500 CPM updates
}

# Nominal (idle, healthy) operating ranges.
RANGES = {
    "rgb": {"width": 640, "height": 480, "fps": 30},
    "thermal": {
        "width": 160, "height": 120,      # Lepton 3.5 native resolution
        "temp_min_c": 15.0, "temp_max_c": 35.0,   # ambient scene baseline
        "hotspot_temp_c": (60.0, 400.0),           # possible hotspot range
        "hotspot_probability": 0.08,
    },
    "gas": {
        "h2s_ppm": (0.0, 10.0),
        "co_ppm": (0.0, 35.0),
        "ch4_pct_lel": (0.0, 5.0),
        "voc_ppm": (0.0, 5.0),
        "o2_pct": (19.5, 23.0),
        "alarm_probability": 0.02,
    },
    "env": {
        "temp_c": (15.0, 35.0),
        "humidity_pct": (20.0, 80.0),
        "pressure_hpa": (990.0, 1025.0),
        "gas_resistance_kohm": (10.0, 200.0),
    },
    "acoustic": {
        "sample_rate_hz": 16000,
        "frame_samples": 512,
        "amplitude_db": (30.0, 90.0),
        "ultrasonic_intensity": (0.0, 1.0),
    },
    "vibration": {
        # Advantech WISE-2410 datasheet: 3-axis accel, selectable
        # +/-2/4/8/16 g full-scale, 5-1000 Hz. Its condition-monitoring
        # output is velocity RMS compliant with ISO 10816, so that's
        # what idle_accel_rms_g/fault_probability drive below, rather
        # than a raw accel-g threshold.
        "sensor_range_g": (-16.0, 16.0),
        "idle_accel_rms_g": 0.015,     # healthy machine/platform-at-rest baseline
        "fault_probability": 0.01,     # simulated bearing/imbalance fault event
        "velocity_ref_freq_hz": 50.0,  # nominal freq for accel->velocity RMS conversion (typical rotating-machinery speed)
        "sample_rate_hz": 1000,
        "fft_bins": 32,
    },
    "lidar": {
        "num_points": 500,           # downsampled for mock payload size
        "range_m": (0.1, 40.0),      # Mid-360 spec: 40m @ 10% reflectivity
        "fov_horizontal_deg": 360,
        "fov_vertical_min_deg": -7.0,   # Mid-360 spec: asymmetric -7 to +52 deg vertical FOV
        "fov_vertical_max_deg": 52.0,
        "blind_zone_m": 0.1,            # Mid-360 spec: minimum detectable range
        "nearest_obstacle_idle_m": (1.0, 15.0),   # typical clearance during a normal inspection route
        "nearest_obstacle_alert_m": (0.05, 0.3),  # simulated close-obstacle/obstruction event
        "obstacle_probability": 0.03,
    },
    "radiation": {
        "background_usv_h": (0.05, 0.30),
        "background_cpm": (10, 60),
        "hotspot_probability": 0.01,
        "hotspot_usv_h": (5.0, 500.0),
    },
}

# Extra noise/degradation injected when the robot is in motion, to
# approximate the "temperature, humidity, dust and vibration exposure
# during locomotion" concern from the sensor review note.
LOCOMOTION_NOISE_PROFILES = {
    "idle":    {"vibration_mult": 1.0, "acoustic_noise_db": 0.0, "rgb_blur": 0.0, "dropout_prob": 0.0},
    "walking": {"vibration_mult": 3.0, "acoustic_noise_db": 8.0, "rgb_blur": 0.25, "dropout_prob": 0.01},
    "running": {"vibration_mult": 6.0, "acoustic_noise_db": 15.0, "rgb_blur": 0.6, "dropout_prob": 0.04},
}

# ---------------------------------------------------------------------------
# Hazardous Area Inspection Application PoV — Section 4 "Data Model" support
# ---------------------------------------------------------------------------

# Local timezone offset used to populate timestamp_local, alongside
# timestamp_utc, per the PoV's data model field ("Exact collection time
# in UTC and local time").
LOCAL_TZ_OFFSET_HOURS = 5.5  # e.g. IST; change to match deployment site

# Mock asset/zone registry. Each reading is attributed to one of these,
# with a location, so multiple sensor readings can be correlated by
# asset + time + location as the PoV's "Key Design Principles" require.
ASSETS = [
    {"asset_id": "Pump-P101",  "zone": "Boiler Area",     "x": 12.4, "y": 4.8},
    {"asset_id": "Pump-P102",  "zone": "Boiler Area",     "x": 13.1, "y": 5.6},
    {"asset_id": "Valve-V22",  "zone": "Compressor Room", "x": 30.2, "y": 11.0},
    {"asset_id": "Motor-M12",  "zone": "Compressor Room", "x": 31.5, "y": 9.7},
    {"asset_id": "Panel-EP04", "zone": "Electrical Bay",  "x": 8.0,  "y": 22.3},
    {"asset_id": "Zone-A",     "zone": "Storage Tank Farm", "x": 50.0, "y": 2.0},
]

# unit + safe-operating threshold per sensor, drawn from the PoV's
# Section 3/9 tables ("unit and threshold Measurement unit and
# applicable safe limit"). Used to populate the evidence record and to
# decide whether a reading should be flagged as a checkpoint breach.
UNIT_THRESHOLDS = {
    "rgb":       {"unit": "n/a", "threshold": None},  # visual defects, not a scalar threshold
    "thermal":   {"unit": "deg_C", "threshold": 80.0},       # panel/motor hotspot alarm baseline
    "gas": {
        "unit": "ppm / %LEL / %O2",
        "threshold": {
            "h2s_ppm": 10.0, "co_ppm": 35.0, "ch4_pct_lel": 10.0,
            "voc_ppm": 5.0, "o2_pct_min": 19.5, "o2_pct_max": 23.0,
        },
    },
    "env":       {"unit": "deg_C / %RH / hPa", "threshold": None},
    "acoustic":  {"unit": "dB", "threshold": 85.0},          # leak/anomaly signature flag level
    "vibration": {"unit": "mm/s (velocity RMS, ISO 10816)", "threshold": 4.5},  # ISO 10816-3 Zone C/D boundary, rigidly-mounted small/medium machines
    "lidar":     {"unit": "m", "threshold": 0.3},              # minimum clearance before flagging obstruction (just above the 0.1m sensor blind zone)
    "radiation": {"unit": "uSv/h", "threshold": 5.0},
}

# Probability that a reading is flagged with a degraded quality_flag,
# independent of whether the value itself breaches a safety threshold.
# Mirrors the PoV's "treat data quality as a core feature" principle.
QUALITY_FLAG_PROBS = {
    "low_confidence": 0.03,
    "calibration_overdue": 0.01,
}
