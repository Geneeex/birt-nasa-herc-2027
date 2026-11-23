"""Picamera2 adapter. Importing this module does not require Pi hardware.

RGB888 is BGR byte order in Picamera2, as documented by Raspberry Pi.
The returned array is an owned, unfiltered copy. No preview is required.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import time
from typing import Any


@dataclass(frozen=True)
class CameraConfig:
    width: int = 4608
    height: int = 2592
    lens_position: float = 0.0
    scaler_crop: tuple[int, int, int, int] | None = None
    format: str = "RGB888"

    def as_dict(self) -> dict:
        data = asdict(self)
        if data["scaler_crop"] is not None:
            data["scaler_crop"] = list(data["scaler_crop"])
        return data

    def __post_init__(self):
        if (
            type(self.width) is not int
            or type(self.height) is not int
            or self.width < 64
            or self.height < 64
            or self.format != "RGB888"
        ):
            raise ValueError("Use an RGB888 image size of at least 64 x 64 pixels")
        if (
            isinstance(self.lens_position, bool)
            or not isinstance(self.lens_position, (int, float))
            or not math.isfinite(self.lens_position)
            or self.lens_position < 0
        ):
            raise ValueError("lens_position must be a nonnegative fixed dioptre value")
        if self.scaler_crop is not None and (
            not isinstance(self.scaler_crop, (tuple, list))
            or len(self.scaler_crop) != 4
            or any(type(x) is not int for x in self.scaler_crop)
            or min(self.scaler_crop[:2]) < 0
            or min(self.scaler_crop[2:]) <= 0
        ):
            raise ValueError("Invalid ScalerCrop rectangle")


@dataclass
class Capture:
    bgr: Any
    captured_at_utc: str
    metadata: dict


def _json_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


class Camera:
    def __init__(self, config: CameraConfig | dict | None = None):
        self.config = (
            CameraConfig(**config)
            if isinstance(config, dict)
            else config or CameraConfig()
        )
        self._camera = None

    def start(self):
        if self._camera is not None:
            return self
        try:
            from picamera2 import Picamera2
            from libcamera import controls
        except ImportError as exc:
            raise RuntimeError(
                "Pi camera requires Raspberry Pi OS Picamera2 and libcamera"
            ) from exc
        camera = Picamera2()
        try:
            if "imx708" not in str(camera.camera_properties.get("Model", "")).lower():
                raise RuntimeError(
                    "Expected Camera Module 3 (IMX708); verify Standard lens separately"
                )
            settings = {
                "AfMode": controls.AfModeEnum.Manual,
                "LensPosition": self.config.lens_position,
            }
            if self.config.scaler_crop is not None:
                settings["ScalerCrop"] = tuple(self.config.scaler_crop)
            cfg = camera.create_still_configuration(
                main={
                    "size": (self.config.width, self.config.height),
                    "format": "RGB888",
                },
                controls=settings,
                buffer_count=3,
                queue=False,
            )
            camera.configure(cfg)
            actual = camera.camera_configuration()["main"]
            if (
                tuple(actual["size"]) != (self.config.width, self.config.height)
                or actual["format"] != "RGB888"
            ):
                raise RuntimeError("Camera changed requested pixel configuration")
            camera.start(show_preview=False)
            time.sleep(
                1.0
            )  # Initial exposure/focus settling; field validation remains necessary.
            self._camera = camera
            return self
        except Exception:
            camera.close()
            raise

    def capture(self) -> Capture:
        if self._camera is None:
            raise RuntimeError("Camera is not started")
        # flush=True prevents a pre-pan buffered image from being used for a new command.
        request = self._camera.capture_request(flush=True)
        try:
            bgr = request.make_array("main").copy()
            metadata = _json_value(request.get_metadata())
            if bgr.shape != (self.config.height, self.config.width, 3):
                raise RuntimeError("Captured image has an unexpected shape")
            focus = metadata.get("LensPosition")
            if (
                focus is None
                or not math.isfinite(float(focus))
                or abs(float(focus) - self.config.lens_position) > 0.05
            ):
                raise RuntimeError(
                    "Fixed focus has not reached the requested lens position"
                )
            if self.config.scaler_crop is not None and tuple(
                metadata.get("ScalerCrop", ())
            ) != tuple(self.config.scaler_crop):
                raise RuntimeError("Captured crop differs from the configured crop")
            # SensorTimestamp is monotonic readout time, not Unix time. Map it to the
            # current wall clock and subtract ExposureTime for first-row exposure start.
            sensor_ns = metadata.get("SensorTimestamp")
            if sensor_ns is None:
                raise RuntimeError("Camera supplied no SensorTimestamp")
            wall_ns, monotonic_ns = time.time_ns(), time.monotonic_ns()
            exposure_ns = int(metadata.get("ExposureTime", 0)) * 1000
            timestamp_ns = wall_ns - monotonic_ns + int(sensor_ns) - exposure_ns
            if abs(timestamp_ns - wall_ns) > 10_000_000_000:
                raise RuntimeError(
                    "Sensor timestamp cannot be mapped to the system clock"
                )
            timestamp = datetime.fromtimestamp(
                timestamp_ns / 1e9, timezone.utc
            ).isoformat(timespec="milliseconds")
            metadata["timestamp_basis"] = (
                "sensor_first_row_exposure_start_mapped_to_system_UTC"
            )
            metadata["camera_config"] = self.config.as_dict()
            metadata["clock_sync_verified"] = (
                False  # Operator's offline RTC check is separate evidence.
            )
            return Capture(bgr, timestamp, metadata)
        finally:
            request.release()

    def close(self):
        if self._camera is not None:
            camera, self._camera = self._camera, None
            try:
                camera.stop()
            finally:
                camera.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
