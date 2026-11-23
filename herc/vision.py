"""Conservative calibrated target ranging; no fabricated calibration or valid guess.

Thresholds are engineering starting values, not NASA scoring tolerances. Validate
on real boards, light, camera and mounting before using an available result.
"""

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import numpy as np

BOARD_M = 1.2192
SHAPE_M = 0.3048


def _cv():
    try:
        import cv2

        return cv2
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for vision; install python3-opencv on the Pi"
        ) from exc


@dataclass
class Calibration:
    matrix: np.ndarray
    distortion: np.ndarray
    image_size: tuple[int, int]
    camera_config: dict
    scaler_crop: tuple[int, int, int, int]
    sha256: str = ""
    quality: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RangeSettings:
    edge_margin_px: int = 6
    min_board_side_px: float = 60.0
    min_shape_height_px: float = 25.0
    center_radius_fraction: float = (
        0.22  # Fraction of image width/height, elliptical gate.
    )
    max_reprojection_px: float = 2.0
    max_relative_spread: float = 0.05
    max_spread_ft: float = 1.0
    allow_frontal_shape_fallback: bool = False
    min_distance_ft: float = 3.0
    max_distance_ft: float = 85.0

    def __post_init__(self):
        if type(self.edge_margin_px) is not int or self.edge_margin_px < 0:
            raise ValueError("edge_margin_px must be a nonnegative integer")
        if type(self.allow_frontal_shape_fallback) is not bool:
            raise ValueError("allow_frontal_shape_fallback must be a boolean")
        for name in (
            "min_board_side_px",
            "min_shape_height_px",
            "center_radius_fraction",
            "max_reprojection_px",
            "max_relative_spread",
            "max_spread_ft",
            "min_distance_ft",
            "max_distance_ft",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        if self.center_radius_fraction > 0.5 or self.max_relative_spread > 1:
            raise ValueError("Invalid centre gate or relative spread")
        if self.min_distance_ft >= self.max_distance_ft:
            raise ValueError("Minimum distance must be below maximum distance")


def load_calibration(path, expected_config: dict | None = None) -> Calibration:
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported camera calibration schema")
    matrix = np.asarray(data["camera_matrix"], dtype=np.float64)
    distortion = np.asarray(data["distortion_coefficients"], dtype=np.float64).reshape(
        -1, 1
    )
    size = tuple(int(x) for x in data["image_size"])
    cfg = data["camera_config"]
    if (
        matrix.shape != (3, 3)
        or not np.isfinite(matrix).all()
        or matrix[0, 0] <= 0
        or matrix[1, 1] <= 0
    ):
        raise ValueError("Invalid camera intrinsic matrix")
    if not np.allclose(matrix[2], [0, 0, 1]) or abs(matrix[0, 1]) > 1e-9:
        raise ValueError("Unsupported intrinsic matrix")
    if distortion.size not in (4, 5, 8, 12, 14) or not np.isfinite(distortion).all():
        raise ValueError("Invalid distortion coefficients")
    if len(size) != 2 or min(size) < 64 or size != (cfg["width"], cfg["height"]):
        raise ValueError("Calibration image size/configuration mismatch")
    if cfg.get("format") != "RGB888" or not math.isfinite(float(cfg["lens_position"])):
        raise ValueError("Calibration must describe fixed-focus Picamera2 RGB888")
    if expected_config is not None and json.dumps(cfg, sort_keys=True) != json.dumps(
        expected_config, sort_keys=True
    ):
        raise ValueError(
            "Calibration differs from the exact active camera configuration"
        )
    crop = tuple(int(x) for x in data["observed_scaler_crop"])
    if len(crop) != 4 or min(crop[2:]) <= 0:
        raise ValueError("Missing measured sensor crop")
    quality = data["quality"]
    if (
        quality.get("image_count", 0) < 20
        or not math.isfinite(quality.get("rms_px", math.inf))
        or not math.isfinite(quality.get("max_view_error_px", math.inf))
        or quality["rms_px"] > 1.0
        or quality.get("max_view_error_px", math.inf) > 2.0
    ):
        raise ValueError("Calibration did not pass the stated fit-quality gate")
    return Calibration(
        matrix, distortion, size, cfg, crop, hashlib.sha256(raw).hexdigest(), quality
    )


def validate_capture_metadata(calibration: Calibration, metadata: dict) -> None:
    if tuple(metadata.get("ScalerCrop", ())) != calibration.scaler_crop:
        raise ValueError("Capture crop differs from calibration")
    focus = metadata.get("LensPosition")
    if (
        focus is None
        or not math.isfinite(float(focus))
        or abs(float(focus) - float(calibration.camera_config["lens_position"])) > 0.05
    ):
        raise ValueError("Capture focus differs from calibration")
    if metadata.get("camera_config") != calibration.camera_config:
        raise ValueError("Capture pixel configuration differs from calibration")


def apply_filter(bgr, name="none"):
    image = np.asarray(bgr)
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise ValueError("Expected an 8-bit BGR image")
    if name not in ("none", "red", "blue"):
        raise ValueError("Filter must be none, red or blue")
    result = image.copy()
    if name == "red":
        result[:, :, :2] = 0
    elif name == "blue":
        result[:, :, 1:] = 0
    return result


def _unavailable(reason, **metadata):
    return {
        "available": False,
        "reason": reason,
        "distance_m": None,
        "distance_ft": None,
        "method": None,
        "target": None,
        "metadata": metadata,
    }


def _ordered_quad(points):
    """Cyclic TL,TR,BR,BL order. Generic IPPE avoids IPPE_SQUARE's special order."""
    pts = np.asarray(points, np.float64).reshape(4, 2)
    centre = pts.mean(axis=0)
    pts = pts[np.argsort(np.arctan2(pts[:, 1] - centre[1], pts[:, 0] - centre[0]))]
    return np.roll(pts, -np.argmin(pts.sum(axis=1)), axis=0)


def range_from_corners(
    corners, calibration: Calibration, side_m=BOARD_M, settings=None
):
    """Geometry-only solver, also used by tests. Inputs must be undistorted pixels."""
    cv = _cv()
    settings = settings or RangeSettings()
    q = _ordered_quad(corners)
    if (
        not cv.isContourConvex(q.astype(np.float32))
        or abs(cv.contourArea(q.astype(np.float32))) < 16
    ):
        return _unavailable("invalid_quadrilateral")
    half = side_m / 2
    obj = np.array(
        [[-half, -half, 0], [half, -half, 0], [half, half, 0], [-half, half, 0]],
        np.float64,
    )
    try:
        solutions = cv.solvePnPGeneric(
            obj, q, calibration.matrix, np.zeros(5), flags=cv.SOLVEPNP_IPPE
        )
    except cv.error:
        return _unavailable("pose_solution_failed")
    candidates = []
    for rvec, tvec in zip(solutions[1], solutions[2]):
        if not np.isfinite(rvec).all() or not np.isfinite(tvec).all():
            continue
        rotation, _ = cv.Rodrigues(rvec)
        if np.any((obj @ rotation.T + tvec.reshape(1, 3))[:, 2] <= 0):
            continue
        projected, _ = cv.projectPoints(
            obj, rvec, tvec, calibration.matrix, np.zeros(5)
        )
        error = float(
            np.sqrt(np.mean(np.sum((projected.reshape(4, 2) - q) ** 2, axis=1)))
        )
        distance = float(np.linalg.norm(tvec))
        if np.isfinite(error) and error <= settings.max_reprojection_px:
            candidates.append((error, distance, tvec.reshape(3).tolist()))
    if not candidates:
        return _unavailable("reprojection_or_positive_depth_failed")
    candidates.sort(key=lambda item: item[0])
    best = candidates[0]
    if len(candidates) > 1 and candidates[1][0] <= best[0] + 0.5:
        if abs(candidates[1][1] - best[1]) / best[1] > 0.05:
            return _unavailable("ambiguous_planar_pose")
    if not settings.min_distance_ft <= best[1] / SHAPE_M <= settings.max_distance_ft:
        return _unavailable("outside_team_test_envelope")
    return {
        "available": True,
        "reason": None,
        "distance_m": best[1],
        "distance_ft": best[1] / SHAPE_M,
        "method": "board_ippe",
        "target": None,
        "metadata": {
            "reprojection_rms_px": best[0],
            "translation_m": best[2],
            "reference": "camera_optical_centre_to_board_centre",
            "corners_px": q.tolist(),
        },
    }


def _white_mask(image):
    cv = _cv()
    hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
    return cv.inRange(hsv, (0, 0, 90), (179, 70, 255))


def _shapes(image, min_height=20):
    cv = _cv()
    hsv = cv.cvtColor(image, cv.COLOR_BGR2HSV)
    masks = {
        "red_circle": cv.inRange(hsv, (0, 85, 45), (12, 255, 255))
        | cv.inRange(hsv, (168, 85, 45), (179, 255, 255)),
        "blue_square": cv.inRange(hsv, (90, 85, 45), (135, 255, 255)),
        "green_triangle": cv.inRange(hsv, (35, 75, 40), (85, 255, 255)),
    }
    found = []
    for name, mask in masks.items():
        contours, _ = cv.findContours(mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area, perimeter = cv.contourArea(contour), cv.arcLength(contour, True)
            if perimeter <= 0 or area < 100:
                continue
            x, y, width, height = cv.boundingRect(contour)
            if (
                height < min_height
                or x <= 2
                or y <= 2
                or x + width >= image.shape[1] - 2
                or y + height >= image.shape[0] - 2
            ):
                continue
            poly = cv.approxPolyDP(contour, 0.025 * perimeter, True)
            ratio = width / height
            valid = False
            if name == "red_circle":
                valid = (
                    len(poly) >= 7
                    and 4 * math.pi * area / perimeter**2 >= 0.78
                    and 0.85 <= ratio <= 1.15
                )
            elif name == "blue_square":
                valid = (
                    len(poly) == 4
                    and cv.isContourConvex(poly)
                    and 0.80 <= ratio <= 1.20
                    and area / (width * height) >= 0.80
                )
            elif len(poly) == 3 and cv.isContourConvex(poly):
                vertices = poly.reshape(3, 2).astype(float)
                lengths = [
                    np.linalg.norm(vertices[i] - vertices[(i + 1) % 3])
                    for i in range(3)
                ]
                valid = min(lengths) / max(lengths) >= 0.78
            if valid:
                moments = cv.moments(contour)
                centre = (
                    moments["m10"] / moments["m00"],
                    moments["m01"] / moments["m00"],
                )
                found.append(
                    {
                        "name": name,
                        "contour": contour,
                        "polygon": poly,
                        "bbox": (x, y, width, height),
                        "centre": centre,
                    }
                )
    return found


def _near_centre(point, calibration, settings):
    w, h = calibration.image_size
    cx, cy = calibration.matrix[0, 2], calibration.matrix[1, 2]
    return ((point[0] - cx) / (w * settings.center_radius_fraction)) ** 2 + (
        (point[1] - cy) / (h * settings.center_radius_fraction)
    ) ** 2 <= 1


def _verified_board(image, quad):
    cv = _cv()
    q = _ordered_quad(quad).astype(np.float32)
    dst = np.float32([[0, 0], [511, 0], [511, 511], [0, 511]])
    transform = cv.getPerspectiveTransform(q, dst)
    rectified = cv.warpPerspective(image, transform, (512, 512))
    if np.count_nonzero(_white_mask(rectified)[10:-10, 10:-10]) / (492 * 492) < 0.78:
        return None
    shapes = _shapes(rectified, 60)
    verified = []
    for shape in shapes:
        # 12-inch shape on a 48-inch board has 1/4 of the board's height.
        x, y, w, h = shape["bbox"]
        if 0.19 <= h / 512 <= 0.31 and min(x, y) > 8 and x + w < 504 and y + h < 504:
            verified.append(shape)
    return verified[0]["name"] if len(verified) == 1 else None


def _shape_fallback(image, calibration, settings):
    cv = _cv()
    shapes = [
        s
        for s in _shapes(image, settings.min_shape_height_px)
        if _near_centre(s["centre"], calibration, settings)
    ]
    if len(shapes) != 1:
        return _unavailable("no_unique_complete_shape")
    shape = shapes[0]
    x, y, w, h = shape["bbox"]
    # Fallback is for a board cut by the frame edge, not a complete board that
    # failed the known 12:48-inch size check.
    whites, _ = cv.findContours(
        _white_mask(image), cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE
    )
    clipped_surround = False
    for white_contour in whites:
        bx, by, bw, bh = cv.boundingRect(white_contour)
        touches_edge = (
            bx < 3
            or by < 3
            or bx + bw >= image.shape[1] - 3
            or by + bh >= image.shape[0] - 3
        )
        if (
            touches_edge
            and cv.pointPolygonTest(white_contour, shape["centre"], False) >= 0
        ):
            clipped_surround = True
    if not clipped_surround:
        return _unavailable("shape_fallback_requires_clipped_white_board")
    pad = max(5, int(0.2 * max(w, h)))
    roi = _white_mask(image)[
        max(0, y - pad) : min(image.shape[0], y + h + pad),
        max(0, x - pad) : min(image.shape[1], x + w + pad),
    ]
    # Require white surrounding the symbol, not merely a coloured object in scenery.
    if len(roi) == 0 or np.count_nonzero(roi) / roi.size < 0.40:
        return _unavailable("shape_has_no_white_surround")
    if shape["name"] == "blue_square":
        result = range_from_corners(shape["polygon"], calibration, SHAPE_M, settings)
        if result["available"]:
            result["method"] = "shape_square_ippe"
            result["metadata"]["reference"] = "camera_optical_centre_to_shape_centre"
    else:
        if shape["name"] == "green_triangle":
            vertices = shape["polygon"].reshape(3, 2)
            apex = vertices[np.argmin(vertices[:, 1])]
            base = vertices[np.argsort(vertices[:, 1])[1:]]
            if (
                abs(base[0, 1] - base[1, 1]) > 0.10 * h
                or abs(apex[0] - (x + w / 2)) > 0.15 * w
            ):
                return _unavailable("triangle_not_upright")
        fy = calibration.matrix[1, 1]
        z = SHAPE_M * fy / h
        cx, cy = shape["centre"]
        xn = (cx - calibration.matrix[0, 2]) / calibration.matrix[0, 0]
        yn = (cy - calibration.matrix[1, 2]) / fy
        distance = float(z * math.sqrt(1 + xn * xn + yn * yn))
        if (
            not settings.min_distance_ft
            <= distance / SHAPE_M
            <= settings.max_distance_ft
        ):
            return _unavailable("outside_team_test_envelope")
        result = {
            "available": True,
            "reason": None,
            "distance_m": distance,
            "distance_ft": distance / SHAPE_M,
            "method": "shape_height_frontal_assumption",
            "target": shape["name"],
            "metadata": {
                "reference": "camera_optical_centre_to_shape_centre",
                "assumptions": [
                    "upright target",
                    "front-facing target",
                    "level camera",
                ],
                "shape_height_px": h,
            },
        }
    if result["available"]:
        result["target"] = shape["name"]
        result["metadata"]["fallback"] = True
    return result


def estimate_range(bgr, calibration: Calibration, settings=None, metadata=None):
    cv = _cv()
    settings = settings or RangeSettings()
    image = np.asarray(bgr)
    if (
        image.shape != (calibration.image_size[1], calibration.image_size[0], 3)
        or image.dtype != np.uint8
    ):
        return _unavailable("image_configuration_mismatch")
    if metadata is not None:
        try:
            validate_capture_metadata(calibration, metadata)
        except ValueError as exc:
            return _unavailable("capture_metadata_mismatch", detail=str(exc))
    # Use the original image; filters must never be applied before ranging.
    image = cv.undistort(
        image, calibration.matrix, calibration.distortion, None, calibration.matrix
    )
    white = _white_mask(image)
    contours, _ = cv.findContours(white, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    candidates = []
    margin = settings.edge_margin_px
    for contour in contours:
        if cv.contourArea(contour) < settings.min_board_side_px**2:
            continue
        poly = cv.approxPolyDP(contour, 0.015 * cv.arcLength(contour, True), True)
        if len(poly) != 4 or not cv.isContourConvex(poly):
            continue
        q = _ordered_quad(poly)
        if (
            np.any(q[:, 0] < margin)
            or np.any(q[:, 1] < margin)
            or np.any(q[:, 0] >= image.shape[1] - margin)
            or np.any(q[:, 1] >= image.shape[0] - margin)
        ):
            continue
        sides = [np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]
        if min(sides) < settings.min_board_side_px or min(sides) / max(sides) < 0.35:
            continue
        if not _near_centre(q.mean(axis=0), calibration, settings):
            continue
        name = _verified_board(image, q)
        if name is not None:
            candidates.append((q, name))
    if len(candidates) > 1:
        return _unavailable(
            "ambiguous_multiple_boards", candidate_count=len(candidates)
        )
    if len(candidates) == 1:
        corners, name = candidates[0]
        result = range_from_corners(corners, calibration, settings=settings)
        if result["available"]:
            result["target"] = name
    elif settings.allow_frontal_shape_fallback:
        result = _shape_fallback(image, calibration, settings)
    else:
        result = _unavailable("no_unique_complete_verified_board")
    result["metadata"]["calibration_sha256"] = calibration.sha256
    result["metadata"]["field_accuracy_verified"] = False
    return result


def estimate_consistent_range(
    images, calibration: Calibration, settings=None, metadata=None
):
    settings = settings or RangeSettings()
    if len(images) != 3:
        return _unavailable("three_observations_required")
    if metadata is not None and len(metadata) != 3:
        return _unavailable("three_metadata_records_required")
    observations = [
        estimate_range(
            im, calibration, settings, None if metadata is None else metadata[i]
        )
        for i, im in enumerate(images)
    ]
    if not all(item["available"] for item in observations):
        return _unavailable("observation_unavailable", observations=observations)
    if len({(x["method"], x["target"]) for x in observations}) != 1:
        return _unavailable("target_or_method_changed", observations=observations)
    values = [x["distance_m"] for x in observations]
    median = float(np.median(values))
    spread = max(values) - min(values)
    if spread > min(
        settings.max_relative_spread * median, settings.max_spread_ft * SHAPE_M
    ):
        return _unavailable("inconsistent_distance", observations=observations)
    result = dict(observations[values.index(sorted(values)[1])])
    result["metadata"] = dict(result["metadata"], observation_count=3, spread_m=spread)
    return result
