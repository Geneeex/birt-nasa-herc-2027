# BIRT NASA HERC 2027

RC autonomous camera-task software for the BIRT rover, targeting **Raspberry Pi 5 Model B 8GB** with a dedicated **PCA9685** servo driver.

This repository contains the 33 source and configuration listings from Appendix A of the Revision 6 design contribution. The implementation is software version **5.0.0**, unchanged from the Revision 5 code package. The Revision 6 update separated the design report from the operating instructions.

## What the system does

- Receives decoded radio packets through a local Dire Wolf KISS connection and validates their sender, structure and command sequence.
- Moves the camera using measured pan calibration, including commanded 30-degree turns within the calibrated travel limits.
- Captures timestamped images and applies red, blue or unfiltered digital channel selection.
- Estimates target distance in feet from calibrated camera geometry.
- Saves original images, processed images, measurements and execution records for an offline results viewer.
- Handles receiver arming, duplicate-attempt protection, output disable and supervised task execution.

The Pi is the only task computer; there is no Arduino firmware. This package covers the autonomous camera task. It does not provide rover propulsion, steering, a physical filter-wheel driver or a LiDAR driver.

## Repository layout

```text
herc/                       Runtime, hardware interfaces and evidence viewer
tools/                      Calibration, bench testing, simulation and deployment
tests/                      Host regression tests with substituted hardware
calibration/                Pan calibration template
deploy/                     Receive-only Dire Wolf configuration
docs/                       Setup guide, architecture and verification status
.github/workflows/          Automated host tests
config.example.json         Configuration template with unset mission values
acceptance.template.json     Physical-test evidence template
requirements-host.txt        Development-computer dependencies only
pyproject.toml               Python formatting and lint configuration
```

## Start here

1. [System architecture and module map](docs/architecture.md)
2. [Pi wiring, installation, calibration and operation](docs/setup-and-operation.md)
3. [Verification status and report traceability](docs/verification.md)

Run the commands from the repository root. For the rover, use the Raspberry Pi OS packages specified in the setup guide; the host dependency file below is for a separate development environment.

## Run the host tests

Python 3.11 or newer is required. In an activated development virtual environment:

```bash
python -m pip install -r requirements-host.txt
python -m unittest discover -s tests -v
```

## Try the software simulation

```bash
python -m tools.simulate --output demo_runs
python -m herc.review --root demo_runs
```

Open http://127.0.0.1:8000 on the same computer. Simulation uses synthetic targets and substituted motion; it does not open GPIO, a physical camera or an SDR. Stop the viewer with Ctrl+C.

## Deployment status

The release has 93 host tests. Host tests do not demonstrate electrical compatibility, actual pan angles, outdoor ranging accuracy, radio reception or stopping time on the rover. Measured calibration, physical qualification and the outstanding organizer interface confirmations remain necessary before live operation.

The configuration intentionally leaves mission frequency, sender identity and confirmation fields unset. Digital filtering and camera-based ranging are implementation choices pending organizer confirmation. The physical filter wheel discussed in earlier concepts is not implemented here.

Source files retain their report paths so imports, calibration references and evidence fingerprints remain consistent. Local mission settings, measured calibration, acceptance records and captured results are excluded by `.gitignore`.
