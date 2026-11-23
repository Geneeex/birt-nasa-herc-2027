"""Synthetic software tests. These do not establish real camera accuracy."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np

from herc.camera import CameraConfig
from herc.vision import (
    BOARD_M,
    Calibration,
    RangeSettings,
    apply_filter,
    estimate_range,
    estimate_consistent_range,
    load_calibration,
    range_from_corners,
    validate_capture_metadata,
)

HAS_CV = importlib.util.find_spec("cv2") is not None


def calibration():
    config = CameraConfig(1280, 960, 0.0).as_dict()
    return Calibration(
        np.array([[1100.0, 0.0, 640.0], [0.0, 1100.0, 480.0], [0.0, 0.0, 1.0]]),
        np.zeros((5, 1)),
        (1280, 960),
        config,
        (0, 0, 4608, 2592),
        "synthetic-only",
    )


def board_image(
    name="red_circle",
    distance=5.0,
    centre_x=640,
    side=512,
    angle=0.0,
    wrong_shape=False,
    shape_scale=1.0,
):
    import cv2 as cv

    image = np.full((960, 1280, 3), 25, np.uint8)
    board = np.full((side, side, 3), 255, np.uint8)
    centre = side // 2
    height = int(side * 0.25 * shape_scale)
    colour = {
        "red_circle": (0, 0, 230),
        "blue_square": (230, 0, 0),
        "green_triangle": (0, 200, 0),
    }[name]
    if name == "red_circle" and not wrong_shape:
        cv.circle(board, (centre, centre), height // 2, colour, -1)
    elif name == "blue_square" and not wrong_shape:
        cv.rectangle(
            board,
            (centre - height // 2, centre - height // 2),
            (centre + height // 2, centre + height // 2),
            colour,
            -1,
        )
    else:
        half_width = height / np.sqrt(3)
        vertices = np.array(
            [
                [centre, centre - height / 2],
                [centre - half_width, centre + height / 2],
                [centre + half_width, centre + height / 2],
            ],
            np.int32,
        )
        cv.fillConvexPoly(board, vertices, colour)
    half = BOARD_M / 2
    obj = np.array(
        [[-half, -half, 0], [half, -half, 0], [half, half, 0], [-half, half, 0]],
        np.float64,
    )
    cal = calibration()
    translation = np.array(
        [(centre_x - 640) * distance / 1100, 0, distance], np.float64
    )
    corners, _ = cv.projectPoints(
        obj, np.array([angle, 0.0, 0.0]), translation, cal.matrix, cal.distortion
    )
    transform = cv.getPerspectiveTransform(
        np.float32([[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]]),
        corners.reshape(4, 2).astype(np.float32),
    )
    warped = cv.warpPerspective(board, transform, (1280, 960))
    mask = cv.warpPerspective(
        np.full((side, side), 255, np.uint8), transform, (1280, 960)
    )
    image[mask > 250] = warped[mask > 250]
    return image


class FilterTests(unittest.TestCase):
    def test_copy_and_channels(self):
        original = np.array([[[10, 20, 30]]], np.uint8)
        np.testing.assert_array_equal(apply_filter(original, "red"), [[[0, 0, 30]]])
        np.testing.assert_array_equal(apply_filter(original, "blue"), [[[10, 0, 0]]])
        np.testing.assert_array_equal(original, [[[10, 20, 30]]])
        self.assertFalse(np.shares_memory(original, apply_filter(original)))

    def test_unknown_filter_rejected(self):
        with self.assertRaises(ValueError):
            apply_filter(np.zeros((4, 4, 3), np.uint8), "purple")

    def test_camera_import_does_not_load_picamera(self):
        import sys

        self.assertNotIn("picamera2", sys.modules)


@unittest.skipUnless(HAS_CV, "OpenCV required for synthetic geometry tests")
class GeometryTests(unittest.TestCase):
    def test_all_target_types(self):
        for name in ("red_circle", "blue_square", "green_triangle"):
            with self.subTest(name=name):
                result = estimate_range(board_image(name), calibration())
                self.assertTrue(result["available"], result)
                self.assertEqual(result["target"], name)
                self.assertAlmostEqual(result["distance_m"], 5.0, delta=0.10)

    def test_tilted_board_pose(self):
        result = estimate_range(board_image(angle=0.20), calibration())
        self.assertTrue(result["available"], result)
        self.assertAlmostEqual(result["distance_m"], 5.0, delta=0.12)

    def test_reprojection_rejects_non_square_geometry(self):
        q = np.float64([[450, 350], [800, 370], [680, 650], [470, 580]])
        result = range_from_corners(
            q, calibration(), settings=RangeSettings(max_reprojection_px=0.01)
        )
        self.assertFalse(result["available"], result)

    def test_wrong_colour_shape_pair_rejected(self):
        result = estimate_range(
            board_image("red_circle", wrong_shape=True), calibration()
        )
        self.assertFalse(result["available"], result)

    def test_wrong_shape_scale_rejected_even_with_fallback(self):
        result = estimate_range(
            board_image(shape_scale=1.7),
            calibration(),
            RangeSettings(allow_frontal_shape_fallback=True),
        )
        self.assertFalse(result["available"], result)

    def test_absent_board_rejected(self):
        result = estimate_range(np.zeros((960, 1280, 3), np.uint8), calibration())
        self.assertFalse(result["available"])

    def test_clipped_board_rejected_by_default(self):
        result = estimate_range(board_image(distance=1.0), calibration())
        self.assertFalse(result["available"])

    def test_optional_shape_fallback_all_three(self):
        for name in ("red_circle", "blue_square", "green_triangle"):
            with self.subTest(name=name):
                result = estimate_range(
                    board_image(name, distance=1.0),
                    calibration(),
                    RangeSettings(allow_frontal_shape_fallback=True),
                )
                self.assertTrue(result["available"], result)
                self.assertAlmostEqual(result["distance_m"], 1.0, delta=0.04)
                self.assertIn("shape", result["method"])
                self.assertTrue(result["metadata"]["fallback"])

    def test_multiple_boards_are_ambiguous(self):
        left = board_image("red_circle", 10.0, 530)
        right = board_image("blue_square", 10.0, 750)
        image = np.maximum(left, right)
        result = estimate_range(image, calibration())
        self.assertFalse(result["available"], result)
        self.assertEqual(result["reason"], "ambiguous_multiple_boards")

    def test_dimension_mismatch(self):
        self.assertEqual(
            estimate_range(np.zeros((20, 20, 3), np.uint8), calibration())["reason"],
            "image_configuration_mismatch",
        )

    def test_consistency_and_motion_rejection(self):
        base = board_image(distance=5.0)
        good = estimate_consistent_range(
            [base, base.copy(), base.copy()], calibration()
        )
        self.assertTrue(good["available"], good)
        bad = estimate_consistent_range(
            [base, base.copy(), board_image(distance=6.0)], calibration()
        )
        self.assertEqual(bad["reason"], "inconsistent_distance")

    def test_capture_metadata_mismatch(self):
        cal = calibration()
        metadata = {
            "ScalerCrop": list(cal.scaler_crop),
            "LensPosition": 0.0,
            "camera_config": cal.camera_config,
        }
        validate_capture_metadata(cal, metadata)
        metadata["LensPosition"] = 1.0
        result = estimate_range(board_image(), cal, metadata=metadata)
        self.assertEqual(result["reason"], "capture_metadata_mismatch")


class CalibrationFileTests(unittest.TestCase):
    def test_exact_config_and_quality_gate(self):
        cal = calibration()
        document = {
            "schema_version": 1,
            "camera_matrix": cal.matrix.tolist(),
            "distortion_coefficients": [0] * 5,
            "image_size": list(cal.image_size),
            "camera_config": cal.camera_config,
            "observed_scaler_crop": list(cal.scaler_crop),
            "quality": {"image_count": 20, "rms_px": 0.2, "max_view_error_px": 0.5},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = load_calibration(path, cal.camera_config)
            self.assertEqual(loaded.image_size, (1280, 960))
            with self.assertRaises(ValueError):
                load_calibration(path, dict(cal.camera_config, lens_position=1.0))
            document["quality"]["rms_px"] = 3.0
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_calibration(path)


if __name__ == "__main__":
    unittest.main()
