"""Regression checks for configuration, supervision and device replies."""

from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import numpy as np

from herc.camera import Camera, CameraConfig
from herc.controller import live, validate_live_configuration
from herc.hardware import HardwareConfig
from herc.supervision import require_watchdog
from herc.vision import RangeSettings
from tools import calibrate_camera


def live_config():
    return {
        "schema": 2,
        "hardware": HardwareConfig().__dict__,
        "excursion_id": "test_excursion_1",
        "clock_check_utc": datetime.now(timezone.utc).isoformat(),
        "execution_limit_seconds": 180,
        "kiss_port": 8001,
        "nasa": {
            "frequency_hz": 145000000,
            "source_callsign": "TEST",
            "source_ssids": [0],
            "confirmation_reference": "UNIT_TEST_ONLY",
            "remote_arm_confirmed": True,
            "whole_packet_sequence_confirmed": True,
            "digital_filter_confirmed": True,
            "range_reference_confirmed": True,
        },
    }


class ConfigurationTests(unittest.TestCase):
    def test_valid_explicit_configuration(self):
        validate_live_configuration(live_config())

    def test_invalid_live_values_are_rejected(self):
        cases = {
            "excursion_id": ["SET_IN_PIT", "../run", "", None],
            "schema": [True, 1, 3],
            "kiss_port": [True, 0, 65536, "8001"],
            "execution_limit_seconds": [True, float("nan"), float("inf"), 0, 181],
            "clock_check_utc": [None, "2020-01-01", ""],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    config = live_config()
                    config[key] = value
                    validate_live_configuration(config)

    def test_invalid_radio_identity_is_rejected(self):
        for key, value in [
            ("frequency_hz", float("nan")),
            ("frequency_hz", "145000000"),
            ("source_callsign", "TOOLONG"),
            ("source_callsign", "TEST-0"),
            ("source_ssids", [True]),
            ("source_ssids", [16]),
            ("source_ssids", "0"),
            ("confirmation_reference", "   "),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                config = live_config()
                config["nasa"][key] = value
                validate_live_configuration(config)

    def test_invalid_range_settings_are_rejected(self):
        for value in [float("nan"), float("inf"), -1, 0, True, "2"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                RangeSettings(max_reprojection_px=value)
        for values in [
            {"edge_margin_px": 1.5},
            {"center_radius_fraction": 0.8},
            {"allow_frontal_shape_fallback": "false"},
            {"min_distance_ft": 90, "max_distance_ft": 85},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                RangeSettings(**values)

    def test_camera_configuration_rejects_wrong_types(self):
        for values in [
            {"width": 128.5},
            {"height": True},
            {"lens_position": float("nan")},
            {"lens_position": True},
            {"scaler_crop": "test"},
            {"scaler_crop": [0, 0, 128.5, 128]},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                CameraConfig(**values)

    def test_calibration_default_matches_example_configuration(self):
        with (
            patch("sys.argv", ["calibrate_camera", "fit", "--square-mm", "25"]),
            patch.object(calibrate_camera, "fit") as fit,
        ):
            calibrate_camera.main()
        self.assertEqual(fit.call_args.args[0].output, "calibration/camera.json")


class WatchdogTests(unittest.TestCase):
    def environment(self):
        return {
            "NOTIFY_SOCKET": "TEST_SOCKET",
            "WATCHDOG_USEC": "15000000",
            "WATCHDOG_PID": str(os.getpid()),
        }

    def test_documented_supervisor_is_accepted(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            require_watchdog()

    def test_missing_or_invalid_supervisor_is_rejected(self):
        cases = [
            {},
            {"WATCHDOG_USEC": "0"},
            {"WATCHDOG_USEC": "15000001"},
            {"WATCHDOG_USEC": "invalid"},
            {"WATCHDOG_PID": "0"},
            {"NOTIFY_SOCKET": ""},
        ]
        for values in cases:
            env = self.environment() if values else {}
            env.update(values)
            with (
                self.subTest(values=values),
                patch.dict(os.environ, env, clear=True),
                self.assertRaises(RuntimeError),
            ):
                require_watchdog()

    def test_live_refuses_unsupervised_start_before_hardware(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(live_config()), encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("herc.hardware.PiMotion") as device,
            ):
                with self.assertRaisesRegex(RuntimeError, "watchdog"):
                    live(path)
            device.assert_not_called()


class CameraAdapterTests(unittest.TestCase):
    def fixture(self, focus=0.0):
        config = CameraConfig(width=64, height=64)
        camera = Camera(config)
        request = Mock()
        request.make_array.return_value = np.zeros((64, 64, 3), dtype=np.uint8)
        request.get_metadata.return_value = {
            "LensPosition": focus,
            "SensorTimestamp": 1_000_000_000,
            "ExposureTime": 10000,
        }
        camera._camera = Mock()
        camera._camera.capture_request.return_value = request
        return camera, request

    def test_capture_uses_fresh_request_and_releases_it(self):
        camera, request = self.fixture()
        with (
            patch("herc.camera.time.time_ns", return_value=1_700_000_000_000_000_000),
            patch("herc.camera.time.monotonic_ns", return_value=1_100_000_000),
        ):
            capture = camera.capture()
        camera._camera.capture_request.assert_called_once_with(flush=True)
        request.release.assert_called_once()
        self.assertEqual(capture.metadata["camera_config"], camera.config.as_dict())
        self.assertFalse(np.shares_memory(capture.bgr, request.make_array.return_value))
        self.assertEqual(capture.captured_at_utc, "2023-11-14T22:13:19.890+00:00")

    def test_nonfinite_focus_releases_request_and_fails(self):
        camera, request = self.fixture(float("nan"))
        with self.assertRaisesRegex(RuntimeError, "focus"):
            camera.capture()
        request.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
