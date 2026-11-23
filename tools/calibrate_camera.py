"""Capture real calibration views on the Pi; fit their exact pixel configuration.

Examples (run from package root):
  python -m tools.calibrate_camera capture --images calibration/images
  python -m tools.calibrate_camera fit --images calibration/images --square-mm 25
The square pitch must be measured. No example intrinsic values are supplied.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from herc.camera import Camera, CameraConfig
from herc.vision import _cv


def capture(args):
    cv = _cv()
    folder = Path(args.images)
    folder.mkdir(parents=True, exist_ok=True)
    config = CameraConfig(
        args.width,
        args.height,
        args.lens_position,
        tuple(args.crop) if args.crop else None,
    )
    session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print("Use a rigid, flat measured checkerboard. Keep focus unchanged.")
    print("Move it across the frame and vary distance/tilt between views.")
    with Camera(config) as camera:
        for index in range(args.count):
            input(
                f"Position checkerboard for view {index+1}/{args.count}; press Enter to capture: "
            )
            frame = camera.capture()
            filename = folder / f"{session}_{index:03d}.png"
            if not cv.imwrite(str(filename), frame.bgr):
                raise RuntimeError("Could not save calibration image")
            record = {
                "captured_at_utc": frame.captured_at_utc,
                "metadata": frame.metadata,
                "sha256": hashlib.sha256(filename.read_bytes()).hexdigest(),
            }
            filename.with_suffix(".json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
            print(f"Saved {filename.name}; inspect focus before fitting.")


def fit(args):
    cv = _cv()
    if (
        not math.isfinite(args.square_mm)
        or args.square_mm <= 0
        or args.columns < 3
        or args.rows < 3
    ):
        raise ValueError(
            "Supply positive measured square pitch and inner-corner dimensions >=3"
        )
    images = sorted(Path(args.images).glob("*.png"))
    pattern = (args.columns, args.rows)
    object_template = np.zeros((args.columns * args.rows, 3), np.float32)
    object_template[:, :2] = (
        np.mgrid[0 : args.columns, 0 : args.rows].T.reshape(-1, 2)
        * args.square_mm
        / 1000
    )
    object_points, image_points, accepted = [], [], []
    baseline_config = baseline_crop = image_size = None
    for filename in images:
        record_path = filename.with_suffix(".json")
        if not record_path.exists():
            raise ValueError(
                f"Missing capture metadata for {filename.name}; do not mix untracked images"
            )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record["sha256"] != hashlib.sha256(filename.read_bytes()).hexdigest():
            raise ValueError(f"Image changed since capture: {filename.name}")
        metadata = record["metadata"]
        config = metadata["camera_config"]
        crop = metadata.get("ScalerCrop")
        if crop is None or len(crop) != 4:
            raise ValueError("Capture does not record actual sensor ScalerCrop")
        if abs(float(metadata["LensPosition"]) - float(config["lens_position"])) > 0.05:
            raise ValueError("Capture lens position differs from requested fixed focus")
        if baseline_config is None:
            baseline_config, baseline_crop = config, crop
        elif config != baseline_config or crop != baseline_crop:
            raise ValueError("Mixed camera pixel/focus/crop configurations")
        image = cv.imread(str(filename))
        if image is None:
            raise ValueError(f"Unreadable image: {filename.name}")
        size = (image.shape[1], image.shape[0])
        if size != (config["width"], config["height"]) or (
            image_size is not None and image_size != size
        ):
            raise ValueError("Mixed image dimensions")
        image_size = size
        gray = cv.cvtColor(image, cv.COLOR_BGR2GRAY)
        found, corners = cv.findChessboardCornersSB(
            gray, pattern, flags=cv.CALIB_CB_EXHAUSTIVE | cv.CALIB_CB_ACCURACY
        )
        if not found:
            print(f"Rejected: checkerboard not found in {filename.name}")
            continue
        object_points.append(object_template.copy())
        image_points.append(corners.astype(np.float32))
        accepted.append({"file": filename.name, "sha256": record["sha256"]})
    if len(accepted) < 20:
        raise ValueError(f"Need >=20 usable real views; found {len(accepted)}")
    centers = np.asarray(
        [points.reshape(-1, 2).mean(axis=0) for points in image_points]
    )
    spans = np.ptp(centers, axis=0) / np.asarray(image_size)
    areas = np.asarray(
        [cv.contourArea(cv.convexHull(points)) for points in image_points]
    )
    if min(spans) < 0.15 or max(areas) / min(areas) < 1.5:
        raise ValueError(
            "Views need more image-position and distance diversity; do not fit repeated views"
        )
    rms, matrix, distortion, rotations, translations = cv.calibrateCamera(
        object_points, image_points, image_size, None, None
    )
    errors = []
    for obj, observed, rvec, tvec in zip(
        object_points, image_points, rotations, translations
    ):
        projected, _ = cv.projectPoints(obj, rvec, tvec, matrix, distortion)
        errors.append(
            float(np.sqrt(np.mean(np.sum((projected - observed) ** 2, axis=2))))
        )
    if not np.isfinite(matrix).all() or rms > 1.0 or max(errors) > 2.0:
        raise ValueError(
            f"Fit rejected: RMS {rms:.3f}px, worst view {max(errors):.3f}px"
        )
    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "camera_config": baseline_config,
        "observed_scaler_crop": baseline_crop,
        "image_size": list(image_size),
        "camera_matrix": matrix.tolist(),
        "distortion_coefficients": distortion.reshape(-1).tolist(),
        "checkerboard": {
            "inner_columns": args.columns,
            "inner_rows": args.rows,
            "measured_square_mm": args.square_mm,
        },
        "quality": {
            "rms_px": float(rms),
            "max_view_error_px": max(errors),
            "image_count": len(accepted),
            "view_errors_px": errors,
            "centre_span_fraction": spans.tolist(),
        },
        "calibration_images": accepted,
        "field_range_accuracy_verified": False,
        "note": "A successful fit is not a field-accuracy or competition acceptance test.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"Saved {output}; RMS {rms:.3f}px. Now perform independent tape-distance tests."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("capture")
    p.add_argument("--images", default="calibration/images")
    p.add_argument("--count", type=int, default=25)
    p.add_argument("--width", type=int, default=4608)
    p.add_argument("--height", type=int, default=2592)
    p.add_argument("--lens-position", type=float, default=0.0)
    p.add_argument("--crop", type=int, nargs=4)
    p.set_defaults(func=capture)
    p = sub.add_parser("fit")
    p.add_argument("--images", default="calibration/images")
    p.add_argument("--output", default="calibration/camera.json")
    p.add_argument("--columns", type=int, default=9)
    p.add_argument("--rows", type=int, default=6)
    p.add_argument("--square-mm", type=float, required=True)
    p.set_defaults(func=fit)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"Calibration failed: {exc}\n")


if __name__ == "__main__":
    main()
