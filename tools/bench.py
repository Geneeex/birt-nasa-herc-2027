"""Explicit physical BENCH test of the real KISS-to-evidence mission path.

Requires GPIO22-to-ground before process startup and --confirm-motion. All records are
BENCH/noncompetition. No NASA confirmations or fabricated acceptance record
are needed; measured pan/camera calibration and ordinary execution limits are.
Run under the documented systemd watchdog for camera-hang testing.
"""

import argparse
import json
import re
import shutil
import socket
import time
from pathlib import Path
from uuid import uuid4

from herc.camera import Camera, CameraConfig
from herc.commissioning import fingerprint
from herc.controller import checked_wait, execute, notify
from herc.evidence import Evidence, claim_attempt
from herc.hardware import PiMotion, HardwareConfig
from herc.pan import PanMap
from herc.protocol import KissDecoder, ProtocolError, parse_sequence
from herc.supervision import require_watchdog
from herc.vision import RangeSettings, load_calibration


def require_bench(motion, expected_state):
    status = motion.status()
    if status.get("mode") != "BENCH":
        raise RuntimeError(
            "Physical GPIO22-to-GND bench jumper must be fitted before startup"
        )
    if status.get("state") != expected_state:
        raise RuntimeError(
            "Bench controller must be " + expected_state + "; reset safely in the pit"
        )


def wait_for_packet(motion, pan, callsign, ssids, port, wait_seconds):
    """Open a fresh loopback KISS connection; never transmit or replay a packet."""
    deadline = time.monotonic() + wait_seconds
    connection = None
    while connection is None:
        motion.check()
        require_bench(motion, "ARMED")
        notify()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Bench packet wait expired")
        try:
            connection = socket.create_connection(
                ("127.0.0.1", port), timeout=min(1.0, remaining)
            )
        except OSError:
            checked_wait(min(0.2, remaining), motion)
    decoder = KissDecoder(port=0)
    with connection:
        connection.settimeout(0.25)
        print("BENCH KISS connected. Send the authorized test packet now.", flush=True)
        while time.monotonic() < deadline:
            motion.check()
            require_bench(motion, "ARMED")
            notify()
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError("Bench decoder connection closed")
            for frame in decoder.feed(chunk):
                try:
                    return parse_sequence(
                        frame,
                        source_callsign=callsign,
                        source_ssids=ssids,
                        pan_min=pan.minimum,
                        pan_max=pan.maximum,
                    )
                except ProtocolError as exc:
                    print("Rejected BENCH packet:", exc, flush=True)
    raise TimeoutError("Bench packet wait expired")


def run(
    config_path,
    source_callsign,
    source_ssids,
    *,
    confirm_motion=False,
    output=None,
    wait_seconds=120,
):
    if confirm_motion is not True:
        raise ValueError(
            "Explicit --confirm-motion is required for this physical bench test"
        )
    # Systemd supervision complements the in-process GPIO output-disable monitor.
    require_watchdog()
    if not isinstance(source_callsign, str) or not re.fullmatch(
        r"[A-Z0-9]{1,6}", source_callsign.upper()
    ):
        raise ValueError("Supply the explicit 1–6 character bench sender identity")
    if not source_ssids or any(
        type(x) is not int or not 0 <= x <= 15 for x in source_ssids
    ):
        raise ValueError("Supply explicit bench SSIDs from 0 through 15")
    if not 1 <= wait_seconds <= 600:
        raise ValueError("Bench packet wait must be from 1 through 600 seconds")
    path = Path(config_path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    limit = cfg.get("execution_limit_seconds", 180)
    if cfg.get("schema") != 2 or not 1 <= limit <= 180:
        raise ValueError(
            "Use schema 2 and an execution limit from 1 through 180 seconds"
        )
    HardwareConfig(**cfg["hardware"])
    port = cfg.get("kiss_port", 8001)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid KISS TCP port")
    pan = PanMap.load(path.parent / cfg["pan_calibration"])
    camera_config = CameraConfig(**cfg["camera"])
    calibration = load_calibration(
        path.parent / cfg["camera_calibration"], camera_config.as_dict()
    )
    range_settings = RangeSettings(**cfg.get("range_settings", {}))
    root = Path(output).resolve() if output else path.parent / "bench_runs"
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < 1024**3:
        raise RuntimeError("At least 1 GiB of free evidence storage is required")
    motion = camera = None
    try:
        motion = PiMotion(cfg["hardware"])
        require_bench(motion, "DISARMED")
        camera = Camera(camera_config).start()
        require_bench(motion, "DISARMED")
        motion.arm_bench()
        notify("READY=1\nSTATUS=BENCH waiting for explicitly configured test sender")
        packet = wait_for_packet(
            motion, pan, source_callsign, source_ssids, port, wait_seconds
        )
        require_bench(motion, "ARMED")
        attempt = "BENCH_" + uuid4().hex[:16]
        claim_attempt(root, attempt)
        evidence = Evidence(root, packet, mode="BENCH", excursion_id=attempt)
        evidence.record["bench_notice"] = (
            "Physical bench test only; not competition evidence or NASA authorization."
        )
        evidence.record["bench_input"] = (
            "Fresh loopback KISS data from explicitly configured bench sender"
        )
        evidence.record["bench_configuration_fingerprint"] = fingerprint(path)
        evidence.flush()
        execute(
            packet, pan, motion, camera, calibration, evidence, range_settings, limit
        )
        return evidence.path
    finally:
        # Stop pulses first, even if closing a failed camera raises or blocks.
        try:
            if motion is not None:
                motion.close()
        finally:
            if camera is not None:
                camera.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--source-callsign", required=True)
    parser.add_argument("--source-ssid", type=int, action="append", required=True)
    parser.add_argument("--confirm-motion", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--wait-seconds", type=float, default=120)
    args = parser.parse_args()
    print(
        "BENCH physical movement requested; keep the guarded pan area clear.",
        flush=True,
    )
    result = run(
        args.config,
        args.source_callsign,
        args.source_ssid,
        confirm_motion=args.confirm_motion,
        output=args.output,
        wait_seconds=args.wait_seconds,
    )
    print("BENCH/noncompetition evidence:", result, flush=True)


if __name__ == "__main__":
    main()
