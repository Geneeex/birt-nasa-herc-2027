"""Offline software demonstration. No radio, GPIO, serial or camera is opened.

All images, intrinsics and motion are synthetic and evidence is labelled
SIMULATION. This exercises the actual parser/executor/vision/evidence pipeline;
it does not establish reception, motor performance or camera accuracy.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import numpy as np

from herc.camera import CameraConfig, Capture
from herc.controller import execute
from herc.evidence import Evidence, claim_attempt
from herc.pan import PanMap
from herc.protocol import parse_sequence
from herc.vision import BOARD_M, Calibration, RangeSettings, _cv


def synthetic_calibration():
    config = CameraConfig(1280, 960, 0.0).as_dict()
    return Calibration(
        np.float64([[1100, 0, 640], [0, 1100, 480], [0, 0, 1]]),
        np.zeros((5, 1)),
        (1280, 960),
        config,
        (0, 0, 4608, 2592),
        sha256="SIMULATION_ONLY_NOT_A_REAL_CALIBRATION",
    )


def synthetic_board(name="red_circle", distance_m=6.096):
    cv = _cv()
    side, centre, height = 512, 256, 128
    board = np.full((side, side, 3), 255, np.uint8)
    if name == "red_circle":
        cv.circle(board, (centre, centre), height // 2, (0, 0, 230), -1)
    elif name == "blue_square":
        cv.rectangle(
            board,
            (centre - 64, centre - 64),
            (centre + 64, centre + 64),
            (230, 0, 0),
            -1,
        )
    elif name == "green_triangle":
        halfwidth = height / np.sqrt(3)
        vertices = np.int32(
            [
                [centre, centre - 64],
                [centre - halfwidth, centre + 64],
                [centre + halfwidth, centre + 64],
            ]
        )
        cv.fillConvexPoly(board, vertices, (0, 200, 0))
    else:
        raise ValueError("Unknown synthetic target")
    cal = synthetic_calibration()
    half = BOARD_M / 2
    object_points = np.float64(
        [[-half, -half, 0], [half, -half, 0], [half, half, 0], [-half, half, 0]]
    )
    corners, _ = cv.projectPoints(
        object_points,
        np.zeros(3),
        np.float64([0, 0, distance_m]),
        cal.matrix,
        cal.distortion,
    )
    transform = cv.getPerspectiveTransform(
        np.float32([[0, 0], [511, 0], [511, 511], [0, 511]]),
        corners.reshape(4, 2).astype(np.float32),
    )
    return cv.warpPerspective(board, transform, (1280, 960), borderValue=(25, 25, 25))


class SimulationMotion:
    def __init__(self):
        self.events = []
        self.pulse_us = 1500

    def begin(self):
        self.events.append(["begin"])

    def move(self, pulse_us):
        self.pulse_us = pulse_us
        self.events.append(["move", pulse_us])

    def check(self):
        pass

    def finish(self):
        self.events.append(["finish"])

    def stop(self):
        self.events.append(["stop"])


class SimulationCamera:
    def __init__(self, motion, distance_ft=20.0, blank=False):
        self.motion = motion
        self.distance_ft = distance_ft
        self.blank = blank
        self.calibration = synthetic_calibration()
        self.capture_count = 0

    def capture(self):
        self.capture_count += 1
        name = (
            "blue_square"
            if self.motion.pulse_us > 1500
            else "green_triangle" if self.motion.pulse_us < 1500 else "red_circle"
        )
        image = (
            np.zeros((960, 1280, 3), np.uint8)
            if self.blank
            else synthetic_board(name, self.distance_ft * 0.3048)
        )
        metadata = {
            "simulation": True,
            "synthetic_target": name,
            "synthetic_reference_distance_ft": self.distance_ft,
            "ScalerCrop": list(self.calibration.scaler_crop),
            "LensPosition": 0.0,
            "camera_config": self.calibration.camera_config,
            "timestamp_basis": "software_simulation_creation_time_not_camera_exposure",
        }
        return Capture(
            image,
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            metadata,
        )


def simulation_packet(sequence):
    def address(name, last):
        return bytes(ord(character) << 1 for character in name.ljust(6)) + bytes(
            [0x60 | int(last)]
        )

    # These identifiers are local fixture data only, never a NASA configuration.
    return (
        address("APTEST", False)
        + address("SIMUL", True)
        + b"\x03\xf0>"
        + sequence.encode("ascii")
    )


def simulation_pan():
    return PanMap(
        {
            "measured": False,
            "points": [[-90, 900], [0, 1500], [90, 2100]],
            "settle_seconds": 0.2,
        },
        require_measured=False,
    )


def run(output="simulation_runs", sequence="AAA B E F D C H G D ZZZ", distance_ft=20.0):
    if not 3 <= distance_ft <= 85:
        raise ValueError("Synthetic distance must be between 3 and 85 feet")
    pan = simulation_pan()
    parsed = parse_sequence(
        simulation_packet(sequence),
        source_callsign="SIMUL",
        source_ssids=[0],
        pan_min=pan.minimum,
        pan_max=pan.maximum,
    )
    root = Path(output)
    excursion = "SIMULATION_" + uuid4().hex[:12]
    claim_attempt(root, excursion)
    evidence = Evidence(root, parsed, mode="SIMULATION", excursion_id=excursion)
    evidence.record["simulation_notice"] = (
        "Synthetic images, calibration and motion only. No NASA transmission or physical hardware was used."
    )
    evidence.flush()
    motion = SimulationMotion()
    camera = SimulationCamera(motion, distance_ft)
    execute(parsed, pan, motion, camera, camera.calibration, evidence, RangeSettings())
    evidence.record["simulated_motion_events"] = motion.events
    evidence.flush()
    return evidence.path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="simulation_runs")
    parser.add_argument("--sequence", default="AAA B E F D C H G D ZZZ")
    parser.add_argument("--distance-ft", type=float, default=20.0)
    args = parser.parse_args()
    path = run(args.output, args.sequence, args.distance_ft)
    print("SIMULATION evidence:", path)
    print("View offline: python -m herc.review --root", args.output)
    print(
        "Then open http://127.0.0.1:8000 on this computer. No hardware readiness is implied."
    )


if __name__ == "__main__":
    main()
