from typing import Any, Optional
from pydantic import BaseModel


class Location(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    zone: Optional[str] = None


class SensorEnvelope(BaseModel):
    """Evidence-record schema per the Hazardous Area Inspection PoV,
    Section 4 ('Data Model: What Each Record Should Contain'). Every
    reading, regardless of sensor_type, is returned in this shape so a
    consuming app can handle all sensor types with one code path
    before branching into `data` for the sensor-specific payload."""
    record_id: str
    timestamp_utc: str
    timestamp_local: str
    inspection_run_id: str
    asset_id: str
    location: Location
    sensor_id: str
    sensor_type: str
    unit: str
    threshold: Optional[Any] = None
    quality_flag: str            # "Good" | "Low confidence" | "Calibration overdue" | "Missing data"
    raw_value: Optional[Any] = None
    file_uri: Optional[str] = None
    checkpoint_status: str       # "Pass" | "Warning" | "N/A"
    locomotion_state: str
    data: dict                   # full sensor-specific payload (frame, matrix, point cloud, ...)


class ConfigUpdate(BaseModel):
    locomotion_state: Optional[str] = None     # "idle" | "walking" | "running"
    rate_hz: Optional[dict] = None              # {"rgb": 10, "lidar": 5, ...}
    anomaly_mode: Optional[bool] = None          # force elevated anomaly probabilities
    alert_sustain_seconds: Optional[float] = None  # how long a checkpoint must stay "Warning" before an alert fires
    alert_webhook_url: Optional[str] = None        # POSTed with the alert payload when set; "" clears it


class AlertRecord(BaseModel):
    """Fired when a sensor's checkpoint_status stays 'Warning' continuously
    for at least alert_sustain_seconds, so a consuming app can validate its
    own alerting path against something other than single noisy readings."""
    alert_id: str
    triggered_at_utc: str
    sensor_type: str
    sensor_id: str
    asset_id: str
    zone: Optional[str] = None
    inspection_run_id: str
    raw_value: Optional[Any] = None
    unit: str
    threshold: Optional[Any] = None
    sustained_seconds: float
