"""Run the receive-only SDR pipeline using the same configured frequency as the controller."""

import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    path = Path(args.config).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    freq = cfg.get("nasa", {}).get("frequency_hz")
    if not isinstance(freq, int) or not 144000000 <= freq <= 148000000:
        raise ValueError("Enter NASA's confirmed receive frequency in config.json")
    gain, ppm = float(cfg.get("sdr_gain_db", 30)), int(cfg.get("sdr_ppm", 0))
    rtl = subprocess.Popen(
        [
            "rtl_fm",
            "-M",
            "fm",
            "-f",
            str(freq),
            "-s",
            "24000",
            "-g",
            str(gain),
            "-p",
            str(ppm),
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    wolf = None
    try:
        wolf = subprocess.Popen(
            [
                "direwolf",
                "-c",
                str(path.parent / "deploy/direwolf.conf"),
                "-r",
                "24000",
                "-D",
                "1",
                "-t",
                "0",
                "-",
            ],
            stdin=rtl.stdout,
        )
        rtl.stdout.close()
        while rtl.poll() is None and wolf.poll() is None:
            time.sleep(0.2)
        raise RuntimeError("Receiver process ended; inspect the service log")
    finally:
        for process in (wolf, rtl):
            if process and process.poll() is None:
                process.terminate()
        for process in (wolf, rtl):
            if process:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
