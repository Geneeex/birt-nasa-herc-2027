"""Live radio-to-task controller. Defaults cannot actuate uncommissioned hardware."""

import argparse
import json
import math
import os
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from . import commissioning
from .evidence import Evidence, atomic_json, claim_attempt, utc_now
from .pan import PanMap
from .protocol import KissDecoder, ProtocolError, parse_sequence
from .supervision import require_watchdog
from .hardware import HardwareConfig


def notify(value="WATCHDOG=1"):
    """Notify systemd from the main execution path, not the heartbeat thread."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(value.encode())
    except OSError:
        pass


def checked_wait(seconds, motion):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        motion.check()
        notify()
        time.sleep(min(0.05, max(0, until - time.monotonic())))


def execute(
    parsed, pan, motion, camera, calibration, evidence, range_settings=None, limit=180
):
    from .vision import (
        apply_filter,
        estimate_consistent_range,
        validate_capture_metadata,
    )

    started = time.monotonic()
    angle, selected_filter, partial = 0.0, "none", False
    try:
        # Recheck against the actual loaded map before START, even if a caller
        # parsed the packet with different bounds. Never fail halfway into an
        # otherwise predictable out-of-range movement.
        if parsed.initial_pan != 0:
            raise ValueError(
                "This executor requires calibrated forward-zero initial pan"
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, (int, float))
            or not math.isfinite(limit)
            or not 0 < limit <= 180
        ):
            raise ValueError("Execution limit must be between zero and 180 seconds")
        future_angle = 0.0
        pan.pulse(future_angle)
        for token in parsed.tokens:
            if token not in ("B", "C", "D", "E", "F", "G", "H"):
                raise ValueError("Unknown operational command")
            if token in ("B", "C"):
                future_angle += 30 if token == "B" else -30
                pan.pulse(future_angle)
        motion.begin()
        motion.move(pan.pulse(0))
        checked_wait(pan.settle_seconds, motion)
        for index, token in enumerate(parsed.tokens, 1):
            motion.check()
            notify()
            if time.monotonic() - started >= limit:
                raise TimeoutError("Team execution time limit exceeded")
            if token in ("B", "C"):
                angle += 30 if token == "B" else -30
                motion.move(pan.pulse(angle))
                checked_wait(pan.settle_seconds, motion)
                evidence.event(
                    step=index, command=token, pan_deg=angle, executed_utc=utc_now()
                )
            elif token in ("E", "F", "G"):
                selected_filter = {"E": "red", "F": "blue", "G": "none"}[token]
                evidence.event(
                    step=index,
                    command=token,
                    filter=selected_filter,
                    executed_utc=utc_now(),
                )
            elif token == "D":
                capture = camera.capture()
                validate_capture_metadata(calibration, capture.metadata)
                motion.check()
                original = evidence.image(
                    capture.bgr, f"{index:02d}_original.png", capture.captured_at_utc
                )
                result = evidence.image(
                    apply_filter(capture.bgr, selected_filter),
                    f"{index:02d}_result.png",
                    capture.captured_at_utc,
                    stamp=True,
                )
                evidence.event(
                    step=index,
                    command=token,
                    pan_deg=angle,
                    filter=selected_filter,
                    original=original,
                    result=result,
                    camera_metadata=capture.metadata,
                )
            elif token == "H":
                captures = []
                for _ in range(3):
                    capture = camera.capture()
                    validate_capture_metadata(calibration, capture.metadata)
                    captures.append(capture)
                    motion.check()
                    notify()
                result = estimate_consistent_range(
                    [c.bgr for c in captures],
                    calibration,
                    range_settings,
                    metadata=[c.metadata for c in captures],
                )
                source_images = [
                    evidence.image(
                        c.bgr, f"{index:02d}_range_{n}.png", c.captured_at_utc
                    )
                    for n, c in enumerate(captures, 1)
                ]
                evidence.event(
                    step=index,
                    command=token,
                    pan_deg=angle,
                    range=result,
                    source_images=source_images,
                    measured_utc=captures[-1].captured_at_utc,
                )
                partial |= not result["available"]
            if time.monotonic() - started >= limit:
                raise TimeoutError("Team execution time limit exceeded")
        motion.finish()
        evidence.finish("PARTIAL" if partial else "DONE")
    except BaseException as exc:
        # Preserve the original fault even if storage or GPIO cleanup fails.
        try:
            motion.stop()
        except Exception:
            pass
        try:
            evidence.finish(
                (
                    "INTERRUPTED"
                    if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "FAULT"
                ),
                exc,
            )
        except Exception:
            pass
        raise


def validate_live_configuration(cfg):
    nasa = cfg.get("nasa", {})
    for item in (
        "frequency_hz",
        "source_callsign",
        "source_ssids",
        "confirmation_reference",
    ):
        if not nasa.get(item):
            raise ValueError("NASA configuration is incomplete: " + item)
    if (
        type(nasa["frequency_hz"]) is not int
        or not 144000000 <= nasa["frequency_hz"] <= 148000000
    ):
        raise ValueError("Frequency lies outside the handbook receiver band")
    if not isinstance(nasa["source_callsign"], str) or not re.fullmatch(
        r"[A-Za-z0-9]{1,6}", nasa["source_callsign"]
    ):
        raise ValueError("Source callsign must contain 1-6 letters or digits")
    if not isinstance(nasa["source_ssids"], list) or any(
        type(x) is not int or not 0 <= x <= 15 for x in nasa["source_ssids"]
    ):
        raise ValueError("Source SSIDs must be integers from 0 through 15")
    if (
        not isinstance(nasa["confirmation_reference"], str)
        or not nasa["confirmation_reference"].strip()
    ):
        raise ValueError("Record the NASA confirmation reference")
    for key in (
        "remote_arm_confirmed",
        "whole_packet_sequence_confirmed",
        "digital_filter_confirmed",
        "range_reference_confirmed",
    ):
        if nasa.get(key) is not True:
            raise ValueError("Retain NASA confirmation before enabling " + key)
    if type(cfg.get("schema")) is not int or cfg["schema"] != 2:
        raise ValueError("Use Pi 5 configuration schema 2")
    HardwareConfig(**cfg["hardware"])
    limit = cfg.get("execution_limit_seconds", 180)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not math.isfinite(limit)
        or not 1 <= limit <= 180
    ):
        raise ValueError("Execution limit must be from 1 through 180 seconds")
    port = cfg.get("kiss_port", 8001)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("KISS TCP port must be from 1 through 65535")
    excursion = cfg.get("excursion_id", "")
    if (
        not isinstance(excursion, str)
        or excursion == "SET_IN_PIT"
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", excursion)
    ):
        raise ValueError("Set a unique excursion identifier before live operation")
    if not isinstance(cfg.get("clock_check_utc"), str):
        raise ValueError("Clock-check timestamp must be an ISO 8601 string")
    stamp = datetime.fromisoformat(cfg["clock_check_utc"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Clock-check timestamp requires timezone")
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if not 0 <= age <= 86400:
        raise ValueError(
            "Record a trusted offline clock check within the last 24 hours"
        )


def live(config_path):
    from .camera import Camera, CameraConfig
    from .hardware import PiMotion
    from .vision import RangeSettings, load_calibration

    config_path = Path(config_path).resolve()
    base = config_path.parent
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    validate_live_configuration(cfg)
    require_watchdog()
    commissioning.verify(config_path, base / cfg["commissioning"])
    pan = PanMap.load(base / cfg["pan_calibration"])
    camera_config = CameraConfig(**cfg["camera"])
    calibration = load_calibration(
        base / cfg["camera_calibration"], camera_config.as_dict()
    )
    range_settings = RangeSettings(**cfg.get("range_settings", {}))
    root = (base / cfg.get("results_directory", "runs")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # A crashed previous attempt stays locked and is labelled honestly for PER.
    for item in root.glob("*/manifest.json"):
        record = json.loads(item.read_text(encoding="utf-8"))
        if record.get("status") == "RUNNING":
            record.update(status="INTERRUPTED", interruption_detected_utc=utc_now())
            atomic_json(item, record)
    if (root / "state" / (cfg["excursion_id"] + ".lock")).exists():
        raise RuntimeError("This excursion already has an attempt; no automatic retry")
    if shutil.disk_usage(root).free < 1024**3:
        raise RuntimeError("At least 1 GiB of free evidence storage is required")
    motion, camera = None, None
    try:
        motion = PiMotion(cfg["hardware"])
        if motion.status()["mode"] != "LIVE":
            raise RuntimeError("Bench jumper is installed; live operation refused")
        camera = Camera(camera_config).start()
        notify("READY=1\nSTATUS=Waiting for remote arm")
        while motion.status()["state"] != "ARMED":
            checked_wait(0.1, motion)
        # New TCP connection after arming discards all pre-arm receive data.
        while True:
            motion.check()
            notify()
            try:
                connection = socket.create_connection(
                    ("127.0.0.1", cfg.get("kiss_port", 8001)), timeout=1
                )
                break
            except OSError:
                checked_wait(0.5, motion)
        decoder = KissDecoder(port=0)
        with connection:
            connection.settimeout(0.25)
            while True:
                motion.check()
                notify()
                if motion.status()["state"] != "ARMED":
                    raise RuntimeError("Remote arming was withdrawn before execution")
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise RuntimeError("Packet decoder connection closed")
                for frame in decoder.feed(chunk):
                    try:
                        parsed = parse_sequence(
                            frame,
                            source_callsign=cfg["nasa"]["source_callsign"],
                            source_ssids=cfg["nasa"]["source_ssids"],
                            pan_min=pan.minimum,
                            pan_max=pan.maximum,
                        )
                    except ProtocolError as exc:
                        print("Rejected packet:", exc, flush=True)
                        continue
                    claim_attempt(root, cfg["excursion_id"])
                    evidence = Evidence(root, parsed, excursion_id=cfg["excursion_id"])
                    evidence.record["configuration_fingerprint"] = (
                        commissioning.fingerprint(config_path)
                    )
                    evidence.flush()
                    execute(
                        parsed,
                        pan,
                        motion,
                        camera,
                        calibration,
                        evidence,
                        range_settings,
                        cfg.get("execution_limit_seconds", 180),
                    )
                    print("Results:", evidence.path, flush=True)
                    return
    finally:
        # Stop servo signalling before a camera close that could fail or block.
        try:
            if motion:
                motion.close()
        finally:
            if camera:
                camera.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    live(args.config)


if __name__ == "__main__":
    main()
