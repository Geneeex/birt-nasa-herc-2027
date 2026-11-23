"""Convert an already mono 16-bit 24 kHz WAV to PCM stdout for Dire Wolf stdin."""

import argparse
import sys
import time
import wave


def main():
    p = argparse.ArgumentParser()
    p.add_argument("wav")
    p.add_argument(
        "--startup-delay",
        type=float,
        default=0.0,
        help="Wait this many seconds before PCM, allowing a KISS client to connect",
    )
    a = p.parse_args()
    if not 0 <= a.startup_delay <= 60:
        raise ValueError("Startup delay must be from zero through 60 seconds")
    with wave.open(a.wav, "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (
            1,
            2,
            24000,
        ):
            raise ValueError(
                "Generate a mono 16-bit WAV at 24000 Hz; no silent resampling is performed"
            )
        time.sleep(a.startup_delay)
        while chunk := source.readframes(4096):
            sys.stdout.buffer.write(chunk)


if __name__ == "__main__":
    main()
