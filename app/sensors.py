"""
Synthetic data generators, one function per sensor type.

Each function returns a plain dict matching the "Typical Data" column
from the sensor comparison table. Values are randomized within
realistic ranges from config.py, with optional injected anomalies
(hotspots, alarms, dropouts) so downstream logic (alerting, thresholds,
fusion) can be exercised, not just the happy path.
"""
import base64
import io
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import (
    RANGES,
    LOCOMOTION_NOISE_PROFILES,
    ASSETS,
    UNIT_THRESHOLDS,
    QUALITY_FLAG_PROBS,
    LOCAL_TZ_OFFSET_HOURS,
)

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def _now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_local_iso():
    local = datetime.now(timezone.utc) + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)
    return local.replace(tzinfo=timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))).isoformat()


def _noise_profile(locomotion_state: str):
    return LOCOMOTION_NOISE_PROFILES.get(locomotion_state, LOCOMOTION_NOISE_PROFILES["idle"])


def _maybe_drop(locomotion_state: str) -> bool:
    return random.random() < _noise_profile(locomotion_state)["dropout_prob"]


def _pick_asset(asset_id: str = None) -> dict:
    """Look up a named asset from the registry, or pick one at random
    so readings are attributed to an asset/zone + location per the
    PoV's 'correlate by asset, time, and location' principle."""
    if asset_id:
        for a in ASSETS:
            if a["asset_id"] == asset_id:
                return a
        # unknown asset_id: fabricate a location-less stub rather than fail,
        # so callers can still test with asset IDs outside the mock registry
        return {"asset_id": asset_id, "zone": "Unregistered", "x": None, "y": None}
    return random.choice(ASSETS)


def _quality_flag(dropped: bool, anomaly_mode: bool) -> str:
    if dropped:
        return "Missing data"
    mult = 3 if anomaly_mode else 1
    if random.random() < QUALITY_FLAG_PROBS["calibration_overdue"] * mult:
        return "Calibration overdue"
    if random.random() < QUALITY_FLAG_PROBS["low_confidence"] * mult:
        return "Low confidence"
    return "Good"


def _raw_value_and_breach(sensor_type: str, data: dict):
    """Extract the PoV data model's `raw_value` summary and evaluate it
    against the sensor's safe-limit `threshold` (Section 9 risk inputs).
    Returns (raw_value, checkpoint_breach) where checkpoint_breach is
    True/False for sensors with a defined scalar threshold, or None for
    sensors evaluated qualitatively (RGB defects, environmental trend).
    """
    thr = UNIT_THRESHOLDS[sensor_type]["threshold"]

    if sensor_type == "thermal":
        raw_value = data.get("temp_max_c")
        return raw_value, (raw_value is not None and raw_value > thr)

    if sensor_type == "gas":
        raw_value = {
            "h2s_ppm": data.get("h2s_ppm"), "co_ppm": data.get("co_ppm"),
            "ch4_pct_lel": data.get("ch4_pct_lel"), "voc_ppm": data.get("voc_ppm"),
            "o2_pct": data.get("o2_pct"),
        }
        breach = (
            data.get("h2s_ppm", 0) > thr["h2s_ppm"] or
            data.get("co_ppm", 0) > thr["co_ppm"] or
            data.get("ch4_pct_lel", 0) > thr["ch4_pct_lel"] or
            data.get("voc_ppm", 0) > thr["voc_ppm"] or
            data.get("o2_pct", 21) < thr["o2_pct_min"] or
            data.get("o2_pct", 21) > thr["o2_pct_max"]
        )
        return raw_value, breach

    if sensor_type == "acoustic":
        raw_value = data.get("amplitude_db")
        return raw_value, (raw_value is not None and raw_value > thr)

    if sensor_type == "vibration":
        raw_value = data.get("velocity_rms_mms")
        return raw_value, (raw_value is not None and raw_value > thr)

    if sensor_type == "lidar":
        raw_value = data.get("nearest_obstacle_m")
        return raw_value, (raw_value is not None and raw_value < thr)

    if sensor_type == "radiation":
        raw_value = data.get("dose_rate_usv_h")
        return raw_value, (raw_value is not None and raw_value > thr)

    if sensor_type == "env":
        raw_value = {
            "ambient_temp_c": data.get("ambient_temp_c"),
            "humidity_pct": data.get("humidity_pct"),
            "pressure_hpa": data.get("pressure_hpa"),
        }
        return raw_value, None  # no single safe-limit threshold per PoV table

    if sensor_type == "rgb":
        return None, None  # evaluated via CV/defect detection downstream, not a scalar

    return None, None


