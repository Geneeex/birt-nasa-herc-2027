"""Measured angle to servo pulse mapping. No nominal servo-angle assumptions."""

import json
import math
from pathlib import Path


class PanMap:
    def __init__(self, data, require_measured=True):
        if require_measured and data.get("measured") is not True:
            raise ValueError("A measured pan calibration is required")
        self.points = [(float(p[0]), float(p[1])) for p in data.get("points", [])]
        if len(self.points) < 3 or any(
            not math.isfinite(x) for p in self.points for x in p
        ):
            raise ValueError("At least three finite angle/pulse pairs required")
        if any(b[0] <= a[0] for a, b in zip(self.points, self.points[1:])):
            raise ValueError("Angles must increase strictly")
        deltas = [b[1] - a[1] for a, b in zip(self.points, self.points[1:])]
        if not (all(d > 0 for d in deltas) or all(d < 0 for d in deltas)):
            raise ValueError("Pulse mapping must be strictly monotonic")
        if any(not 900 <= pulse <= 2100 for _, pulse in self.points):
            raise ValueError("Pulse map exceeds configured electrical pulse envelope")
        self.minimum, self.maximum = self.points[0][0], self.points[-1][0]
        if not self.minimum < 0 < self.maximum or not any(
            a == 0 for a, _ in self.points
        ):
            raise ValueError(
                "Calibration must include an explicitly measured forward zero"
            )
        self.settle_seconds = float(data.get("settle_seconds", 1.0))
        if (
            not math.isfinite(self.settle_seconds)
            or not 0.2 <= self.settle_seconds <= 5
        ):
            raise ValueError("Settling delay must be 0.2 to 5 seconds")

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def pulse(self, angle):
        angle = float(angle)
        if not math.isfinite(angle) or not self.minimum <= angle <= self.maximum:
            raise ValueError("Requested pan lies outside the measured safe travel")
        for (a, u), (b, v) in zip(self.points, self.points[1:]):
            if a <= angle <= b:
                return round(u + (v - u) * (angle - a) / (b - a))
        raise ValueError("Pan interpolation failed")
