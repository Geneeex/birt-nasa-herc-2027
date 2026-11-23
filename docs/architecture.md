# System architecture

## Signal path

```text
Receive antenna -> USB SDR -> rtl_fm -> Dire Wolf -> localhost KISS :8001
                                                        |
                                                  herc.protocol
                                                        |
                                                  herc.controller
                         +------------------------------+--------------------+
                         |                              |                    |
                    herc.hardware                  herc.camera          herc.evidence
                    RP1 GPIO + I2C                  Picamera2             saved records
                         |                              |                    |
                      PCA9685                       herc.vision           herc.review
                         |                       filters and ranging     localhost :8000
                     pan servo
```

The RC receiver provides arming permission through a protected GPIO input. It does not supply the autonomous task sequence. A dedicated PCA9685 generates the camera-servo pulses; it is not a second task computer.

## Runtime modules

| Module | Responsibility |
| --- | --- |
| `herc.protocol` | KISS framing, AX.25 sender checks, command parsing and pan-path preflight |
| `herc.controller` | Validated one-attempt task execution, state checks and result handling |
| `herc.hardware` | Pi 5 RP1 GPIO discovery, I2C PCA9685 control and output-enable management |
| `herc.rc_input` | PWM validity, freshness and deliberate low-to-high arming |
| `herc.pan` | Interpolation from measured viewing angle to servo pulse width |
| `herc.camera` | Fixed-configuration Camera Module 3 acquisition through Picamera2 |
| `herc.vision` | Digital filtering, target validation and calibrated geometric ranging |
| `herc.evidence` | Atomic records, images, timestamps and persistent attempt locks |
| `herc.commissioning` | Configuration/source fingerprints and physical-test record checks |
| `herc.supervision` | Required systemd watchdog configuration checks |
| `herc.review` | Read-only local evidence display |

## Supporting tools

`tools.calibrate_pan` and `tools.calibrate_camera` produce measured calibration. `tools.check_hardware` observes the driver and receiver without commanding motion. `tools.bench` exercises the physical task path in explicit BENCH mode. `tools.simulate` exercises the software without physical devices. `tools.receive` runs the receive-only decoder chain; `tools.wav_to_pcm` supports recorded-audio bench input. `tools.commission` records physical acceptance evidence identifiers, and `tools.services` generates systemd service files for the installed path.

The wiring and operating procedures are maintained separately in [setup-and-operation.md](setup-and-operation.md). No bounded stop time is claimed for a complete Pi/kernel failure; the independent servo-power cutoff remains part of the physical design.