_FILE_EXT = {"rgb": "jpg", "thermal": "json", "lidar": "pcd"}


def _mock_file_uri(sensor_type: str, asset_id: str, sensor_type_run_id: str, record_id: str):
    """Heavy payloads (images, thermal matrices, point clouds) should be
    referenced by URI to object storage per the PoV, not inlined into the
    time-series record. There's no real S3 bucket in this mock, so the
    actual bytes/array still ride along in `data` for convenience — but
    the field is shaped exactly like the real pointer would be."""
    ext = _FILE_EXT.get(sensor_type)
    if not ext:
        return None
    return f"s3://inspection-evidence/{sensor_type_run_id}/{asset_id}/{sensor_type}/{record_id}.{ext}"


# ---------------------------------------------------------------- RGB ----
def generate_rgb_frame(locomotion_state="idle", anomaly_mode=False):
    r = RANGES["rgb"]
    w, h = r["width"], r["height"]
    blur = _noise_profile(locomotion_state)["rgb_blur"]

    if _HAS_PIL:
        arr = np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)
        # cheap "blur" stand-in for motion blur during locomotion: blend rows
        if blur > 0:
            k = max(1, int(blur * 10))
            arr = np.clip(arr.astype(np.int32) + np.random.randint(-k * 5, k * 5, arr.shape), 0, 255).astype(np.uint8)
        img = Image.fromarray(arr, "RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        payload = base64.b64encode(buf.getvalue()).decode("ascii")
        encoding = "jpeg_base64"
    else:
        # fallback: no PIL available, send raw random bytes b64-encoded
        payload = base64.b64encode(np.random.bytes(w * h * 3 // 50)).decode("ascii")
        encoding = "raw_b64_placeholder"

    return {
        "width": w,
        "height": h,
        "fps_nominal": r["fps"],
        "encoding": encoding,
        "motion_blur_level": round(blur, 2),
        "frame_b64": payload,
    }


# ------------------------------------------------------------ THERMAL ----
def generate_thermal_frame(locomotion_state="idle", anomaly_mode=False):
    r = RANGES["thermal"]
    w, h = r["width"], r["height"]
    base = np.random.uniform(r["temp_min_c"], r["temp_max_c"], (h, w))

    hotspot_prob = r["hotspot_probability"] * (3 if anomaly_mode else 1)
    has_hotspot = random.random() < hotspot_prob
    if has_hotspot:
        hx, hy = random.randint(0, w - 1), random.randint(0, h - 1)
        hot_temp = random.uniform(*r["hotspot_temp_c"])
        radius = random.randint(3, 10)
        yy, xx = np.ogrid[:h, :w]
        mask = (xx - hx) ** 2 + (yy - hy) ** 2 <= radius ** 2
        base[mask] = hot_temp

    matrix = np.round(base, 1)
    return {
        "width": w,
        "height": h,
        "unit": "celsius",
        "temp_matrix": matrix.tolist(),  # full matrix; consumer can downsample
        "temp_min_c": float(matrix.min()),
        "temp_max_c": float(matrix.max()),
        "temp_avg_c": round(float(matrix.mean()), 2),
        "hotspot_detected": bool(has_hotspot),
    }


# ---------------------------------------------------------------- GAS ----
def generate_gas_reading(locomotion_state="idle", anomaly_mode=False):
    r = RANGES["gas"]
    alarm_prob = r["alarm_probability"] * (5 if anomaly_mode else 1)
    alarm = random.random() < alarm_prob

    def sample(key, alarm_mult=6.0):
        lo, hi = r[key]
        val = random.uniform(lo, hi)
        if alarm and key != "o2_pct":
            val = hi * random.uniform(1.5, alarm_mult)
        return round(val, 3)

    o2 = round(random.uniform(*r["o2_pct"]) - (3.0 if alarm else 0.0), 2)

    return {
        "h2s_ppm": sample("h2s_ppm"),
        "co_ppm": sample("co_ppm"),
        "ch4_pct_lel": sample("ch4_pct_lel"),
        "voc_ppm": sample("voc_ppm"),
        "o2_pct": max(0.0, o2),
        "alarm_triggered": bool(alarm),
    }


# ---------------------------------------------------------- ENVIRONMENT --
def generate_env_reading(locomotion_state="idle", anomaly_mode=False):
    r = RANGES["env"]
    return {
        "ambient_temp_c": round(random.uniform(*r["temp_c"]), 2),
        "humidity_pct": round(random.uniform(*r["humidity_pct"]), 2),
        "pressure_hpa": round(random.uniform(*r["pressure_hpa"]), 2),
        "gas_resistance_kohm": round(random.uniform(*r["gas_resistance_kohm"]), 1),
    }


# ------------------------------------------------------------ ACOUSTIC ---
def generate_acoustic_reading(locomotion_state="idle", anomaly_mode=False):
    r = RANGES["acoustic"]
    n = r["frame_samples"]
    extra_noise_db = _noise_profile(locomotion_state)["acoustic_noise_db"]

    t = np.linspace(0, n / r["sample_rate_hz"], n, endpoint=False)
    tone_freq = random.uniform(200, 4000)
    waveform = 0.3 * np.sin(2 * np.pi * tone_freq * t)
    waveform += np.random.normal(0, 0.05 + extra_noise_db / 100.0, n)

    spectrum = np.abs(np.fft.rfft(waveform))
    spectrum = (spectrum / (spectrum.max() + 1e-9)).round(4)

    base_db_lo, base_db_hi = r["amplitude_db"]
    amplitude_db = round(random.uniform(base_db_lo, base_db_hi) + extra_noise_db, 1)

    return {
        "sample_rate_hz": r["sample_rate_hz"],
        "num_samples": n,
        "waveform_preview": np.round(waveform[:32], 4).tolist(),  # truncated preview
        "spectrum_preview": spectrum[:32].tolist(),
        "dominant_freq_hz": round(tone_freq, 1),
        "amplitude_db": amplitude_db,
        "ultrasonic_intensity": round(random.uniform(*r["ultrasonic_intensity"]), 3),
    }


# ----------------------------------------------------------- VIBRATION ---
def generate_vibration_reading(locomotion_state="idle", anomaly_mode=False):
    """WISE-2410-style 3-axis vibration.

    Real condition-monitoring alarms (and the WISE-2410's own ISO 10816
    compliance) are based on velocity RMS, not raw accel-g, so the AC
    vibration content (gravity excluded - a static 1g DC offset on Z
    would otherwise dominate any accel-based RMS/threshold) is what
    drives velocity_rms_mms below. A fault event (bearing wear/imbalance
    style) can spike it into ISO 10816's unsatisfactory zone.
    """
    r = RANGES["vibration"]
    mult = _noise_profile(locomotion_state)["vibration_mult"]
    n = 128

    fault_prob = r["fault_probability"] * (6 if anomaly_mode else 1)
    faulted = random.random() < fault_prob

    def axis_signal():
        rms_g = r["idle_accel_rms_g"] * mult * random.uniform(0.8, 1.3)
        if faulted:
            rms_g *= random.uniform(8.0, 20.0)
        return np.random.normal(0, rms_g, n)

    x, y, z = axis_signal(), axis_signal(), axis_signal()

    def stats(axis):
        rms = float(np.sqrt(np.mean(axis ** 2)))
        peak = float(np.max(np.abs(axis)))
        return round(rms, 4), round(peak, 4)

    rms_x, peak_x = stats(x)
    rms_y, peak_y = stats(y)
    rms_z, peak_z = stats(z)

    fft_mag = np.abs(np.fft.rfft(z))[: r["fft_bins"]]
    fft_mag = np.round(fft_mag / (fft_mag.max() + 1e-9), 4).tolist()

    # v = a / (2*pi*f) for a sinusoid; f_ref is a nominal dominant
    # frequency (typical rotating-machinery running speed) used to
    # convert the combined AC acceleration into an ISO 10816 velocity RMS.
    combined_rms_g = float(np.sqrt(rms_x ** 2 + rms_y ** 2 + rms_z ** 2))
    f_ref = r["velocity_ref_freq_hz"]
    velocity_rms_mms = round((combined_rms_g * 9.81) / (2 * np.pi * f_ref) * 1000.0, 3)

    return {
        "accel_g": {
            "x_rms": rms_x, "x_peak": peak_x,
            "y_rms": rms_y, "y_peak": peak_y,
            "z_rms": rms_z, "z_peak": peak_z,
        },
        "z_gravity_offset_g": 1.0,  # static 1g on the vertical axis; excluded from the vibration stats above
        "velocity_rms_mms": velocity_rms_mms,
        "fft_spectrum_z": fft_mag,
        "locomotion_multiplier_applied": mult,
        "fault_simulated": bool(faulted),
    }


# --------------------------------------------------------------- LIDAR ---
def generate_lidar_frame(locomotion_state="idle", anomaly_mode=False):
    """Livox Mid-360-style point cloud.

    The nearest-obstacle distance is simulated as its own variable
    (typical open-route clearance vs. a close-obstacle event) rather
    than taken as the min of the whole point cloud - the min of ~500
    points uniformly scattered across the full 0.1-40m sensing range
    lands near the blind zone almost every time by construction, which
    isn't a meaningful "closest obstacle" signal.
    """
    r = RANGES["lidar"]
    n = r["num_points"]
    az = np.random.uniform(0, 2 * np.pi, n)
    el = np.radians(np.random.uniform(r["fov_vertical_min_deg"], r["fov_vertical_max_deg"], n))
    dist = np.random.uniform(*r["range_m"], n)

    obstacle_prob = r["obstacle_probability"] * (5 if anomaly_mode else 1)
    has_close_obstacle = random.random() < obstacle_prob
    if has_close_obstacle:
        nearest_m = round(random.uniform(*r["nearest_obstacle_alert_m"]), 3)
    else:
        nearest_m = round(random.uniform(*r["nearest_obstacle_idle_m"]), 3)
    dist[0] = nearest_m  # splice the simulated nearest obstacle into the cloud

    x = dist * np.cos(el) * np.cos(az)
    y = dist * np.cos(el) * np.sin(az)
    z = dist * np.sin(el)
    intensity = np.random.uniform(0, 1, n)

    points = np.stack([x, y, z, intensity], axis=1).round(3).tolist()

    return {
        "num_points": n,
        "fov_horizontal_deg": r["fov_horizontal_deg"],
        "fov_vertical_deg": {"min": r["fov_vertical_min_deg"], "max": r["fov_vertical_max_deg"]},
        "range_m": {"min": float(dist.min()), "max": float(dist.max())},
        "nearest_obstacle_m": nearest_m,
        "obstacle_detected": bool(has_close_obstacle),
        "points_xyzi": points,  # [x, y, z, intensity] per point (downsampled)
    }


# ----------------------------------------------------------- RADIATION ---
def generate_radiation_reading(locomotion_state="idle", anomaly_mode=False):
    r = RANGES["radiation"]
    hotspot_prob = r["hotspot_probability"] * (5 if anomaly_mode else 1)
    is_hotspot = random.random() < hotspot_prob

    if is_hotspot:
        usv_h = round(random.uniform(*r["hotspot_usv_h"]), 3)
    else:
        usv_h = round(random.uniform(*r["background_usv_h"]), 4)

    cpm_lo, cpm_hi = r["background_cpm"]
    cpm = int(random.uniform(cpm_lo, cpm_hi) * (usv_h / r["background_usv_h"][1] if is_hotspot else 1))

    return {
        "dose_rate_usv_h": usv_h,
        "cpm": cpm,
        "alarm_triggered": bool(is_hotspot),
    }


GENERATORS = {
    "rgb": generate_rgb_frame,
    "thermal": generate_thermal_frame,
    "gas": generate_gas_reading,
    "env": generate_env_reading,
    "acoustic": generate_acoustic_reading,
    "vibration": generate_vibration_reading,
    "lidar": generate_lidar_frame,
    "radiation": generate_radiation_reading,
}

SENSOR_IDS = {
    "rgb": "rgb_cam_01",
    "thermal": "flir_lepton_3_5",
    "gas": "go2_multigas_module",
    "env": "adafruit_bme688",
    "acoustic": "sph0641lu4h",
    "vibration": "wise_2410",
    "lidar": "livox_mid360",
    "radiation": "gq_gmc500",
}


def build_reading(
    sensor_type: str,
    asset_id: str = None,
    inspection_run_id: str = "RUN-UNSPECIFIED",
    locomotion_state: str = "idle",
    anomaly_mode: bool = False,
) -> dict:
    """Build one evidence record matching the PoV's Section 4 data model:
    timestamp (UTC + local), asset_id, location, sensor_id/sensor_type,
    raw_value/file_uri, unit/threshold, quality_flag, inspection_run_id.
    """
    if sensor_type not in GENERATORS:
        raise ValueError(f"Unknown sensor_type '{sensor_type}'. Valid: {list(GENERATORS)}")

    record_id = uuid.uuid4().hex[:12]
    asset = _pick_asset(asset_id)
    dropped = _maybe_drop(locomotion_state)
    quality_flag = _quality_flag(dropped, anomaly_mode)

    data = {} if dropped else GENERATORS[sensor_type](locomotion_state, anomaly_mode)
    raw_value, checkpoint_breach = (None, None) if dropped else _raw_value_and_breach(sensor_type, data)

    if checkpoint_breach is None:
        checkpoint_status = "N/A"
    elif dropped:
        checkpoint_status = "N/A"
    else:
        checkpoint_status = "Warning" if checkpoint_breach else "Pass"

    unit_thr = UNIT_THRESHOLDS[sensor_type]

    return {
        "record_id": record_id,
        "timestamp_utc": _now_utc_iso(),
        "timestamp_local": _now_local_iso(),
        "inspection_run_id": inspection_run_id,
        "asset_id": asset["asset_id"],
        "location": {"x": asset["x"], "y": asset["y"], "zone": asset["zone"]},
        "sensor_id": SENSOR_IDS[sensor_type],
        "sensor_type": sensor_type,
        "unit": unit_thr["unit"],
        "threshold": unit_thr["threshold"],
        "quality_flag": quality_flag,
        "raw_value": raw_value,
        "file_uri": None if dropped else _mock_file_uri(sensor_type, asset["asset_id"], inspection_run_id, record_id),
        "checkpoint_status": checkpoint_status,
        "locomotion_state": locomotion_state,
        "data": data,  # full sensor-specific payload (frame bytes, matrix, point cloud, etc.)
    }
