"""Interactive BENCH-only measured calibration. Remove horn before first pulse."""

import argparse
import json
import select
import sys
from pathlib import Path
from herc.hardware import PiMotion
from herc.pan import PanMap
from herc.evidence import atomic_json, utc_now


def read_input(device, prompt):
    print(prompt, end="", flush=True)
    while not select.select([sys.stdin], [], [], 0.1)[0]:
        device.check()
    device.check()
    line = sys.stdin.readline()
    if not line:
        raise EOFError("Calibration input closed")
    return line.rstrip("\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.json")
    p.add_argument("--output", default="calibration/pan.json")
    a = p.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    device = PiMotion(cfg["hardware"])
    points, pulse = {}, 1500
    try:
        if device.status()["mode"] != "BENCH":
            raise RuntimeError(
                "GPIO22-to-GND bench jumper must be fitted before startup"
            )
        print(
            "Horn removed, servo free, separate 5 V supply verified. This session is limited to 180 seconds."
        )
        if read_input(device, "Type BENCH to permit servo pulses: ").strip() != "BENCH":
            return
        device.arm_bench()
        device.begin()
        device.move(pulse)
        print(
            "p 1510 = pulse; a -30 = record MEASURED angle at current pulse; save = write; q = stop"
        )
        print(
            "At centre, install the horn with all power removed, then restart this tool. Do not force the shaft."
        )
        while True:
            device.check()
            entry = read_input(device, "> ").strip().split()
            if not entry:
                continue
            if entry[0] == "q":
                break
            if entry[0] == "p" and len(entry) == 2:
                new = int(entry[1])
                if not 900 <= new <= 2100 or abs(new - pulse) > 50:
                    print(
                        "Use <=50 us steps within 900..2100, stopping before a mechanical limit."
                    )
                    continue
                device.move(new)
                pulse = new
            elif entry[0] == "a" and len(entry) == 2:
                points[float(entry[1])] = pulse
                print("Recorded", sorted(points.items()))
            elif entry[0] == "save":
                operator = read_input(device, "Operator name: ").strip()
                record = read_input(
                    device, "Angle/repeatability test record identifier: "
                ).strip()
                settle = float(
                    read_input(
                        device, "Measured conservative settling delay in seconds: "
                    )
                )
                if not operator or not record:
                    raise ValueError("Operator and physical record are required")
                data = {
                    "measured": True,
                    "points": sorted(points.items()),
                    "settle_seconds": settle,
                    "operator": operator,
                    "record": record,
                    "created_utc": utc_now(),
                }
                PanMap(data)
                atomic_json(a.output, data)
                print(
                    "Saved",
                    a.output,
                    "Repeatable angles must still pass field verification.",
                )
                break
            else:
                print("Unknown command")
    finally:
        device.close()


if __name__ == "__main__":
    main()
