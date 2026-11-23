"""Atomic evidence records and a persistent one-attempt-per-excursion lock."""

import hashlib
import json
import os
import re
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sync_directory(path):
    """Persist directory entries on the Linux target; unsupported on Windows."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _png_text(keyword, value):
    payload = keyword.encode("latin-1") + b"\0" + value.encode("latin-1")
    body = b"tEXt" + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as out:
        json.dump(value, out, indent=2, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    _sync_directory(path.parent)


def claim_attempt(root, excursion_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", excursion_id):
        raise ValueError("Use a short alphanumeric excursion identifier")
    path = Path(root) / "state" / (excursion_id + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents duplicate starts, including after process restart.
    with path.open("x", encoding="utf-8") as out:
        out.write(utc_now())
        out.flush()
        os.fsync(out.fileno())
    _sync_directory(path.parent)
    return path


class Evidence:
    def __init__(self, root, parsed, mode="LIVE", excursion_id="unassigned"):
        self.path = Path(root) / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8]
        )
        self.path.mkdir(parents=True)
        self.record = {
            "schema": 1,
            "mode": mode,
            "excursion_id": excursion_id,
            "status": "RUNNING",
            "started_utc": utc_now(),
            "source": parsed.source,
            "commands": list(parsed.tokens),
            "ax25_hex": parsed.raw_frame.hex(),
            "information_hex": parsed.information.hex(),
            "results": [],
        }
        self.flush()

    def flush(self):
        atomic_json(self.path / "manifest.json", self.record)

    def event(self, **values):
        self.record["results"].append(values)
        self.flush()

    def image(self, bgr, name, timestamp, stamp=False):
        import cv2
        import numpy as np

        if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", name):
            raise ValueError("Evidence image name must be a simple PNG filename")
        if stamp:
            band = np.full((130, bgr.shape[1], 3), 255, np.uint8)
            label = self.record["mode"] + " | " + timestamp + " UTC"
            nominal_width = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 1.4, 3)[0][
                0
            ]
            scale = 1.4 * min(1.0, max(1, bgr.shape[1] - 50) / nominal_width)
            cv2.putText(
                band,
                label,
                (25, 85),
                cv2.FONT_HERSHEY_SIMPLEX,
                scale,
                (0, 0, 0),
                2,
                cv2.LINE_AA,
            )
            bgr = np.vstack([bgr, band])
        ok, data = cv2.imencode(".png", bgr)
        if not ok:
            raise RuntimeError("PNG encoding failed")
        # PNG ancillary text leaves decoded image pixels unchanged. Every source
        # and result file contains its capture date/time as well as the manifest.
        encoded = data.tobytes()
        encoded = (
            encoded[:33]
            + _png_text("CaptureTimeUTC", timestamp)
            + _png_text("EvidenceMode", self.record["mode"])
            + encoded[33:]
        )
        target = self.path / name
        with target.open("xb") as out:
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        _sync_directory(target.parent)
        return {
            "file": name,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "captured_at_utc": timestamp,
        }

    def finish(self, status, error=None):
        self.record.update(status=status, finished_utc=utc_now())
        if error:
            self.record["error"] = str(error)
        self.flush()
