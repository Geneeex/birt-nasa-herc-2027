"""Inspect Pi 5 task hardware and receiver arming without commanding movement."""

import argparse
import json
from pathlib import Path
import time

from herc.hardware import PiMotion


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("Observation must last 1 through 300 seconds")
    cfg = json.loads(Path(args.config).read_text())
    motion = PiMotion(cfg["hardware"])
    try:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            print(motion.status(), flush=True)
            time.sleep(0.05)
    finally:
        motion.close()


if __name__ == "__main__":
    main()
